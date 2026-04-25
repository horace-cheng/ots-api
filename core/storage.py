import google.auth
import google.auth.transport.requests
from google.auth import impersonated_credentials
from google.cloud import storage
from core.config import settings
from datetime import timedelta
import logging

logger = logging.getLogger(__name__)

_client: storage.Client | None = None
_signing_credentials = None


def get_storage_client() -> storage.Client:
    global _client
    if _client is None:
        _client = storage.Client(project=settings.project_id)
    return _client


def _get_signing_credentials():
    """
    Cloud Run 使用 Compute Engine credentials，沒有 private key 無法直接簽名。
    改用 IAM Credentials API 透過 impersonation 取得可簽名的 credentials。
    Service Account 需要有 roles/iam.serviceAccountTokenCreator 自我授權。
    """
    global _signing_credentials
    if _signing_credentials is not None:
        return _signing_credentials

    # 取得當前 ADC credentials
    source_credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )

    # 取得 Cloud Run Service Account email
    sa_email = settings.service_account_email

    # 使用 impersonated credentials 進行簽名
    _signing_credentials = impersonated_credentials.Credentials(
        source_credentials=source_credentials,
        target_principal=sa_email,
        target_scopes=["https://www.googleapis.com/auth/devstorage.read_write"],
        lifetime=3600,
    )
    return _signing_credentials


def generate_upload_signed_url(
    order_id: str,
    filename: str,
    content_type: str = "text/plain",
    expiration_minutes: int = 30,
) -> tuple[str, str]:
    """產生 GCS 上傳用 Signed URL（PUT method，有效 30 分鐘）"""
    client   = get_storage_client()
    bucket   = client.bucket(settings.gcs_uploads_bucket)
    gcs_path = f"orders/{order_id}/{filename}"
    blob     = bucket.blob(gcs_path)

    signed_url = blob.generate_signed_url(
        version             = "v4",
        expiration          = timedelta(minutes=expiration_minutes),
        method              = "PUT",
        content_type        = content_type,
        credentials         = _get_signing_credentials(),
    )
    return signed_url, gcs_path


def generate_download_signed_url(
    gcs_path: str,
    expiration_minutes: int = 60,
) -> str:
    """產生 GCS 下載用 Signed URL（GET method，有效 1 小時）"""
    client = get_storage_client()
    bucket = client.bucket(settings.gcs_outputs_bucket)
    blob   = bucket.blob(gcs_path)

    signed_url = blob.generate_signed_url(
        version     = "v4",
        expiration  = timedelta(minutes=expiration_minutes),
        method      = "GET",
        credentials = _get_signing_credentials(),
    )
    return signed_url