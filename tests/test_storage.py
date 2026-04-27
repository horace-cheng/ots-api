import pytest
from unittest.mock import MagicMock, patch


BUCKET_NAME = "ots-outputs-bucket"
BARE_PATH   = "orders/abc123/output.docx"
FULL_URI    = f"gs://{BUCKET_NAME}/{BARE_PATH}"
SIGNED_URL  = "https://storage.googleapis.com/signed"


@pytest.fixture(autouse=True)
def patch_storage_deps(monkeypatch):
    """Stub out GCS client and signing credentials so no GCP calls are made."""
    from core.config import settings
    monkeypatch.setattr(settings, "gcs_outputs_bucket", BUCKET_NAME)
    monkeypatch.setattr(settings, "gcs_uploads_bucket", "ots-uploads-bucket")
    monkeypatch.setattr(settings, "project_id", "ots-translation")
    monkeypatch.setattr(settings, "service_account_email", "sa@ots.iam.gserviceaccount.com")

    import core.storage as storage_mod

    mock_blob = MagicMock()
    mock_blob.generate_signed_url.return_value = SIGNED_URL

    mock_bucket = MagicMock()
    mock_bucket.blob.return_value = mock_blob

    mock_client = MagicMock()
    mock_client.bucket.return_value = mock_bucket

    monkeypatch.setattr(storage_mod, "_client", mock_client)
    monkeypatch.setattr(storage_mod, "_signing_credentials", MagicMock())

    return mock_bucket, mock_blob


class TestGenerateDownloadSignedUrl:
    def test_bare_path_passed_through(self, patch_storage_deps):
        mock_bucket, mock_blob = patch_storage_deps
        from core.storage import generate_download_signed_url

        url = generate_download_signed_url(BARE_PATH)

        mock_bucket.blob.assert_called_once_with(BARE_PATH)
        assert url == SIGNED_URL

    def test_full_gs_uri_stripped(self, patch_storage_deps):
        mock_bucket, mock_blob = patch_storage_deps
        from core.storage import generate_download_signed_url

        url = generate_download_signed_url(FULL_URI)

        mock_bucket.blob.assert_called_once_with(BARE_PATH)
        assert url == SIGNED_URL

    def test_different_bucket_prefix_not_stripped(self, patch_storage_deps):
        mock_bucket, mock_blob = patch_storage_deps
        from core.storage import generate_download_signed_url

        other_uri = "gs://other-bucket/orders/abc123/output.docx"
        generate_download_signed_url(other_uri)

        # prefix doesn't match — full URI is passed as-is to blob()
        mock_bucket.blob.assert_called_once_with(other_uri)

    def test_returns_signed_url_string(self, patch_storage_deps):
        from core.storage import generate_download_signed_url

        result = generate_download_signed_url(BARE_PATH)
        assert isinstance(result, str)
        assert result == SIGNED_URL
