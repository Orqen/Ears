import logging
import time
from dataclasses import dataclass

import requests

from app.config import settings

logger = logging.getLogger(__name__)

TRANSCRIBE_URL = "https://transcribe.api.cloud.yandex.net/speech/stt/v2/longRunningRecognize"
OPERATION_URL = "https://operation.api.cloud.yandex.net/operations"


def _auth_header() -> dict[str, str]:
    if settings.yc_iam_token:
        return {"Authorization": f"Bearer {settings.yc_iam_token}"}
    return {"Authorization": f"Api-Key {settings.yandex_api_key}"}


def _detect_audio_encoding(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    mapping = {
        "ogg": "OGG_OPUS",
        "opus": "OGG_OPUS",
        "mp3": "MP3",
        "wav": "LINEAR16_PCM",
        "m4a": "LINEAR16_PCM",
        "aac": "LINEAR16_PCM",
    }
    return mapping.get(ext, "OGG_OPUS")


def start_recognition(s3_uri: str, filename: str) -> str:
    """Submit audio for async recognition. Returns operation_id."""
    encoding = _detect_audio_encoding(filename)

    body = {
        "config": {
            "specification": {
                "languageCode": settings.stt_language,
                "model": "general",
                "audioEncoding": encoding,
                "literatureText": True,
            },
            "folderId": settings.yandex_folder_id,
        },
        "audio": {
            "uri": s3_uri,
        },
    }

    resp = requests.post(TRANSCRIBE_URL, json=body, headers=_auth_header(), timeout=30)
    if not resp.ok:
        logger.error("STT request failed %s: %s", resp.status_code, resp.text)
        raise RuntimeError(f"STT error {resp.status_code}: {resp.text}")
    data = resp.json()

    operation_id = data["id"]
    logger.info("STT operation started: %s", operation_id)
    return operation_id


@dataclass
class OperationResult:
    done: bool
    text: str | None = None
    error: str | None = None


def check_operation(operation_id: str) -> OperationResult:
    """Check the status of an async STT operation."""
    resp = requests.get(
        f"{OPERATION_URL}/{operation_id}",
        headers=_auth_header(),
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    if not data.get("done", False):
        return OperationResult(done=False)

    if "error" in data:
        return OperationResult(
            done=True,
            error=f"STT error {data['error'].get('code')}: {data['error'].get('message')}",
        )

    # Extract text from response chunks
    chunks = data.get("response", {}).get("chunks", [])
    texts = []
    for chunk in chunks:
        alternatives = chunk.get("alternatives", [])
        if alternatives:
            texts.append(alternatives[0].get("text", ""))

    full_text = "\n".join(texts)
    return OperationResult(done=True, text=full_text)


def wait_for_result(operation_id: str, max_wait: int = 3600, poll_interval: int = 10) -> OperationResult:
    """Poll until operation completes. Used in background tasks."""
    elapsed = 0
    while elapsed < max_wait:
        result = check_operation(operation_id)
        if result.done:
            return result
        time.sleep(poll_interval)
        elapsed += poll_interval
    return OperationResult(done=True, error="Recognition timed out")
