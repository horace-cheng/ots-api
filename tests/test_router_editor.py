import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.database import get_db
from routers.auth import get_editor_user
from routers.editor import router

MOCK_EDITOR_USER = {
    "uid": "editor-uid",
    "email": "editor@ots.tw",
    "user_id": "editor-db-id",
    "client_type": "b2c",
    "is_editor": True
}

@pytest.fixture
def editor_client(mock_db):
    app = FastAPI()
    app.include_router(router)

    async def override_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_editor_user] = lambda: MOCK_EDITOR_USER

    return TestClient(app)

class TestEditorListOrders:
    def test_list_assigned_orders_success(self, editor_client, mock_db):
        row = MagicMock()
        row._mapping = {
            "id": "order-001",
            "track_type": "fast",
            "status": "editor_verify",
            "source_lang": "zh-tw",
            "target_lang": "en",
            "word_count": 1000,
            "price_ntd": 2000,
            "title": "Title",
            "notes": None,
            "created_at": datetime.now(timezone.utc),
            "deadline_at": None,
            "delivered_at": None,
            "gcs_output_path": None,
            "editor_id": "editor-db-id",
            "payment_status": "paid",
            "invoice_no": None
        }
        mock_db.execute.return_value.fetchall.return_value = [row]
        
        resp = editor_client.get("/editor/orders")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["orders"]) == 1
        assert data["orders"][0]["id"] == "order-001"

class TestEditorSegments:
    @patch("core.storage.read_temp_json")
    def test_get_segments_success(self, mock_read, editor_client, mock_db):
        # Mock DB for permission check
        mock_db.execute.return_value.fetchone.return_value = MagicMock()
        
        # Mock GCS read
        mock_read.side_effect = [
            [{"index": 0, "text": "Source"}], # segments
            [{"index": 0, "translated": "Translated"}], # translations
            [{"index": 0, "translated": "Raw"}] # translations_raw
        ]
        
        # Mock DB for flags
        mock_db.execute.return_value.fetchall.return_value = []
        
        resp = editor_client.get("/editor/orders/order-001/segments")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["segments"]) == 1
        assert data["segments"][0]["source"] == "Source"

    def test_get_segments_forbidden(self, editor_client, mock_db):
        # Mock DB for permission check failure
        mock_db.execute.return_value.fetchone.return_value = None
        
        resp = editor_client.get("/editor/orders/order-001/segments")
        assert resp.status_code == 403

    @patch("core.storage.read_temp_json")
    @patch("core.storage.write_temp_json")
    def test_update_segments_success(self, mock_write, mock_read, editor_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = MagicMock()
        mock_read.return_value = [{"index": 0, "translated": "old", "editor_comments": None}]
        
        resp = editor_client.patch(
            "/editor/orders/order-001/segments",
            json={"segments": [{"index": 0, "translated": "new", "editor_comments": "edited"}]}
        )
        assert resp.status_code == 200
        mock_write.assert_called_once()
        args = mock_write.call_args[0]
        assert args[2][0]["translated"] == "new"
        assert args[2][0]["editor_comments"] == "edited"

class TestEditorActions:
    def test_submit_success(self, editor_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = MagicMock()
        resp = editor_client.post("/editor/orders/order-001/submit")
        assert resp.status_code == 200
        assert "delivered" in resp.json()["message"].lower()

    def test_return_success(self, editor_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = MagicMock()
        resp = editor_client.post("/editor/orders/order-001/return")
        assert resp.status_code == 200
        assert "returned to qa_review" in resp.json()["message"].lower()
