import logging
import math
import os
import subprocess
import tempfile
from datetime import timedelta
import uuid

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from google.cloud import storage as gcs_storage

from app.auth import verify_api_key
from app.config import settings
from app import storage, stt, taskstore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Ears", description="Audio transcription service")

MAX_FILE_SIZE = settings.max_audio_size_mb * 1024 * 1024

NEEDS_CONVERT = {".m4a", ".aac", ".wma", ".flac", ".webm"}

def _get_audio_duration_seconds(path: str) -> float:
    """Get media duration in seconds via ffprobe."""
    # ffprobe output example: "12.345678"
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    duration_str = (result.stdout or "").strip()
    if not duration_str or duration_str.lower() == "nan":
        raise RuntimeError(f"ffprobe failed to parse duration for {path}")
    return float(duration_str)


def _estimate_cost_from_duration(duration_seconds: float) -> float:
    """Deterministic STT cost estimate from audio duration.

    Note: This is an estimate; real billing may differ.
    """
    price_per_second = float(getattr(settings, "stt_price_per_second", 0.0) or 0.0)
    price_per_minute = float(getattr(settings, "stt_price_per_minute", 0.0) or 0.0)
    rounding_mode = str(getattr(settings, "stt_billing_rounding", "ceil_minute") or "ceil_minute")

    # Prefer exact per-second pricing if provided; otherwise fall back to per-minute.
    if price_per_second > 0:
        if rounding_mode == "none":
            billed_seconds = duration_seconds
        elif rounding_mode == "ceil_second":
            billed_seconds = math.ceil(duration_seconds)
        elif rounding_mode == "round":
            billed_seconds = round(duration_seconds)
        else:
            # Default: ceil to minutes, then charge full minutes as seconds.
            billed_seconds = math.ceil(duration_seconds / 60.0) * 60.0
        return round(billed_seconds * price_per_second, 6)

    if price_per_minute > 0:
        if rounding_mode == "none":
            billed_minutes = duration_seconds / 60.0
        elif rounding_mode == "ceil_second":
            # Convert second rounding to minute billing (still deterministic).
            billed_minutes = math.ceil(duration_seconds / 60.0)
        elif rounding_mode == "round":
            billed_minutes = round(duration_seconds / 60.0)
        else:
            billed_minutes = math.ceil(duration_seconds / 60.0)
        return round(billed_minutes * price_per_minute, 6)

    return 0.0


_gcs_client: gcs_storage.Client | None = None


def _get_gcs_client() -> gcs_storage.Client:
    global _gcs_client
    if _gcs_client is None:
        _gcs_client = gcs_storage.Client()
    return _gcs_client


def _parse_gcs_uri(gcs_uri: str) -> tuple[str, str]:
    if not gcs_uri.startswith("gs://"):
        raise ValueError("gcs_uri must start with gs://")
    _, rest = gcs_uri.split("gs://", 1)
    parts = rest.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError("gcs_uri must be in form gs://bucket/object")
    return parts[0], parts[1]


class TranscribeGcsRequest(BaseModel):
    gcs_uri: str
    lang: str | None = None


def _process_audio_from_gcs(task_id: str, gcs_uri: str, language_code: str | None = None) -> None:
    """Download audio from GCS, cleanup, then reuse the existing pipeline."""
    try:
        bucket_name, object_name = _parse_gcs_uri(gcs_uri)
        if bucket_name != settings.gcs_upload_bucket:
            raise ValueError(f"gcs_uri bucket must be '{settings.gcs_upload_bucket}'")

        client = _get_gcs_client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(object_name)

        if not blob.exists():
            raise FileNotFoundError("GCS object not found")

        # Best-effort size check (can be None for some backends).
        if blob.size is not None and blob.size > MAX_FILE_SIZE:
            raise ValueError(f"File too large (max {settings.max_audio_size_mb}MB)")

        filename = os.path.basename(object_name) or "audio"
        file_data = blob.download_as_bytes()
        if len(file_data) > MAX_FILE_SIZE:
            raise ValueError(f"File too large (max {settings.max_audio_size_mb}MB)")

        # Cleanup: delete the source file after we have a local copy.
        try:
            blob.delete()
        except Exception:
            logger.warning("Failed to delete GCS object %s (continuing)", gcs_uri, exc_info=True)

        _process_audio(task_id, filename, file_data, language_code=language_code)

    except Exception as e:
        logger.exception("Processing from GCS failed for %s", task_id)
        taskstore.update_task(task_id, status="error", error=str(e))


def _convert_to_mp3(file_data: bytes, original_ext: str) -> tuple[bytes, str, float]:
    """Convert unsupported audio formats to MP3 via ffmpeg."""
    with tempfile.NamedTemporaryFile(suffix=original_ext, delete=False) as src:
        src.write(file_data)
        src_path = src.name
    dst_path = src_path.rsplit(".", 1)[0] + ".mp3"
    duration_seconds: float
    try:
        subprocess.run(
            ["ffmpeg", "-i", src_path, "-vn", "-acodec", "libmp3lame", "-q:a", "4", dst_path, "-y"],
            check=True,
            capture_output=True,
        )
        duration_seconds = _get_audio_duration_seconds(dst_path)
        with open(dst_path, "rb") as f:
            return f.read(), "converted.mp3", duration_seconds
    finally:
        for p in (src_path, dst_path):
            if os.path.exists(p):
                os.unlink(p)


