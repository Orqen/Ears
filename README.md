# Ears

Audio transcription service powered by Yandex SpeechKit. Accepts audio files via REST API, transcribes them asynchronously, and returns the text result.

Designed to run on **Google Cloud Run** with **Firestore** for task tracking and **Yandex Object Storage** for audio file storage.

## Architecture

```
Client                    Cloud Run (Ears)              Yandex Cloud
  |                            |                            |
  |-- POST /transcribe ------->|                            |
  |<-- 202 { task_id } --------|                            |
  |                            |-- ffmpeg convert --------->|
  |                            |-- upload to S3 ----------->| Object Storage
  |                            |-- start recognition ------>| SpeechKit STT
  |                            |                            |
  |-- GET /tasks/{id} -------->|                            |
  |                            |-- check operation -------->| SpeechKit STT
  |<-- { status, text } -------|                            |
  |                            |                            |
  |-- GET /tasks/{id}/download->|                           |
  |<-- result.txt --------------|                           |
```

## Supported Audio Formats

| Format | Extension | Processing |
|--------|-----------|------------|
| MP3    | `.mp3`    | Direct upload |
| OGG    | `.ogg`    | Direct upload |
| WAV    | `.wav`    | Direct upload |
| M4A    | `.m4a`    | Converted to MP3 via ffmpeg |
| AAC    | `.aac`    | Converted to MP3 via ffmpeg |
| FLAC   | `.flac`   | Converted to MP3 via ffmpeg |
| WMA    | `.wma`    | Converted to MP3 via ffmpeg |
| WebM   | `.webm`   | Converted to MP3 via ffmpeg |

Max file size: **500 MB** (configurable via `MAX_AUDIO_SIZE_MB`).

## Quick Start

### Prerequisites

- Google Cloud account with a project
- Firestore database enabled in the project
- Yandex Cloud account with:
  - API key or IAM token
  - Object Storage bucket
  - SpeechKit access

### 1. Clone the repository

```bash
git clone https://github.com/Orqen/Ears.git
cd Ears
```

### 2. Create Firestore database

```bash
gcloud firestore databases create --project YOUR_PROJECT_ID --location=eur3
```

### 3. Deploy to Cloud Run

```bash
gcloud run deploy ears \
  --project YOUR_PROJECT_ID \
  --source . \
  --region europe-west1 \
  --memory 2Gi \
  --cpu 2 \
  --timeout 900 \
  --min-instances 1 \
  --max-instances 5 \
  --allow-unauthenticated \
  --cpu-boost \
  --no-cpu-throttling \
  --set-env-vars "\
API_KEY=your-api-key,\
YANDEX_API_KEY=your-yandex-api-key,\
YANDEX_FOLDER_ID=your-folder-id,\
YC_S3_ACCESS_KEY=your-s3-access-key,\
YC_S3_SECRET_KEY=your-s3-secret-key,\
YC_S3_BUCKET=your-bucket-name,\
GOOGLE_CLOUD_PROJECT=your-project-id,\
MAX_AUDIO_SIZE_MB=500"
```

### 4. Test

```bash
# Health check
curl https://YOUR-URL/health

# Upload audio
curl -X POST https://YOUR-URL/transcribe \
  -H "X-API-Key: your-api-key" \
  -F "file=@recording.mp3"

# Check status
curl https://YOUR-URL/tasks/TASK_ID \
  -H "X-API-Key: your-api-key"

# Download result
curl https://YOUR-URL/tasks/TASK_ID/download \
  -H "X-API-Key: your-api-key" \
  -o result.txt
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `API_KEY` | Yes | API key for client authentication (`X-API-Key` header) |
| `YANDEX_API_KEY` | Yes* | Yandex Cloud API key |
| `YC_IAM_TOKEN` | Yes* | Yandex Cloud IAM token (alternative to API key) |
| `YANDEX_FOLDER_ID` | Yes | Yandex Cloud folder ID |
| `YC_S3_ACCESS_KEY` | Yes | Yandex Object Storage access key |
| `YC_S3_SECRET_KEY` | Yes | Yandex Object Storage secret key |
| `YC_S3_BUCKET` | Yes | S3 bucket name for audio files |
| `GOOGLE_CLOUD_PROJECT` | Yes | GCP project ID (for Firestore) |
| `MAX_AUDIO_SIZE_MB` | No | Max upload size in MB (default: 500) |
| `STT_LANGUAGE` | No | Recognition language (default: `ru-RU`) |
| `GCS_UPLOAD_BUCKET` | No | GCS bucket for uploads (default: `ears-uploads`) |
| `GCS_SIGNED_URL_TTL_MINUTES` | No | TTL for signed upload URLs (default: `30`) |
| `STT_PRICE_PER_SECOND` | No | STT price per second (used for `cost_estimate` only) |
| `STT_PRICE_PER_MINUTE` | No | STT price per minute (used for `cost_estimate` only) |
| `STT_BILLING_ROUNDING` | No | Billing rounding mode: `none`, `ceil_second`, `round`, `ceil_minute` (default: `ceil_minute`) |

\* Provide either `YANDEX_API_KEY` or `YC_IAM_TOKEN`.

---

## API Reference

All endpoints (except `/health`) require the `X-API-Key` header.

### `POST /transcribe`

Upload an audio file for transcription.

**Request:**

```
POST /transcribe
Content-Type: multipart/form-data
X-API-Key: <your-api-key>

