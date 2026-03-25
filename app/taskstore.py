from datetime import datetime, timezone
from typing import Any

from google.cloud import firestore

_db: firestore.Client | None = None

COLLECTION = "tasks"


def _get_db() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client()
    return _db


def create_task(task_id: str, filename: str) -> dict[str, Any]:
    doc = {
        "task_id": task_id,
        "filename": filename,
        "status": "uploading",
        "operation_id": None,
        "text": None,
        "audio_duration_seconds": None,
        "cost_estimate": None,
        "error": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _get_db().collection(COLLECTION).document(task_id).set(doc)
    return doc


def update_task(task_id: str, **fields: Any) -> None:
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    _get_db().collection(COLLECTION).document(task_id).update(fields)


def get_task(task_id: str) -> dict[str, Any] | None:
    doc = _get_db().collection(COLLECTION).document(task_id).get()
    if not doc.exists:
        return None
    return doc.to_dict()
