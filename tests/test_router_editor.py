import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.database import get_db
from routers.auth import get_editor_user, get_current_user, get_qa_user, get_reviewer_user
from routers.editor import router

MOCK_EDITOR_USER = {
    "uid": "editor-uid",
    "email": "editor@ots.tw",
    "user_id": "editor-db-id",
    "client_type": "b2c",
    "is_editor": True,
    "is_qa": False,
    "is_admin": False,
}

@pytest.fixture
def editor_client(mock_db):
    app = FastAPI()
    app.include_router(router)

    async def override_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: MOCK_EDITOR_USER
    app.dependency_overrides[get_editor_user] = lambda: MOCK_EDITOR_USER
    app.dependency_overrides[get_reviewer_user] = lambda: MOCK_EDITOR_USER

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
            "qa_id": None,
            "qa_submitted_at": None,
            "payment_status": "paid",
            "invoice_no": None
        }
        mock_db.execute.return_value.fetchall.return_value = [row]
        
        resp = editor_client.get("/editor/orders", headers={"Authorization": "Bearer dummy"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["orders"]) == 1
        assert data["orders"][0]["id"] == "order-001"


class TestEditorGetOrder:
    def test_get_order_success(self, editor_client, mock_db):
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
            "qa_id": None,
            "qa_submitted_at": None,
            "payment_status": "paid",
            "invoice_no": None
        }
        mock_db.execute.return_value.fetchone.return_value = row
        
        resp = editor_client.get("/editor/orders/order-001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "order-001"

    def test_get_order_not_found(self, editor_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = None
        resp = editor_client.get("/editor/orders/nonexistent")
        assert resp.status_code == 404

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
        
        resp = editor_client.get("/editor/orders/order-001/segments", headers={"Authorization": "Bearer dummy"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["segments"]) == 1
        assert data["segments"][0]["source"] == "Source"

    def test_get_segments_forbidden(self, editor_client, mock_db):
        # Mock DB for permission check failure
        mock_db.execute.return_value.fetchone.return_value = None
        
        resp = editor_client.get("/editor/orders/order-001/segments", headers={"Authorization": "Bearer dummy"})
        assert resp.status_code == 403

    @patch("core.storage.read_temp_json")
    @patch("core.storage.write_temp_json")
    def test_update_segments_success(self, mock_write, mock_read, editor_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = MagicMock()
        mock_read.return_value = [{"index": 0, "translated": "old", "editor_comments": None}]
        
        resp = editor_client.patch(
            "/editor/orders/order-001/segments",
            json={"segments": [{"index": 0, "translated": "new", "editor_comments": "edited"}]},
            headers={"Authorization": "Bearer dummy"}
        )
        assert resp.status_code == 200
        mock_write.assert_called_once()
        args = mock_write.call_args[0]
        assert args[2][0]["translated"] == "new"
        assert args[2][0]["editor_comments"] == "edited"

class TestEditorActions:
    def test_submit_success(self, editor_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = MagicMock()
        resp = editor_client.post("/editor/orders/order-001/submit", headers={"Authorization": "Bearer dummy"})
        assert resp.status_code == 200
        assert "delivered" in resp.json()["message"].lower()

    def test_submit_as_qa_only_moves_to_editor_verify(self, mock_db):
        """QA-only user submitting a qa_review order should transition to editor_verify, not delivered."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from core.database import get_db
        from routers.auth import get_reviewer_user
        from routers.editor import router

        QA_ONLY_USER = {
            "uid": "qa-uid",
            "email": "qa@ots.tw",
            "user_id": "qa-db-id",
            "client_type": "b2c",
            "is_qa": True,
            "is_editor": False,
            "is_admin": False,
        }

        app = FastAPI()
        app.include_router(router)

        async def override_db():
            yield mock_db

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_reviewer_user] = lambda: QA_ONLY_USER

        order = MagicMock()
        order.status = "qa_review"
        order.editor_id = "editor-db-id"
        order.qa_id = "qa-db-id"
        mock_db.execute.return_value.fetchone.return_value = order

        client = TestClient(app)
        resp = client.post("/editor/orders/order-001/submit")
        assert resp.status_code == 200
        assert "editor_verify" in resp.json()["message"].lower()

    def test_return_success(self, editor_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = MagicMock()
        resp = editor_client.post("/editor/orders/order-001/return", headers={"Authorization": "Bearer dummy"})
        assert resp.status_code == 200
        assert "returned to qa_review" in resp.json()["message"].lower()

    def test_return_access_denied_when_not_assigned(self, editor_client, mock_db):
        """Return is denied if the order isn't assigned to this editor or isn't in editor_verify."""
        mock_db.execute.return_value.fetchone.return_value = None
        resp = editor_client.post("/editor/orders/order-002/return", headers={"Authorization": "Bearer dummy"})
        assert resp.status_code == 403



class TestEditorTeam:
    def test_list_team_success(self, editor_client, mock_db):
        row = MagicMock()
        row._mapping = {
            "id": "qa-001",
            "uid_firebase": "qa-uid-001",
            "email": "qa@ots.tw",
            "client_type": "b2c",
            "disabled": False,
            "created_at": datetime.now(timezone.utc),
            "roles": ["qa"],
            "languages": [{"source_lang": "zh-tw", "target_lang": "en"}]
        }
        mock_db.execute.return_value.fetchall.return_value = [row]
        
        resp = editor_client.get("/editor/team", headers={"Authorization": "Bearer dummy"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["users"]) == 1
        assert data["users"][0]["is_qa"] is True

class TestEditorAssignQA:
    def test_assign_qa_success(self, editor_client, mock_db):
        # 1. Permission check
        # 2. QA role check
        mock_db.execute.return_value.fetchone.side_effect = [MagicMock(), MagicMock()]
        
        resp = editor_client.patch(
            "/editor/orders/order-001/assign-qa", 
            json={"qa_id": "qa-001"},
            headers={"Authorization": "Bearer dummy"}
        )
        assert resp.status_code == 200
        assert "qa assigned" in resp.json()["message"].lower()

    def test_assign_qa_not_found(self, editor_client, mock_db):
        # Permission check fails
        mock_db.execute.return_value.fetchone.return_value = None
        
        resp = editor_client.patch(
            "/editor/orders/order-001/assign-qa", 
            json={"qa_id": "qa-001"},
            headers={"Authorization": "Bearer dummy"}
        )
        assert resp.status_code == 403

    def test_assign_qa_invalid_role(self, editor_client, mock_db):
        # Permission check success, but QA role check fails
        mock_db.execute.return_value.fetchone.side_effect = [MagicMock(), None]
        
        resp = editor_client.patch(
            "/editor/orders/order-001/assign-qa", 
            json={"qa_id": "invalid-id"},
            headers={"Authorization": "Bearer dummy"}
        )
        assert resp.status_code == 400
        assert "not a qa" in resp.json()["detail"].lower()


class TestQaCannotAccessEditorOnlyEndpoints:
    """QA users should NOT be able to access editor-only endpoints."""

    def _qa_only_user(self):
        return {
            "uid": "qa-uid",
            "email": "qa@ots.tw",
            "user_id": "qa-db-id",
            "client_type": "b2c",
            "is_qa": True,
            "is_editor": False,
            "is_admin": False,
        }

    def _make_qa_app(self, mock_db):
        app = FastAPI()
        app.include_router(router)

        async def override_db():
            yield mock_db

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_reviewer_user] = lambda: self._qa_only_user()
        app.dependency_overrides[get_current_user] = lambda: self._qa_only_user()

        return TestClient(app)

    def test_qa_cannot_list_team(self, mock_db):
        """QA users cannot access /editor/team."""
        client = self._make_qa_app(mock_db)
        resp = client.get("/editor/team")
        assert resp.status_code == 403

    def test_qa_cannot_assign_qa_to_order(self, mock_db):
        """QA users cannot assign QA to orders."""
        client = self._make_qa_app(mock_db)
        resp = client.patch("/editor/orders/order-001/assign-qa", json={"qa_id": "qa-002"})
        assert resp.status_code == 403

    def test_qa_cannot_return_order(self, mock_db):
        """QA users cannot return orders to QA review."""
        client = self._make_qa_app(mock_db)
        resp = client.post("/editor/orders/order-001/return")
        assert resp.status_code == 403


class TestQaAccessSharedEndpoints:
    """QA users SHOULD be able to access shared endpoints."""

    def _qa_only_user(self):
        return {
            "uid": "qa-uid",
            "email": "qa@ots.tw",
            "user_id": "qa-db-id",
            "client_type": "b2c",
            "is_qa": True,
            "is_editor": False,
            "is_admin": False,
        }

    def _make_qa_app(self, mock_db):
        app = FastAPI()
        app.include_router(router)

        async def override_db():
            yield mock_db

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_reviewer_user] = lambda: self._qa_only_user()
        app.dependency_overrides[get_current_user] = lambda: self._qa_only_user()

        return TestClient(app)

    def test_qa_can_list_assigned_orders(self, mock_db):
        row = MagicMock()
        row._mapping = {
            "id": "order-001",
            "track_type": "fast",
            "status": "qa_review",
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
            "qa_id": "qa-db-id",
            "qa_submitted_at": None,
            "payment_status": "paid",
            "invoice_no": None
        }
        mock_db.execute.return_value.fetchall.return_value = [row]

        client = self._make_qa_app(mock_db)
        resp = client.get("/editor/orders")
        assert resp.status_code == 200
        assert len(resp.json()["orders"]) == 1

    def test_qa_cannot_see_editor_verify_orders(self, mock_db):
        """QA users should NOT see orders in editor_verify status."""
        # Mock count query returns 0 (no editor_verify orders visible to QA)
        mock_db.execute.return_value.scalar.return_value = 0
        mock_db.execute.return_value.fetchall.return_value = []

        client = self._make_qa_app(mock_db)
        resp = client.get("/editor/orders")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["orders"]) == 0
        assert data["total"] == 0

        # Verify the SQL query contains the qa_review-only filter for QA
        calls = mock_db.execute.call_args_list
        sql = str(calls[0][0][0]) if calls else ""
        assert "qa_review" in sql

    @patch("core.storage.read_temp_json")
    def test_qa_can_get_segments(self, mock_read, mock_db):
        mock_db.execute.return_value.fetchone.return_value = MagicMock()
        mock_read.side_effect = [
            [{"index": 0, "text": "Source"}],
            [{"index": 0, "translated": "Translated"}],
            [{"index": 0, "translated": "Raw"}]
        ]
        mock_db.execute.return_value.fetchall.return_value = []

        client = self._make_qa_app(mock_db)
        resp = client.get("/editor/orders/order-001/segments")
        assert resp.status_code == 200


class TestEditorListOrdersStatusFiltering:
    """Verify status-based filtering logic for different roles."""

    def test_editor_sees_qa_review_and_editor_verify(self, mock_db):
        """Editor users should see both qa_review and editor_verify orders."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from core.database import get_db
        from routers.auth import get_reviewer_user
        from routers.editor import router

        EDITOR_USER = {
            "uid": "editor-uid",
            "email": "editor@ots.tw",
            "user_id": "editor-db-id",
            "client_type": "b2c",
            "is_editor": True,
            "is_qa": False,
            "is_admin": False,
        }

        app = FastAPI()
        app.include_router(router)

        async def override_db():
            yield mock_db

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_reviewer_user] = lambda: EDITOR_USER

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
            "qa_id": "qa-db-id",
            "qa_submitted_at": None,
            "payment_status": "paid",
            "invoice_no": None
        }
        mock_db.execute.return_value.fetchall.return_value = [row]
        mock_db.execute.return_value.scalar.return_value = 1

        client = TestClient(app)
        resp = client.get("/editor/orders")
        assert resp.status_code == 200
        assert len(resp.json()["orders"]) == 1

        # Verify SQL includes both statuses for editor
        calls = mock_db.execute.call_args_list
        sql = str(calls[0][0][0]) if calls else ""
        assert "qa_review" in sql
        assert "editor_verify" in sql
