import logging
import uuid

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse

from app.auth import verify_api_key
from app.config import settings
from app import storage, stt, taskstore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Ears", description="Audio transcription service")

MAX_FILE_SIZE = 150 * 1024 * 1024  # 150 MB


def _process_audio(task_id: str, filename: str, file_data: bytes) -> None:
    """Background task: upload to S3, start STT, poll for result."""
    try:
        # 1. Upload to Yandex Object Storage
        taskstore.update_task(task_id, status="uploading")
        s3_uri = storage.upload_file(task_id, filename, file_data)
        logger.info("Uploaded %s -> %s", filename, s3_uri)

        # 2. Start async recognition
        taskstore.update_task(task_id, status="recognizing")
        operation_id = stt.start_recognition(s3_uri, filename)
        taskstore.update_task(task_id, operation_id=operation_id)

        # 3. Poll for result
        result = stt.wait_for_result(operation_id)

        if result.error:
            taskstore.update_task(task_id, status="error", error=result.error)
            logger.error("STT failed for %s: %s", task_id, result.error)
        else:
            taskstore.update_task(task_id, status="done", text=result.text)
            logger.info("Transcription done for %s", task_id)

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
        raise HTTPException(status_code=413, detail="File too large (max 150MB)")

    task_id = str(uuid.uuid4())
    taskstore.create_task(task_id, file.filename)

    background_tasks.add_task(_process_audio, task_id, file.filename, file_data)

    return {"task_id": task_id, "status": "processing"}


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


@app.get("/health")
async def health():
    return {"status": "ok"}