file: <audio file>
```

**Response `202 Accepted`:**

```json
{
  "task_id": "8ae26a94-49f8-4039-9c08-c2b0e3ddfbb6",
  "status": "processing"
}
```

**Errors:**

| Code | Description |
|------|-------------|
| 400  | No file provided |
| 403  | Invalid API key |
| 413  | File exceeds size limit |

---

### `POST /transcribe-gcs`

Upload an audio file from Google Cloud Storage (`GCS`). The request accepts a `gs://...` URI and starts transcription asynchronously.

**Request:**

```
POST /transcribe-gcs
Content-Type: application/json
X-API-Key: <your-api-key>

{
  "gcs_uri": "gs://ears-uploads/abc123/recording.m4a",
  "lang": "ru"  // optional
}
```

`lang` overrides the `STT_LANGUAGE` for this task (use a Yandex STT-compatible language code, e.g. `ru-RU`).

**Response `202 Accepted`:**

```json
{
  "task_id": "8ae26a94-49f8-4039-9c08-c2b0e3ddfbb6",
  "status": "processing"
}
```

---

### `GET /upload-url`

Get a signed URL for uploading a file to `GCS` (`PUT`). The server returns both the signed `upload_url` and the resulting `gcs_uri`.

**Request:**

```
GET /upload-url?filename=recording.m4a&content_type=audio/mp4
X-API-Key: <your-api-key>
```

**Response `200 OK`:**

```json
{
  "upload_url": "https://storage.googleapis.com/ears-uploads/...",
  "gcs_uri": "gs://ears-uploads/abc123/recording.m4a"
}
```

---

### `GET /tasks/{task_id}`

Check transcription status. When status is `recognizing`, each call checks Yandex STT for updates.

**Request:**

```
GET /tasks/{task_id}
X-API-Key: <your-api-key>
```

**Response `200 OK`:**

```json
{
  "task_id": "8ae26a94-49f8-4039-9c08-c2b0e3ddfbb6",
  "status": "done",
  "text": "Transcribed text content...",
  "error": null,
  "created_at": "2026-03-24T08:41:21.082576+00:00"
}
```

**Task statuses:**

| Status | Description |
|--------|-------------|
| `processing` | Task created, queued for processing |
| `converting` | Audio is being converted to MP3 (ffmpeg) |
| `uploading` | File is uploading to Yandex Object Storage |
| `recognizing` | Yandex STT is processing the audio |
| `done` | Transcription complete, text available |
| `error` | Something went wrong, see `error` field |

**Errors:**

| Code | Description |
|------|-------------|
| 403  | Invalid API key |
| 404  | Task not found |

---

### `GET /tasks/{task_id}/cost`

Get a deterministic **estimated** STT cost for the task. The estimate is available only when the task status is `done`.

**Request:**

```
GET /tasks/{task_id}/cost
X-API-Key: <your-api-key>
```

**Response `200 OK`:**

```json
{
  "task_id": "8ae26a94-49f8-4039-9c08-c2b0e3ddfbb6",
  "cost_estimate": 0.123
}
```

**Errors:**

| Code | Description |
|------|-------------|
| 400  | Task not ready (not in `done` status) |
| 403  | Invalid API key |
| 404  | Task not found / cost estimate not available |

---

### `GET /tasks/{task_id}/download`

Download transcription result as a `.txt` file. Only available when status is `done`.

**Request:**

```
GET /tasks/{task_id}/download
X-API-Key: <your-api-key>
```

**Response `200 OK`:**

```
Content-Type: text/plain; charset=utf-8
Content-Disposition: attachment; filename="8ae26a94-49f8-4039-9c08-c2b0e3ddfbb6.txt"

Transcribed text content...
```

**Errors:**

| Code | Description |
|------|-------------|
| 400  | Task not ready (not in `done` status) |
| 403  | Invalid API key |
| 404  | Task not found |

---

### `GET /health`

Health check endpoint. No authentication required.

**Response `200 OK`:**

```json
{
  "status": "ok"
}
```

---

## Usage Example (Python)

```python
import requests
import time

BASE = "https://ears-XXXXX.europe-west1.run.app"
HEADERS = {"X-API-Key": "your-api-key"}

# 1. Upload
with open("recording.m4a", "rb") as f:
    resp = requests.post(f"{BASE}/transcribe", headers=HEADERS, files={"file": f})
task_id = resp.json()["task_id"]
print(f"Task: {task_id}")

# 2. Poll for result
while True:
    resp = requests.get(f"{BASE}/tasks/{task_id}", headers=HEADERS)
    data = resp.json()
    print(f"Status: {data['status']}")

    if data["status"] == "done":
        print(data["text"])
        break
    elif data["status"] == "error":
        print(f"Error: {data['error']}")
        break

    time.sleep(30)

# 3. Download as file
resp = requests.get(f"{BASE}/tasks/{task_id}/download", headers=HEADERS)
with open("result.txt", "wb") as f:
    f.write(resp.content)
```

## Project Structure

```
Ears/
├── app/
│   ├── __init__.py
│   ├── auth.py        # API key verification
│   ├── config.py      # Pydantic settings
│   ├── main.py        # FastAPI app & endpoints
│   ├── storage.py     # Yandex Object Storage (S3) client
│   ├── stt.py         # Yandex SpeechKit STT integration
│   └── taskstore.py   # Firestore task persistence
├── Dockerfile
├── requirements.txt
├── .env.example
└── README.md
```

## License

Private repository.
