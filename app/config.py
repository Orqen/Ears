from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # API auth
    api_key: str

    # Yandex Cloud auth
    yandex_api_key: str = ""
    yandex_folder_id: str = ""
    yc_iam_token: str = ""

    # Yandex Object Storage (for audio uploads)
    yc_s3_access_key: str
    yc_s3_secret_key: str
    yc_s3_bucket: str
    yc_s3_endpoint: str = "https://storage.yandexcloud.net"

    # Google Cloud
    google_cloud_project: str = ""

    # Limits
    max_audio_size_mb: int = 500

    # STT settings
    stt_language: str = "ru-RU"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
