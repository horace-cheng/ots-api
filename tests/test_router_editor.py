import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.database import get_db
from routers.auth import get_editor_user, get_current_user, get_lt_user, get_qa_user, get_reviewer_user
from routers.editor import router
from services.lt_segment_retranslate import RetranslateResult

MOCK_EDITOR_USER = {
    "uid": "editor-uid",
    "email": "editor@ots.tw",
    "user_id": "editor-db-id",
    "client_type": "b2c",
    "is_editor": True,
    "is_qa": False,
    "is_admin": False,
}

MOCK_LT_USER = {
    "uid": "lt-user-uid",
    "email": "lt-user@ots.tw",
    "user_id": "lt-db-id",
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
            "payment_status": "paid",
            "invoice_no": None
        }
        mock_db.execute.return_value.scalar.return_value = 1
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
            "payment_status": "paid",
            "invoice_no": None
        }
        mock_db.execute.return_value.scalar.return_value = 1
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


def _make_lt_app(mock_db):
    """Helper to create a TestClient with LT user overrides."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.database import get_db
    from routers.auth import get_lt_user
    from routers.editor import router

    app = FastAPI()
    app.include_router(router)

    async def override_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_lt_user] = lambda: MOCK_LT_USER
    return TestClient(app)


class TestLtCompleteAssignment:
    """Tests for POST /editor/lt/orders/{order_id}/complete with version auto-save hooks."""

    @patch("core.storage.read_temp_json")
    def test_editor_complete_success(self, mock_read, mock_db):
        assignment = MagicMock()
        assignment.status = "editing"
        mock_db.execute.return_value.fetchone.return_value = assignment
        mock_read.return_value = [{"index": 0, "translated": "Hello", "editor_comments": "checked"}]

        with patch("routers.editor.svc_save_version", new_callable=AsyncMock) as mock_save:
            client = _make_lt_app(mock_db)
            resp = client.post("/editor/lt/orders/order-001/complete?role=editor")

        assert resp.status_code == 200
        assert "Assignment completed" in resp.json()["message"]
        mock_save.assert_awaited_once_with(mock_db, "order-001", source="editor", created_by="lt-db-id")

    @patch("core.storage.read_temp_json")
    def test_editor_revision_needed(self, mock_read, mock_db):
        assignment = MagicMock()
        assignment.status = "revision_needed"
        mock_db.execute.return_value.fetchone.return_value = assignment

        with patch("routers.editor.svc_save_version", new_callable=AsyncMock) as mock_save:
            client = _make_lt_app(mock_db)
            resp = client.post("/editor/lt/orders/order-001/complete?role=editor")

        assert resp.status_code == 200
        assert "proofreader" in resp.json()["message"].lower()
        mock_save.assert_not_called()

    def test_editor_already_done(self, mock_db):
        assignment = MagicMock()
        assignment.status = "editor_done"
        mock_db.execute.return_value.fetchone.return_value = assignment

        with patch("routers.editor.svc_save_version", new_callable=AsyncMock) as mock_save:
            client = _make_lt_app(mock_db)
            resp = client.post("/editor/lt/orders/order-001/complete?role=editor")

        assert resp.status_code == 200
        assert "already completed" in resp.json()["message"].lower()
        mock_save.assert_not_called()

    def test_editor_access_denied(self, mock_db):
        mock_db.execute.return_value.fetchone.return_value = None
        client = _make_lt_app(mock_db)
        resp = client.post("/editor/lt/orders/order-001/complete?role=editor")
        assert resp.status_code == 403

    @patch("core.storage.read_temp_json")
    def test_editor_no_translations(self, mock_read, mock_db):
        assignment = MagicMock()
        assignment.status = "editing"
        mock_db.execute.return_value.fetchone.return_value = assignment
        mock_read.return_value = None

        client = _make_lt_app(mock_db)
        resp = client.post("/editor/lt/orders/order-001/complete?role=editor")
        assert resp.status_code == 404

    @patch("core.storage.read_temp_json")
    def test_editor_unresolved_qa_flags(self, mock_read, mock_db):
        assignment = MagicMock()
        assignment.status = "editing"
        mock_db.execute.return_value.fetchone.return_value = assignment
        mock_read.return_value = [{"index": 0, "translated": "Hello", "editor_comments": ""}]

        must_fix = MagicMock()
        must_fix.paragraph_index = 0
        must_fix.id = "flag-001"
        qa_res = MagicMock()
        qa_res.fetchall.return_value = [must_fix]
        mock_db.execute.return_value.fetchall.return_value = [must_fix]

        client = _make_lt_app(mock_db)
        resp = client.post("/editor/lt/orders/order-001/complete?role=editor")
        assert resp.status_code == 400
        assert "QA flags" in resp.json()["detail"]

    @patch("core.storage.read_temp_json")
    def test_proofreader_complete_success(self, mock_read, mock_db):
        assignment = MagicMock()
        assignment.status = "proofreading"
        mock_db.execute.return_value.fetchone.return_value = assignment

        with patch("routers.editor.svc_save_version", new_callable=AsyncMock) as mock_save:
            client = _make_lt_app(mock_db)
            resp = client.post("/editor/lt/orders/order-001/complete?role=proofreader")

        assert resp.status_code == 200
        assert "Assignment completed" in resp.json()["message"]
        mock_save.assert_awaited_once_with(mock_db, "order-001", source="proofreader", created_by="lt-db-id")

    def test_proofreader_wrong_status(self, mock_db):
        assignment = MagicMock()
        assignment.status = "editing"
        mock_db.execute.return_value.fetchone.return_value = assignment

        client = _make_lt_app(mock_db)
        resp = client.post("/editor/lt/orders/order-001/complete?role=proofreader")
        assert resp.status_code == 400
        assert "proofreading" in resp.json()["detail"]


class TestLtVersions:
    """Tests for LT read-only version history endpoints."""

    def test_list_versions_success(self, mock_db):
        row = MagicMock()
        row.id = "ver-001"
        row.order_id = "order-001"
        row.version = 1
        row.label = None
        row.source = "nmt"
        row.created_by = None
        row.created_at = datetime.now(timezone.utc)
        row.segment_count = 10
        mock_db.execute.return_value.fetchall.return_value = [row]

        client = _make_lt_app(mock_db)
        resp = client.get("/editor/lt/orders/order-001/versions")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    def test_list_versions_empty(self, mock_db):
        mock_db.execute.return_value.fetchall.return_value = []
        client = _make_lt_app(mock_db)
        resp = client.get("/editor/lt/orders/order-001/versions")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_diff_versions_success(self, mock_db):
        diff_result = {"changed": [], "added": [], "removed": []}
        with patch("routers.editor.svc_diff_versions", new_callable=AsyncMock) as mock_diff:
            mock_diff.return_value = diff_result
            client = _make_lt_app(mock_db)
            resp = client.get(
                "/editor/lt/orders/order-001/versions/11111111-1111-1111-1111-111111111001/diff",
                params={"against": "11111111-1111-1111-1111-111111111002"},
            )
        assert resp.status_code == 200
        assert "changed" in resp.json()

    def test_diff_versions_auto_latest(self, mock_db):
        row = MagicMock()
        row.id = "11111111-1111-1111-1111-111111111002"
        mock_db.execute.return_value.fetchone.return_value = row

        diff_result = {"changed": [], "added": [], "removed": []}
        with patch("routers.editor.svc_diff_versions", new_callable=AsyncMock) as mock_diff:
            mock_diff.return_value = diff_result
            client = _make_lt_app(mock_db)
            resp = client.get(
                "/editor/lt/orders/order-001/versions/11111111-1111-1111-1111-111111111001/diff",
            )
        assert resp.status_code == 200

    def test_diff_versions_no_other_version(self, mock_db):
        mock_db.execute.return_value.fetchone.return_value = None
        client = _make_lt_app(mock_db)
        resp = client.get(
            "/editor/lt/orders/order-001/versions/11111111-1111-1111-1111-111111111001/diff",
        )
        assert resp.status_code == 404
        assert "No other version" in resp.json()["detail"]

    @patch("core.storage.read_temp_json")
    def test_diff_live_not_found(self, mock_read, mock_db):
        mock_read.return_value = None
        client = _make_lt_app(mock_db)
        resp = client.get(
            "/editor/lt/orders/order-001/versions/live/diff",
            params={"against": "11111111-1111-1111-1111-111111111001"},
        )
        assert resp.status_code == 404


class TestUpdateLtOrderSegments:
    """Tests for PATCH /editor/lt/orders/{order_id}/segments (draft save).

    Draft saves must NOT require comments on flagged segments — that check is
    enforced only when the editor completes the assignment (POST /complete).
    """

    @patch("core.storage.write_temp_json")
    @patch("core.storage.read_temp_json")
    def test_draft_save_allows_flagged_segment_without_comment(self, mock_read, mock_write, mock_db):
        mock_db.execute.return_value.fetchone.return_value = MagicMock()
        mock_read.return_value = [{"index": 1, "translated": "old", "editor_comments": ""}]
        client = _make_lt_app(mock_db)
        resp = client.patch(
            "/editor/lt/orders/order-001/segments?role=editor",
            json={"segments": [{"index": 1, "translated": "new", "editor_comments": ""}]},
        )
        assert resp.status_code == 200
        written = mock_write.call_args.args[2]
        assert written[0]["translated"] == "new"
        assert written[0]["editor_comments"] == ""

    @patch("core.storage.write_temp_json")
    @patch("core.storage.read_temp_json")
    def test_draft_save_access_denied(self, mock_read, mock_write, mock_db):
        mock_db.execute.return_value.fetchone.return_value = None
        client = _make_lt_app(mock_db)
        resp = client.patch(
            "/editor/lt/orders/order-001/segments?role=editor",
            json={"segments": [{"index": 1, "translated": "new"}]},
        )
        assert resp.status_code == 403
        mock_write.assert_not_called()


class TestRetranslateLtSegment:
    """Tests for POST /editor/lt/orders/{order_id}/segments/{index}/retranslate."""

    @staticmethod
    def _execute_handler(query, *args, **kwargs):
        from types import SimpleNamespace
        q = str(query)
        if "source_lang" in q:
            row = SimpleNamespace(source_lang="zh-tw", target_lang="en")
            res = MagicMock()
            res.fetchone.return_value = row
            return res
        if "UPDATE qa_flags" in q:
            res = MagicMock()
            res.fetchall.return_value = [1, 2]
            return res
        # assignment verification -> allowed
        res = MagicMock()
        res.fetchone.return_value = MagicMock()
        return res

    @patch("services.lt_segment_retranslate.retranslate_segment",
           return_value=RetranslateResult(translated="He stood on the hill."))
    def test_success(self, mock_svc, mock_db):
        mock_db.execute.side_effect = self._execute_handler
        client = _make_lt_app(mock_db)
        resp = client.post("/editor/lt/orders/order-001/segments/1/retranslate?role=editor")
        assert resp.status_code == 200
        body = resp.json()
        assert body["index"] == 1
        assert body["translated"] == "He stood on the hill."
        assert body["flags_resolved"] == 2
        assert body["used_fallback"] is False

    @patch("services.lt_segment_retranslate.retranslate_segment",
           return_value=RetranslateResult(translated="He stood on the hill.", used_fallback=True))
    def test_fallback_flag_reported(self, mock_svc, mock_db):
        mock_db.execute.side_effect = self._execute_handler
        client = _make_lt_app(mock_db)
        resp = client.post("/editor/lt/orders/order-001/segments/1/retranslate?role=editor")
        assert resp.status_code == 200
        assert resp.json()["used_fallback"] is True

    @patch("services.lt_segment_retranslate.retranslate_segment",
           return_value=RetranslateResult(translated="He stood on the hill.", gemini_usage=[
               {"model": "gemini-3.5-flash", "prompt_tokens": 1000, "candidates_tokens": 500,
                "total_tokens": 1500, "cost_usd": 0.002, "input_rate": 0.50, "output_rate": 3.00},
           ]))
    def test_token_usage_recorded(self, mock_svc, mock_db):
        inserted = []
        def handler(query, *args, **kwargs):
            from types import SimpleNamespace
            q = str(query)
            if "source_lang" in q:
                row = SimpleNamespace(source_lang="zh-tw", target_lang="en")
                res = MagicMock()
                res.fetchone.return_value = row
                return res
            if "INSERT INTO token_usage" in q:
                inserted.append(args[0] if args else kwargs.get("params"))
                return MagicMock()
            if "UPDATE qa_flags" in q:
                res = MagicMock()
                res.fetchall.return_value = [1, 2]
                return res
            res = MagicMock()
            res.fetchone.return_value = MagicMock()
            return res
        mock_db.execute.side_effect = handler
        client = _make_lt_app(mock_db)
        resp = client.post("/editor/lt/orders/order-001/segments/1/retranslate?role=editor")
        assert resp.status_code == 200
        assert len(inserted) == 1
        params = inserted[0]
        assert params["order_id"] == "order-001"
        assert params["job_type"] == "lt_retranslate"
        assert params["model"] == "gemini-3.5-flash"
        assert params["prompt_tokens"] == 1000
        assert params["cost_usd"] == pytest.approx(0.002)

    def test_access_denied(self, mock_db):
        mock_db.execute.return_value.fetchone.return_value = None
        client = _make_lt_app(mock_db)
        resp = client.post("/editor/lt/orders/order-001/segments/1/retranslate?role=editor")
        assert resp.status_code == 403

    def test_index_out_of_range(self, mock_db):
        def handler(query, *args, **kwargs):
            from types import SimpleNamespace
            q = str(query)
            if "source_lang" in q:
                row = SimpleNamespace(source_lang="zh-tw", target_lang="en")
                res = MagicMock()
                res.fetchone.return_value = row
                return res
            res = MagicMock()
            res.fetchone.return_value = MagicMock()
            return res

        mock_db.execute.side_effect = handler
        with patch("services.lt_segment_retranslate.retranslate_segment",
                   side_effect=ValueError("Segment index 99 not found")):
            client = _make_lt_app(mock_db)
            resp = client.post("/editor/lt/orders/order-001/segments/99/retranslate?role=editor")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_gemini_failure(self, mock_db):
        from services.lt_segment_retranslate import SegmentRetranslateError

        def handler(query, *args, **kwargs):
            from types import SimpleNamespace
            q = str(query)
            if "source_lang" in q:
                row = SimpleNamespace(source_lang="zh-tw", target_lang="en")
                res = MagicMock()
                res.fetchone.return_value = row
                return res
            res = MagicMock()
            res.fetchone.return_value = MagicMock()
            return res

        mock_db.execute.side_effect = handler
        with patch("services.lt_segment_retranslate.retranslate_segment",
                   side_effect=SegmentRetranslateError("empty response")):
            client = _make_lt_app(mock_db)
            resp = client.post("/editor/lt/orders/order-001/segments/1/retranslate?role=editor")
        assert resp.status_code == 502

    def test_content_blocked_422(self, mock_db):
        from services.lt_segment_retranslate import SegmentContentBlocked

        def handler(query, *args, **kwargs):
            from types import SimpleNamespace
            q = str(query)
            if "source_lang" in q:
                row = SimpleNamespace(source_lang="zh-tw", target_lang="en")
                res = MagicMock()
                res.fetchone.return_value = row
                return res
            res = MagicMock()
            res.fetchone.return_value = MagicMock()
            return res

        mock_db.execute.side_effect = handler
        with patch("services.lt_segment_retranslate.retranslate_segment",
                   side_effect=SegmentContentBlocked("content blocked by Gemini safety policy")):
            client = _make_lt_app(mock_db)
            resp = client.post("/editor/lt/orders/order-001/segments/1/retranslate?role=editor")
        assert resp.status_code == 422
        assert "safety" in resp.json()["detail"].lower()


class TestUpdateLtSegmentSource:
    """Tests for PATCH /editor/lt/orders/{order_id}/segments/{index}/source."""

    SEGMENTS = [
        {"index": 0, "text": "Old source", "char_count": 10},
        {"index": 1, "text": "Keep me", "char_count": 7},
    ]
    TRANSLATIONS = [
        {"index": 0, "translated": "Old trans", "source": "Old source"},
        {"index": 1, "translated": "Keep trans", "source": "Keep me"},
    ]
    OVERRIDES = {"0": {"original": "Old source", "edited": "Old source"}}

    @staticmethod
    def _reads():
        from types import SimpleNamespace
        res = MagicMock()
        res.fetchone.return_value = SimpleNamespace(status="editing")
        return res

    def test_editor_edits_source_updates_all_files(self, mock_db):
        mock_db.execute.return_value = self._reads()
        with patch("core.storage.read_temp_json", side_effect=[
            list(self.SEGMENTS), list(self.TRANSLATIONS), dict(self.OVERRIDES),
        ]) as mock_read, \
             patch("core.storage.write_temp_json") as mock_write:
            client = _make_lt_app(mock_db)
            resp = client.patch(
                "/editor/lt/orders/order-001/segments/0/source?role=editor",
                json={"source": "  New source  "},
            )

        assert resp.status_code == 200
        assert resp.json() == {"index": 0, "source": "New source", "source_edited": True}

        written_segments = mock_write.call_args_list[0].args[2]
        written_trans = mock_write.call_args_list[1].args[2]
        written_overrides = mock_write.call_args_list[2].args[2]
        # segments.json canonical source updated
        assert written_segments[0]["text"] == "New source"
        assert written_segments[0]["char_count"] == len("New source")
        assert written_segments[1]["text"] == "Keep me"
        # translations.json source synced + stale flag set
        assert written_trans[0]["source"] == "New source"
        assert written_trans[0]["source_edited"] is True
        assert written_trans[1]["source"] == "Keep me"
        # audit trail keeps the original for revert
        assert written_overrides["0"]["original"] == "Old source"
        assert written_overrides["0"]["edited"] == "New source"
        assert written_overrides["0"]["edited_by"] == "lt-db-id"

    def test_editor_edit_reuses_existing_original(self, mock_db):
        """A second edit preserves the original source in the audit trail."""
        mock_db.execute.return_value = self._reads()
        with patch("core.storage.read_temp_json", side_effect=[
            list(self.SEGMENTS), list(self.TRANSLATIONS),
            {"0": {"original": "Old source", "edited": "First fix"}},
        ]) as mock_read, \
             patch("core.storage.write_temp_json") as mock_write:
            client = _make_lt_app(mock_db)
            resp = client.patch(
                "/editor/lt/orders/order-001/segments/0/source?role=editor",
                json={"source": "Second fix"},
            )

        assert resp.status_code == 200
        written_overrides = mock_write.call_args_list[2].args[2]
        assert written_overrides["0"]["original"] == "Old source"
        assert written_overrides["0"]["edited"] == "Second fix"

    def test_admin_allowed(self, mock_db):
        from types import SimpleNamespace
        admin_user = dict(MOCK_LT_USER, is_admin=True)
        mock_db.execute.return_value = self._reads()
        with patch("core.storage.read_temp_json", side_effect=[
            list(self.SEGMENTS), list(self.TRANSLATIONS), dict(self.OVERRIDES),
        ]), \
             patch("core.storage.write_temp_json") as mock_write:
            app = FastAPI()
            app.include_router(router)
            async def override_db():
                yield mock_db
            from core.database import get_db
            from routers.auth import get_lt_user
            app.dependency_overrides[get_db] = override_db
            app.dependency_overrides[get_lt_user] = lambda: admin_user
            client = TestClient(app)
            resp = client.patch(
                "/editor/lt/orders/order-001/segments/0/source?role=editor",
                json={"source": "Admin edit"},
            )
        assert resp.status_code == 200

    def test_proofreader_role_denied(self, mock_db):
        mock_db.execute.return_value = self._reads()
        with patch("core.storage.read_temp_json", side_effect=[
            list(self.SEGMENTS), list(self.TRANSLATIONS), dict(self.OVERRIDES),
        ]), \
             patch("core.storage.write_temp_json") as mock_write:
            client = _make_lt_app(mock_db)
            resp = client.patch(
                "/editor/lt/orders/order-001/segments/0/source?role=proofreader",
                json={"source": "New source"},
            )
        assert resp.status_code == 403
        mock_write.assert_not_called()

    def test_access_denied(self, mock_db):
        mock_db.execute.return_value.fetchone.return_value = None
        with patch("core.storage.read_temp_json", side_effect=[
            list(self.SEGMENTS), list(self.TRANSLATIONS), dict(self.OVERRIDES),
        ]), \
             patch("core.storage.write_temp_json") as mock_write:
            client = _make_lt_app(mock_db)
            resp = client.patch(
                "/editor/lt/orders/order-001/segments/0/source?role=editor",
                json={"source": "New source"},
            )
        assert resp.status_code == 403
        mock_write.assert_not_called()

    def test_index_out_of_range(self, mock_db):
        mock_db.execute.return_value = self._reads()
        with patch("core.storage.read_temp_json", side_effect=[
            list(self.SEGMENTS), list(self.TRANSLATIONS), dict(self.OVERRIDES),
        ]), \
             patch("core.storage.write_temp_json") as mock_write:
            client = _make_lt_app(mock_db)
            resp = client.patch(
                "/editor/lt/orders/order-001/segments/99/source?role=editor",
                json={"source": "New source"},
            )
        assert resp.status_code == 404
        mock_write.assert_not_called()

    def test_empty_source_allowed(self, mock_db):
        """Empty source is allowed so editors can merge segments (clearing one)."""
        mock_db.execute.return_value = self._reads()
        with patch("core.storage.read_temp_json", side_effect=[
            list(self.SEGMENTS), list(self.TRANSLATIONS), dict(self.OVERRIDES),
        ]), \
             patch("core.storage.write_temp_json") as mock_write:
            client = _make_lt_app(mock_db)
            resp = client.patch(
                "/editor/lt/orders/order-001/segments/0/source?role=editor",
                json={"source": ""},
            )
        assert resp.status_code == 200
        assert resp.json() == {"index": 0, "source": "", "source_edited": True}
        written_segments = mock_write.call_args_list[0].args[2]
        written_trans = mock_write.call_args_list[1].args[2]
        written_overrides = mock_write.call_args_list[2].args[2]
        assert written_segments[0]["text"] == ""
        assert written_trans[0]["source"] == ""
        assert written_trans[0]["source_edited"] is True
        assert written_overrides["0"]["original"] == "Old source"
        assert written_overrides["0"]["edited"] == ""

    def test_missing_translation_entry(self, mock_db):
        mock_db.execute.return_value = self._reads()
        with patch("core.storage.read_temp_json", side_effect=[
            list(self.SEGMENTS), [{"index": 2, "translated": "orphan"}], dict(self.OVERRIDES),
        ]), \
             patch("core.storage.write_temp_json") as mock_write:
            client = _make_lt_app(mock_db)
            resp = client.patch(
                "/editor/lt/orders/order-001/segments/0/source?role=editor",
                json={"source": "New source"},
            )
        assert resp.status_code == 404
        mock_write.assert_not_called()

    def test_get_segments_exposes_source_edited(self, mock_db):
        """GET segments surfaces the source_edited flag for badge display."""
        mock_db.execute.return_value.fetchone.return_value = MagicMock()
        mock_db.execute.return_value.fetchall.return_value = []
        with patch("core.storage.read_temp_json", side_effect=[
            list(self.SEGMENTS),
            [dict(self.TRANSLATIONS[0], source_edited=True), dict(self.TRANSLATIONS[1])],
            [{"index": 0, "translated": "raw"}],
        ]):
            client = _make_lt_app(mock_db)
            resp = client.get("/editor/lt/orders/order-001/segments?role=editor&limit=100")

        assert resp.status_code == 200
        segs = {s["index"]: s for s in resp.json()["segments"]}
        assert segs[0]["source_edited"] is True
        assert segs[1]["source_edited"] is False


class TestUpdateLtSegmentsClearsSourceEdited:
    """Saving a manual translation resolves the stale-translation state."""

    @patch("core.storage.write_temp_json")
    @patch("core.storage.read_temp_json")
    def test_manual_save_clears_source_edited(self, mock_read, mock_write, mock_db):
        mock_db.execute.return_value.fetchone.return_value = MagicMock()
        mock_read.return_value = [{"index": 1, "translated": "old", "source_edited": True}]
        client = _make_lt_app(mock_db)
        resp = client.patch(
            "/editor/lt/orders/order-001/segments?role=editor",
            json={"segments": [{"index": 1, "translated": "new"}]},
        )
        assert resp.status_code == 200
        written = mock_write.call_args.args[2]
        assert written[0]["translated"] == "new"
        assert written[0]["source_edited"] is False


class TestGetLtSegmentsChapters:
    """GET /editor/lt/orders/{order_id}/segments derives chapters from marks."""

    SEGMENTS = [{"index": i, "text": f"S{i}"} for i in range(10)]
    TRANSLATIONS = [
        {"index": i, "translated": f"T{i}"}
        for i in range(10)
    ]

    @staticmethod
    def _client(mock_db):
        mock_db.execute.return_value.fetchone.return_value = MagicMock()
        mock_db.execute.return_value.fetchall.return_value = []
        return _make_lt_app(mock_db)

    @staticmethod
    def _translations_with_marks(*marked_indices):
        return [
            dict(t, is_chapter_title=(i in marked_indices))
            for i, t in enumerate(TestGetLtSegmentsChapters.TRANSLATIONS)
        ]

    def test_chapters_derived_from_marks(self, mock_db):
        translations = self._translations_with_marks(3, 7)
        with patch("core.storage.read_temp_json", side_effect=[
            list(self.SEGMENTS), translations, None,
        ]):
            resp = self._client(mock_db).get(
                "/editor/lt/orders/order-001/segments?role=editor&limit=1000")

        assert resp.status_code == 200
        data = resp.json()
        chapters = data["chapters"]
        assert len(chapters) == 3

        # Leading untitled group: seg#1-2 (indices 0..2)
        assert chapters[0]["chapter_index"] == 0
        assert chapters[0]["start_index"] == 0
        assert chapters[0]["end_index"] == 2
        assert chapters[0]["segment_count"] == 3
        assert chapters[0]["title_segment_index"] is None

        # Title marked at index 3 → chapter seg#4-7 (indices 3..6)
        assert chapters[1]["chapter_index"] == 1
        assert chapters[1]["start_index"] == 3
        assert chapters[1]["end_index"] == 6
        assert chapters[1]["segment_count"] == 4
        assert chapters[1]["title_segment_index"] == 3
        assert chapters[1]["title_source"] == "S3"
        assert chapters[1]["title_translated"] == "T3"

        # Title marked at index 7 → chapter seg#8-10 (indices 7..9)
        assert chapters[2]["chapter_index"] == 2
        assert chapters[2]["start_index"] == 7
        assert chapters[2]["end_index"] == 9
        assert chapters[2]["segment_count"] == 3
        assert chapters[2]["title_segment_index"] == 7

        # is_chapter_title echoed per segment
        segs = {s["index"]: s for s in data["segments"]}
        assert segs[3]["is_chapter_title"] is True
        assert segs[7]["is_chapter_title"] is True
        assert segs[0]["is_chapter_title"] is False

    def test_no_marks_single_untitled_chapter(self, mock_db):
        with patch("core.storage.read_temp_json", side_effect=[
            list(self.SEGMENTS), list(self.TRANSLATIONS), None,
        ]):
            resp = self._client(mock_db).get(
                "/editor/lt/orders/order-001/segments?role=editor")

        assert resp.status_code == 200
        chapters = resp.json()["chapters"]
        assert len(chapters) == 1
        assert chapters[0]["start_index"] == 0
        assert chapters[0]["end_index"] == 9
        assert chapters[0]["segment_count"] == 10
        assert chapters[0]["title_segment_index"] is None

    def test_limit_1000_accepted(self, mock_db):
        with patch("core.storage.read_temp_json", side_effect=[
            list(self.SEGMENTS), list(self.TRANSLATIONS), None,
        ]):
            resp = self._client(mock_db).get(
                "/editor/lt/orders/order-001/segments?role=editor&limit=1000")

        assert resp.status_code == 200
        assert len(resp.json()["segments"]) == 10

    def test_limit_over_1000_rejected(self, mock_db):
        with patch("core.storage.read_temp_json", side_effect=[
            list(self.SEGMENTS), list(self.TRANSLATIONS), None,
        ]):
            resp = self._client(mock_db).get(
                "/editor/lt/orders/order-001/segments?role=editor&limit=1001")

        assert resp.status_code == 422


class TestUpdateLtSegmentsChapterMark:
    """PATCH /editor/lt/orders/{order_id}/segments persists chapter-title marks."""

    @patch("core.storage.write_temp_json")
    @patch("core.storage.read_temp_json")
    def test_editor_persists_chapter_title(self, mock_read, mock_write, mock_db):
        mock_db.execute.return_value.fetchone.return_value = MagicMock()
        mock_read.return_value = [{"index": 1, "translated": "old"}]
        client = _make_lt_app(mock_db)
        resp = client.patch(
            "/editor/lt/orders/order-001/segments?role=editor",
            json={"segments": [{"index": 1, "translated": "old", "is_chapter_title": True}]},
        )
        assert resp.status_code == 200
        written = mock_write.call_args.args[2]
        assert written[0]["is_chapter_title"] is True

    @patch("core.storage.write_temp_json")
    @patch("core.storage.read_temp_json")
    def test_editor_unmarks_chapter_title(self, mock_read, mock_write, mock_db):
        mock_db.execute.return_value.fetchone.return_value = MagicMock()
        mock_read.return_value = [{"index": 1, "translated": "old", "is_chapter_title": True}]
        client = _make_lt_app(mock_db)
        resp = client.patch(
            "/editor/lt/orders/order-001/segments?role=editor",
            json={"segments": [{"index": 1, "translated": "old", "is_chapter_title": False}]},
        )
        assert resp.status_code == 200
        written = mock_write.call_args.args[2]
        assert written[0]["is_chapter_title"] is False

    @patch("core.storage.write_temp_json")
    @patch("core.storage.read_temp_json")
    def test_proofreader_cannot_set_chapter_title(self, mock_read, mock_write, mock_db):
        mock_db.execute.return_value.fetchone.return_value = MagicMock()
        mock_read.return_value = [{"index": 1, "translated": "old"}]
        client = _make_lt_app(mock_db)
        resp = client.patch(
            "/editor/lt/orders/order-001/segments?role=proofreader",
            json={"segments": [{"index": 1, "translated": "old", "is_chapter_title": True}]},
        )
        assert resp.status_code == 403
        mock_write.assert_not_called()

    @patch("core.storage.write_temp_json")
    @patch("core.storage.read_temp_json")
    def test_proofreader_without_flag_still_allowed(self, mock_read, mock_write, mock_db):
        mock_db.execute.return_value.fetchone.return_value = MagicMock()
        mock_read.return_value = [{"index": 1, "translated": "old"}]
        client = _make_lt_app(mock_db)
        resp = client.patch(
            "/editor/lt/orders/order-001/segments?role=proofreader",
            json={"segments": [{"index": 1, "translated": "new"}]},
        )
        assert resp.status_code == 200
        written = mock_write.call_args.args[2]
        assert "is_chapter_title" not in written[0]
