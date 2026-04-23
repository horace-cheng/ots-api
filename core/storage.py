from google.cloud import storage
from core.config import settings
from datetime import timedelta
import logging

logger = logging.getLogger(__name__)

_client: storage.Client | None = None


def get_storage_client() -> storage.Client:
    global _client
    if _client is None:
        _client = storage.Client(project=settings.project_id)
    return _client


def generate_upload_signed_url(
    order_id: str,
    filename: str,
    content_type: str = "text/plain",
    expiration_minutes: int = 30,
) -> tuple[str, str]:
    """
    產生 GCS 上傳用 Signed URL（PUT method）。
    回傳 (signed_url, gcs_path)。
    """
    client = get_storage_client()
    bucket = client.bucket(settings.gcs_uploads_bucket)
    gcs_path = f"orders/{order_id}/{filename}"
    blob = bucket.blob(gcs_path)

    signed_url = blob.generate_signed_url(
        version="v4",
        expiration=timedelta(minutes=expiration_minutes),
        method="PUT",
        content_type=content_type,
    )
    return signed_url, gcs_path


def generate_download_signed_url(
    gcs_path: str,
    expiration_minutes: int = 60,
) -> str:
    """
    產生 GCS 下載用 Signed URL（GET method）。
    """
    client = get_storage_client()
    bucket = client.bucket(settings.gcs_outputs_bucket)
    blob = bucket.blob(gcs_path)

    signed_url = blob.generate_signed_url(
        version="v4",
        expiration=timedelta(minutes=expiration_minutes),
        method="GET",
    )
    return signed_url
