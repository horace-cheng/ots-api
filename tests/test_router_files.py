import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.database import get_db
from routers.auth import get_current_user
from routers.files import router
from tests.factories import MOCK_USER

SIGNED_URL = "https://storage.googleapis.com/signed-upload"
GCS_PATH   = "orders/order-001/doc.docx"


@pytest.fixture
def files_client(mock_db):
    app = FastAPI()
    app.include_router(router)

    async def override_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: MOCK_USER

    with (
        patch("routers.files.generate_upload_signed_url", return_value=(SIGNED_URL, GCS_PATH)),
        patch("routers.files.generate_download_signed_url", return_value=SIGNED_URL),
        patch("routers.files._check_file_size", return_value=1000),
        patch("routers.files._check_order_total_size"),
    ):
        yield TestClient(app)


# ── POST /files/upload-url ────────────────────────────────────────────────────

class TestGetUploadUrl:
    def test_unsupported_content_type_returns_400(self, files_client):
        resp = files_client.post("/files/upload-url", json={
            "order_id": "order-001",
            "filename": "doc.exe",
            "content_type": "application/x-msdownload",
            "file_size": 1000,
        })
        assert resp.status_code == 400
        assert "Unsupported content type" in resp.json()["detail"]

    def test_order_not_found_returns_404(self, files_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = None

        resp = files_client.post("/files/upload-url", json={
            "order_id": "nonexistent",
            "filename": "doc.docx",
            "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "file_size": 1000,
        })
        assert resp.status_code == 404

    def test_wrong_order_status_returns_400(self, files_client, mock_db):
        row = MagicMock()
        row.status = "processing"
        mock_db.execute.return_value.fetchone.return_value = row

        resp = files_client.post("/files/upload-url", json={
            "order_id": "order-001",
            "filename": "doc.docx",
            "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "file_size": 1000,
        })
        assert resp.status_code == 400
        assert "pending_payment" in resp.json()["detail"]

    def test_success_returns_signed_url(self, files_client, mock_db):
        row = MagicMock()
        row.status = "paid"
        mock_db.execute.return_value.fetchone.return_value = row

        resp = files_client.post("/files/upload-url", json={
            "order_id": "order-001",
            "filename": "doc.docx",
            "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "file_size": 1000,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["signed_url"] == SIGNED_URL
        assert data["gcs_path"] == GCS_PATH
        assert data["expires_in"] == 1800

    def test_pending_payment_status_allowed(self, files_client, mock_db):
        row = MagicMock()
        row.status = "pending_payment"
        mock_db.execute.return_value.fetchone.return_value = row

        resp = files_client.post("/files/upload-url", json={
            "order_id": "order-001",
            "filename": "source.txt",
            "content_type": "text/plain",
            "file_size": 1000,
        })
        assert resp.status_code == 200

    def test_awaiting_quote_status_allowed(self, files_client, mock_db):
        """LT orders are created with awaiting_quote status and should allow file upload."""
        row = MagicMock()
        row.status = "awaiting_quote"
        mock_db.execute.return_value.fetchone.return_value = row

        resp = files_client.post("/files/upload-url", json={
            "order_id": "order-001",
            "filename": "manuscript.docx",
            "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "file_size": 1000,
        })
        assert resp.status_code == 200

    def test_file_size_exceeds_limit_returns_400(self, files_client, mock_db):
        """Reject files larger than MAX_FILE_SIZE at signed URL generation."""
        row = MagicMock()
        row.status = "paid"
        mock_db.execute.return_value.fetchone.return_value = row

        resp = files_client.post("/files/upload-url", json={
            "order_id": "order-001",
            "filename": "huge.docx",
            "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "file_size": 3 * 1024 * 1024,
        })
        assert resp.status_code == 400
        assert "size limit" in resp.json()["detail"]


# ── POST /files/{order_id}/confirm ───────────────────────────────────────────

class TestConfirmUpload:
    def _row(self, title=None, track_type="fast", source_lang="zh-tw", target_lang="en"):
        row = MagicMock()
        row.title = title
        row.track_type = track_type
        row.source_lang = source_lang
        row.target_lang = target_lang
        return row

    def test_order_not_found_returns_404(self, files_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = None

        resp = files_client.post(
            "/files/order-001/confirm",
            params={"gcs_path": GCS_PATH},
        )
        assert resp.status_code == 404

    def test_existing_title_preserved(self, files_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = self._row(title="My Project")

        resp = files_client.post(
            "/files/order-001/confirm",
            params={"gcs_path": GCS_PATH},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["message"] == "Upload confirmed"
        assert data["gcs_path"] == GCS_PATH
        assert data["title"] == "My Project"

    def test_title_extracted_from_txt_content(self, files_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = self._row(title=None)

        with patch("routers.files._extract_title", return_value="Once upon a time in Taiwan"):
            resp = files_client.post(
                "/files/order-001/confirm",
                params={"gcs_path": "orders/order-001/source.txt"},
            )
        assert resp.status_code == 200
        assert resp.json()["title"] == "Once upon a time in Taiwan"

    def test_title_fallback_to_lang_label_for_binary(self, files_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = self._row(
            title=None, track_type="fast", source_lang="zh-tw", target_lang="en"
        )

        with patch("routers.files._extract_title", return_value=None):
            resp = files_client.post(
                "/files/order-001/confirm",
                params={"gcs_path": "orders/order-001/source.docx"},
            )
        assert resp.status_code == 200
        assert resp.json()["title"] == "繁體中文 → English 快速翻譯"

    def test_title_fallback_literary_track(self, files_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = self._row(
            title=None, track_type="literary", source_lang="tai-lo", target_lang="zh-tw"
        )

        with patch("routers.files._extract_title", return_value=None):
            resp = files_client.post(
                "/files/order-001/confirm",
                params={"gcs_path": "orders/order-001/source.pdf"},
            )
        assert resp.status_code == 200
        assert resp.json()["title"] == "台語 → 繁體中文 文學翻譯"


# ── _extract_title helper ─────────────────────────────────────────────────────

class TestExtractTitle:
    def _blob(self, content: bytes):
        blob = MagicMock()
        blob.download_as_bytes.return_value = content
        bucket = MagicMock()
        bucket.blob.return_value = blob
        client = MagicMock()
        client.bucket.return_value = bucket
        return client

    def test_txt_returns_first_ten_words(self, monkeypatch):
        from routers.files import _extract_title
        import routers.files as files_mod
        monkeypatch.setattr(files_mod, "get_storage_client", lambda: self._blob(
            b"The quick brown fox jumps over the lazy dog extra words here"
        ))
        result = _extract_title("orders/x/source.txt")
        assert result == "The quick brown fox jumps over the lazy dog extra"

    def test_non_text_file_returns_none(self, monkeypatch):
        from routers.files import _extract_title
        result = _extract_title("orders/x/source.docx")
        assert result is None

    def test_gcs_error_returns_none(self, monkeypatch):
        from routers.files import _extract_title
        import routers.files as files_mod
        def bad_client():
            raise Exception("GCS unavailable")
        monkeypatch.setattr(files_mod, "get_storage_client", bad_client)
        result = _extract_title("orders/x/source.txt")
        assert result is None

    def test_empty_file_returns_none(self, monkeypatch):
        from routers.files import _extract_title
        import routers.files as files_mod
        monkeypatch.setattr(files_mod, "get_storage_client", lambda: self._blob(b""))
        result = _extract_title("orders/x/source.txt")
        assert result is None


# ── GET /files/{order_id}/download-url ───────────────────────────────────────

class TestGetDownloadUrl:
    def test_order_not_found_returns_404(self, files_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = None

        resp = files_client.get("/files/order-001/download-url")
        assert resp.status_code == 404

    def test_not_delivered_returns_400(self, files_client, mock_db):
        row = MagicMock()
        row.status = "processing"
        row.gcs_output_path = "orders/order-001/output.docx"
        mock_db.execute.return_value.fetchone.return_value = row

        resp = files_client.get("/files/order-001/download-url")
        assert resp.status_code == 400
        assert "not yet delivered" in resp.json()["detail"]

    def test_missing_output_path_returns_404(self, files_client, mock_db):
        row = MagicMock()
        row.status = "delivered"
        row.gcs_output_path = None
        mock_db.execute.return_value.fetchone.return_value = row

        resp = files_client.get("/files/order-001/download-url")
        assert resp.status_code == 404
        assert "Output file not found" in resp.json()["detail"]

    def test_success_returns_signed_url(self, files_client, mock_db):
        row = MagicMock()
        row.status = "delivered"
        row.gcs_output_path = "orders/order-001/output.docx"
        mock_db.execute.return_value.fetchone.return_value = row

        resp = files_client.get("/files/order-001/download-url")
        assert resp.status_code == 200
        data = resp.json()
        assert data["signed_url"] == SIGNED_URL
        assert data["expires_in"] == 3600


# ── POST /files/{order_id}/support-confirm ──────────────────────────────────

class TestConfirmSupportUpload:
    def test_success_commits_and_returns_file(self, files_client, mock_db):
        order_row = MagicMock()
        file_row = MagicMock()
        file_row._mapping = {
            "id": "file-001",
            "order_id": "order-001",
            "filename": "glossary.pdf",
            "content_type": "application/pdf",
            "file_size": 12345,
            "gcs_path": "orders/order-001/support/glossary.pdf",
            "file_role": "glossary",
            "created_at": "2026-05-09T00:00:00Z",
        }
        mock_db.execute.return_value.fetchone.side_effect = [order_row, file_row]

        resp = files_client.post("/files/order-001/support-confirm", params={
            "filename": "glossary.pdf",
            "content_type": "application/pdf",
            "file_size": 12345,
            "gcs_path": "orders/order-001/support/glossary.pdf",
            "file_role": "glossary",
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["filename"] == "glossary.pdf"
        assert data["file_role"] == "glossary"
        mock_db.commit.assert_awaited_once()

    def test_order_not_found_returns_404(self, files_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = None

        resp = files_client.post("/files/order-001/support-confirm", params={
            "filename": "doc.txt",
            "content_type": "text/plain",
            "file_size": 100,
            "gcs_path": "orders/order-001/support/doc.txt",
            "file_role": "reference",
        })

        assert resp.status_code == 404
