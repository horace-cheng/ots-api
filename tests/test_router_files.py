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
    ):
        yield TestClient(app)


# ── POST /files/upload-url ────────────────────────────────────────────────────

class TestGetUploadUrl:
    def test_unsupported_content_type_returns_400(self, files_client):
        resp = files_client.post("/files/upload-url", json={
            "order_id": "order-001",
            "filename": "doc.exe",
            "content_type": "application/x-msdownload",
        })
        assert resp.status_code == 400
        assert "Unsupported content type" in resp.json()["detail"]

    def test_order_not_found_returns_404(self, files_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = None

        resp = files_client.post("/files/upload-url", json={
            "order_id": "nonexistent",
            "filename": "doc.docx",
            "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
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
        })
        assert resp.status_code == 400
        assert "pending_payment or paid" in resp.json()["detail"]

    def test_success_returns_signed_url(self, files_client, mock_db):
        row = MagicMock()
        row.status = "paid"
        mock_db.execute.return_value.fetchone.return_value = row

        resp = files_client.post("/files/upload-url", json={
            "order_id": "order-001",
            "filename": "doc.docx",
            "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
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
        })
        assert resp.status_code == 200


# ── POST /files/{order_id}/confirm ───────────────────────────────────────────

class TestConfirmUpload:
    def test_order_not_found_returns_404(self, files_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = None

        resp = files_client.post(
            "/files/order-001/confirm",
            params={"gcs_path": GCS_PATH},
        )
        assert resp.status_code == 404

    def test_success_returns_message(self, files_client, mock_db):
        row = MagicMock()
        mock_db.execute.return_value.fetchone.return_value = row

        resp = files_client.post(
            "/files/order-001/confirm",
            params={"gcs_path": GCS_PATH},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["message"] == "Upload confirmed"
        assert data["gcs_path"] == GCS_PATH


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