def _process_audio(task_id: str, filename: str, file_data: bytes, language_code: str | None = None) -> None:
    """Background task: upload to S3, start STT, poll for result."""
    try:
        # 0. Convert unsupported formats to MP3
        ext = os.path.splitext(filename)[1].lower()
        duration_seconds: float
        if ext in NEEDS_CONVERT:
            logger.info("Converting %s to MP3...", ext)
            taskstore.update_task(task_id, status="converting")
            file_data, filename, duration_seconds = _convert_to_mp3(file_data, ext)
        else:
            # Measure original duration for direct upload.
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                tmp.write(file_data)
                tmp_path = tmp.name
            try:
                duration_seconds = _get_audio_duration_seconds(tmp_path)
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)

        cost_estimate = _estimate_cost_from_duration(duration_seconds)
        taskstore.update_task(
            task_id,
            audio_duration_seconds=duration_seconds,
            cost_estimate=cost_estimate,
        )

        # 1. Upload to Yandex Object Storage
        taskstore.update_task(task_id, status="uploading")
        s3_uri = storage.upload_file(task_id, filename, file_data)
        logger.info("Uploaded %s -> %s", filename, s3_uri)

        # 2. Start async recognition (no waiting — status checked via GET /tasks)
        taskstore.update_task(task_id, status="recognizing")
        operation_id = stt.start_recognition(s3_uri, filename, language_code=language_code)
        taskstore.update_task(task_id, operation_id=operation_id)
        logger.info("STT started for %s, operation_id=%s", task_id, operation_id)

    except Exception as e:
        logger.exception("Processing failed for %s", task_id)
        taskstore.update_task(task_id, status="error", error=str(e))


@app.post("/transcribe", status_code=202)
async def transcribe(
    file: UploadFile,
    background_tasks: BackgroundTasks,
    _: str = Depends(verify_api_key),
):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    file_data = await file.read()
    if len(file_data) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File too large (max {settings.max_audio_size_mb}MB)",
        )

    task_id = str(uuid.uuid4())
    taskstore.create_task(task_id, file.filename)

    background_tasks.add_task(_process_audio, task_id, file.filename, file_data, None)

    return {"task_id": task_id, "status": "processing"}


@app.post("/transcribe-gcs", status_code=202)
async def transcribe_gcs(
    req: TranscribeGcsRequest,
    background_tasks: BackgroundTasks,
    _: str = Depends(verify_api_key),
):
    try:
        bucket_name, object_name = _parse_gcs_uri(req.gcs_uri)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if bucket_name != settings.gcs_upload_bucket:
        raise HTTPException(
            status_code=400,
            detail=f"gcs_uri bucket must be '{settings.gcs_upload_bucket}'",
        )

    client = _get_gcs_client()
    blob = client.bucket(bucket_name).blob(object_name)
    if not blob.exists():
        raise HTTPException(status_code=404, detail="GCS object not found")
    if blob.size is not None and blob.size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File too large (max {settings.max_audio_size_mb}MB)",
        )

    filename = os.path.basename(object_name) or "audio"
    task_id = str(uuid.uuid4())
    taskstore.create_task(task_id, filename)
    background_tasks.add_task(_process_audio_from_gcs, task_id, req.gcs_uri, req.lang)

    return {"task_id": task_id, "status": "processing"}


@app.get("/upload-url")
async def get_upload_url(
    filename: str,
    content_type: str,
    _: str = Depends(verify_api_key),
):
    safe_filename = os.path.basename(filename or "")
    if not safe_filename or safe_filename in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid filename")

    object_name = f"{uuid.uuid4()}/{safe_filename}"
    bucket_name = settings.gcs_upload_bucket

    blob = _get_gcs_client().bucket(bucket_name).blob(object_name)
    upload_url = blob.generate_signed_url(
        expiration=timedelta(minutes=settings.gcs_signed_url_ttl_minutes),
        method="PUT",
        content_type=content_type,
        version="v4",
    )

    return {
        "upload_url": upload_url,
        "gcs_uri": f"gs://{bucket_name}/{object_name}",
    }


@app.get("/tasks/{task_id}")
async def get_task(task_id: str, _: str = Depends(verify_api_key)):
    task = taskstore.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    # Lazy polling: if still recognizing, check STT status
    if task["status"] == "recognizing" and task.get("operation_id"):
        result = stt.check_operation(task["operation_id"])
        if result.done:
            if result.error:
                taskstore.update_task(task_id, status="error", error=result.error)
                task["status"] = "error"
                task["error"] = result.error
            else:
                taskstore.update_task(task_id, status="done", text=result.text)
                task["status"] = "done"
                task["text"] = result.text

    return {
        "task_id": task["task_id"],
        "status": task["status"],
        "text": task.get("text"),
        "error": task.get("error"),
        "created_at": task.get("created_at"),
    }


@app.get("/tasks/{task_id}/download")
async def download_result(task_id: str, _: str = Depends(verify_api_key)):
    task = taskstore.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"] != "done":
        raise HTTPException(status_code=400, detail=f"Task not ready (status: {task['status']})")

    return PlainTextResponse(
        content=task["text"] or "",
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{task_id}.txt"'},
    )


@app.get("/tasks/{task_id}/cost")
async def get_task_cost(task_id: str, _: str = Depends(verify_api_key)):
    task = taskstore.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"] != "done":
        raise HTTPException(status_code=400, detail=f"Task not ready (status: {task['status']})")

    cost_estimate = task.get("cost_estimate")
    if cost_estimate is None:
        raise HTTPException(status_code=404, detail="Cost estimate not available for this task")

    return {
        "task_id": task["task_id"],
        "cost_estimate": cost_estimate,
    }


@app.get("/health")
async def health():
    return {"status": "ok"}
