import boto3
from botocore.config import Config as BotoConfig

from app.config import settings


def _get_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=settings.yc_s3_endpoint,
        aws_access_key_id=settings.yc_s3_access_key,
        aws_secret_access_key=settings.yc_s3_secret_key,
        region_name="ru-central1",
        config=BotoConfig(signature_version="s3v4"),
    )


def upload_file(task_id: str, filename: str, file_data: bytes) -> str:
    """Upload audio file to Yandex Object Storage.

    Returns the S3 URI (s3://bucket/key) for use with Yandex STT.
    """
    s3 = _get_s3_client()
    key = f"audio/{task_id}/{filename}"
    s3.put_object(
        Bucket=settings.yc_s3_bucket,
        Key=key,
        Body=file_data,
    )
    return f"https://storage.yandexcloud.net/{settings.yc_s3_bucket}/{key}"
