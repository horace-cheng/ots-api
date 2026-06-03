import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.database import get_db
from routers.auth import get_admin_user
from routers.admin import router
from tests.factories import MOCK_ADMIN_USER
from sqlalchemy.exc import ProgrammingError

pytestmark = pytest.mark.usefixtures("mock_notification_publisher")


@pytest.fixture
def admin_client(mock_db):
    app = FastAPI()
    app.include_router(router)

    async def override_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_admin_user] = lambda: MOCK_ADMIN_USER

    return TestClient(app)


class TestConfirmManualPayment:
    def _order_row(self, payment_status="pending", status="pending_payment", price=3000):
        row = MagicMock()
        row.status = status
        row.price_ntd = price
        row.payment_status = payment_status
        return row

    def test_success_triggers_pipeline(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = self._order_row()

        with patch("routers.admin.trigger_pipeline", new_callable=AsyncMock) as mock_trigger:
            resp = admin_client.post(
                "/admin/payments/ORDER-001/confirm",
                json={"confirmed_amount_ntd": 3000},
            )

        assert resp.status_code == 200
        mock_trigger.assert_awaited_once_with("ORDER-001")

    def test_amount_mismatch_returns_400(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = self._order_row(price=3000)

        resp = admin_client.post(
            "/admin/payments/ORDER-001/confirm",
            json={"confirmed_amount_ntd": 9999},
        )
        assert resp.status_code == 400
        assert "Amount mismatch" in resp.json()["detail"]

    def test_already_paid_returns_400(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = self._order_row(payment_status="paid")

        resp = admin_client.post(
            "/admin/payments/ORDER-001/confirm",
            json={"confirmed_amount_ntd": 3000},
        )
        assert resp.status_code == 400
        assert "already confirmed" in resp.json()["detail"]

    def test_cancelled_order_returns_400(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = self._order_row(status="cancelled")

        resp = admin_client.post(
            "/admin/payments/ORDER-001/confirm",
            json={"confirmed_amount_ntd": 3000},
        )
        assert resp.status_code == 400

    def test_order_not_found_returns_404(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = None

        resp = admin_client.post(
            "/admin/payments/NONEXIST/confirm",
            json={"confirmed_amount_ntd": 1000},
        )
        assert resp.status_code == 404


class TestResolveQAFlag:
    def test_flag_not_found_returns_404(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = None

        resp = admin_client.patch(
            "/admin/qa-flags/NONEXIST",
            json={"reviewer_note": "Checked and resolved"},
        )
        assert resp.status_code == 404

    def test_resolve_flag_success(self, admin_client, mock_db):
        flag_row = MagicMock()
        flag_row.id = "flag-001"

        job_row = MagicMock()
        job_row.order_id = "order-001"
        job_row.job_id = "job-001"
        job_row.unresolved = 0

        mock_db.execute.return_value.fetchone.side_effect = [flag_row, job_row]

        resp = admin_client.patch(
            "/admin/qa-flags/flag-001",
            json={"reviewer_note": "All good"},
        )
        assert resp.status_code == 200
        assert resp.json()["message"] == "QA flag resolved"


class TestListQAFlags:
    def test_returns_paginated_flags(self, admin_client, mock_db):
        row = MagicMock()
        row._mapping = {
            "id": "flag-001",
            "job_id": "job-001",
            "order_id": "order-001",
            "paragraph_index": 5,
            "flag_level": "must_fix",
            "flag_type": "accuracy",
            "source_segment": "Hello",
            "translated_segment": "你好",
            "reviewer_note": None,
            "resolved": False,
            "flagged_at": datetime.now(timezone.utc),
        }
        
        res_list = MagicMock()
        res_list.fetchall.return_value = [row]
        res_count = MagicMock()
        res_count.scalar.return_value = 1
        mock_db.execute.side_effect = [res_list, res_count]

        resp = admin_client.get("/admin/qa-flags?resolved=false&limit=10&order_id=order-001")
        assert resp.status_code == 200
        data = resp.json()
        assert "flags" in data
        assert data["total"] == 1
        assert len(data["flags"]) == 1
        assert data["flags"][0]["id"] == "flag-001"

    def test_list_all_flags_by_default(self, admin_client, mock_db):
        res_list = MagicMock()
        res_list.fetchall.return_value = []
        res_count = MagicMock()
        res_count.scalar.return_value = 0
        mock_db.execute.side_effect = [res_list, res_count]

        # No resolved param passed
        resp = admin_client.get("/admin/qa-flags")
        assert resp.status_code == 200
        # Verify the query doesn't include WHERE resolved if possible, 
        # but here we just check it doesn't crash.


class TestAdminGetOrder:
    def _order_row(self, qa_result=None, gcs_output_path=None, editor_id=None, qa_id=None, proofreader_id=None, assignment_status=None):
        from datetime import datetime, timezone
        row = MagicMock()
        row._mapping = {
            "id":              "order-001",
            "track_type":      "literary",
            "status":          "processing",
            "source_lang":     "zh-tw",
            "target_lang":     "en",
            "word_count":      5000,
            "price_ntd":       30000,
            "title":           None,
            "notes":           None,
            "created_at":      datetime(2026, 4, 27, tzinfo=timezone.utc),
            "deadline_at":     None,
            "delivered_at":    None,
            "gcs_output_path": gcs_output_path,
            "payment_status":  "paid",
            "invoice_no":      None,
            "qa_result":       qa_result,
            "editor_id":       editor_id,
            "qa_id":           qa_id,
            "proofreader_id":  proofreader_id,
            "assignment_status": assignment_status,
        }
        return row

    def test_order_not_found_returns_404(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = None

        resp = admin_client.get("/admin/orders/nonexistent")
        assert resp.status_code == 404

    def test_success_returns_order_detail(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = self._order_row()

        resp = admin_client.get("/admin/orders/order-001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "order-001"
        assert data["track_type"] == "literary"
        assert data["status"] == "processing"
        assert data["price_ntd"] == 30000

    def test_includes_qa_result(self, admin_client, mock_db):
        qa = {"layer1_structure": {"pass": True, "flags": 0}}
        mock_db.execute.return_value.fetchone.return_value = self._order_row(qa_result=qa)

        resp = admin_client.get("/admin/orders/order-001")
        assert resp.status_code == 200
        assert resp.json()["qa_result"] == qa

    def test_qa_result_none_when_no_pipeline_job(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = self._order_row(qa_result=None)

        resp = admin_client.get("/admin/orders/order-001")
        assert resp.status_code == 200
        assert resp.json()["qa_result"] is None

    def test_includes_qa_id(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = self._order_row(qa_id="qa-user-123")

        resp = admin_client.get("/admin/orders/order-001")
        assert resp.status_code == 200
        assert resp.json()["qa_id"] == "qa-user-123"

    def test_qa_id_none_when_not_assigned(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = self._order_row(qa_id=None)

        resp = admin_client.get("/admin/orders/order-001")
        assert resp.status_code == 200
        assert resp.json()["qa_id"] is None

    def test_includes_editor_id(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = self._order_row(editor_id="editor-user-456")

        resp = admin_client.get("/admin/orders/order-001")
        assert resp.status_code == 200
        assert resp.json()["editor_id"] == "editor-user-456"


class TestAdminGetDownloadUrl:
    def test_order_not_found_returns_404(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = None

        resp = admin_client.get("/admin/orders/nonexistent/download-url")
        assert resp.status_code == 404

    def test_missing_output_path_returns_404(self, admin_client, mock_db):
        row = MagicMock()
        row.gcs_output_path = None
        mock_db.execute.return_value.fetchone.return_value = row

        resp = admin_client.get("/admin/orders/order-001/download-url")
        assert resp.status_code == 404
        assert "Output file not found" in resp.json()["detail"]

    def test_success_returns_signed_url(self, admin_client, mock_db):
        row = MagicMock()
        row.gcs_output_path = "orders/order-001/output.docx"
        mock_db.execute.return_value.fetchone.return_value = row

        with patch("routers.admin.generate_download_signed_url",
                   return_value="https://storage.googleapis.com/signed"):
            resp = admin_client.get("/admin/orders/order-001/download-url")

        assert resp.status_code == 200
        data = resp.json()
        assert data["signed_url"] == "https://storage.googleapis.com/signed"
        assert data["expires_in"] == 3600


class TestListUsers:
    def test_returns_empty_list(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchall.return_value = []
        mock_db.execute.return_value.scalar.return_value = 0

        resp = admin_client.get("/admin/users")
        assert resp.status_code == 200
        data = resp.json()
        assert data["users"] == []
        assert data["total"] == 0

    def test_returns_user_list(self, admin_client, mock_db):
        from datetime import datetime, timezone
        row = MagicMock()
        row._mapping = {
            "id":           "user-001",
            "uid_firebase": "firebase-uid-001",
            "email":        "user@ots.tw",
            "client_type":  "b2c",
            "disabled":     False,
            "created_at":   datetime(2026, 4, 27, tzinfo=timezone.utc),
            "roles":        ["editor"],
            "languages":    [{"source_lang": "zh-tw", "target_lang": "en"}],
        }
        # First call for users list, second for count
        res_list = MagicMock()
        res_list.fetchall.return_value = [row]
        res_count = MagicMock()
        res_count.scalar.return_value = 1
        mock_db.execute.side_effect = [res_list, res_count]

        resp = admin_client.get("/admin/users?limit=10&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["users"]) == 1
        assert data["users"][0]["email"] == "user@ots.tw"
        assert data["total"] == 1


class TestUpdateUser:
    def _user_row(self, uid="other-uid-001"):
        row = MagicMock()
        row.uid_firebase = uid
        row.email = "user@ots.tw"
        return row

    def test_user_not_found_returns_404(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = None

        resp = admin_client.patch("/admin/users/nonexistent", json={"disabled": True})
        assert resp.status_code == 404

    def test_cannot_disable_own_account(self, admin_client, mock_db):
        from tests.factories import MOCK_ADMIN_USER
        # Return a row whose uid_firebase matches the acting admin's uid
        mock_db.execute.return_value.fetchone.return_value = self._user_row(
            uid=MOCK_ADMIN_USER["uid"]
        )

        resp = admin_client.patch("/admin/users/admin-db-id", json={"disabled": True})
        assert resp.status_code == 400
        assert "Cannot disable your own account" in resp.json()["detail"]

    def test_cannot_remove_own_admin_role(self, admin_client, mock_db):
        from tests.factories import MOCK_ADMIN_USER
        mock_db.execute.return_value.fetchone.return_value = self._user_row(
            uid=MOCK_ADMIN_USER["uid"]
        )

        resp = admin_client.patch("/admin/users/admin-db-id", json={"is_admin": False})
        assert resp.status_code == 400
        assert "Cannot remove your own admin role" in resp.json()["detail"]

    def test_disable_user_success(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = self._user_row()

        resp = admin_client.patch("/admin/users/user-001", json={"disabled": True})
        assert resp.status_code == 200
        assert resp.json()["message"] == "User updated"
        mock_db.commit.assert_awaited()

    def test_grant_admin_success(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = self._user_row()

        resp = admin_client.patch("/admin/users/user-001", json={"is_admin": True})
        assert resp.status_code == 200
        mock_db.commit.assert_awaited()

    def test_revoke_admin_success(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = self._user_row()

        resp = admin_client.patch("/admin/users/user-001", json={"is_admin": False})
        assert resp.status_code == 200
        mock_db.commit.assert_awaited()

    def test_update_is_editor_success(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = self._user_row()

        resp = admin_client.patch("/admin/users/user-001", json={"is_editor": True})
        assert resp.status_code == 200
        mock_db.commit.assert_awaited()

    def test_grant_qa_success(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = self._user_row()

        resp = admin_client.patch("/admin/users/user-001", json={"is_qa": True})
        assert resp.status_code == 200
        assert resp.json()["message"] == "User updated"
        mock_db.commit.assert_awaited()

    def test_revoke_qa_success(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = self._user_row()

        resp = admin_client.patch("/admin/users/user-001", json={"is_qa": False})
        assert resp.status_code == 200
        mock_db.commit.assert_awaited()

    def test_revoke_editor_success(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = self._user_row()

        resp = admin_client.patch("/admin/users/user-001", json={"is_editor": False})
        assert resp.status_code == 200
        mock_db.commit.assert_awaited()

    def test_cannot_disable_own_account_when_re_enabling(self, admin_client, mock_db):
        """disabled=False on own account is allowed (only True is blocked)."""
        from tests.factories import MOCK_ADMIN_USER
        mock_db.execute.return_value.fetchone.return_value = self._user_row(
            uid=MOCK_ADMIN_USER["uid"]
        )
        resp = admin_client.patch("/admin/users/admin-db-id", json={"disabled": False})
        # Re-enabling own account is fine — only disabling is blocked
        assert resp.status_code == 200


class TestUpdateAssignment:
    def test_no_fields_returns_400(self, admin_client):
        resp = admin_client.patch("/admin/assignments/ORDER-001", json={})
        assert resp.status_code == 400
        assert "No fields to update" in resp.json()["detail"]

    def test_assign_editor_succeeds(self, admin_client, mock_db):
        now = datetime.now(timezone.utc)
        row = MagicMock()
        row._mapping = {
            "id": "assign-id",
            "order_id": "ORDER-001",
            "editor_id": "editor-001",
            "qa_id": None,
            "proofreader_id": None,
            "status": "editing",
            "assigned_at": now,
            "editor_submitted_at": None,
            "proofread_submitted_at": None,
            "qa_submitted_at": None,
            "editor_notes": None,
            "proofreader_notes": None,
        }
        mock_db.execute.return_value.fetchone.return_value = row

        resp = admin_client.patch(
            "/admin/assignments/ORDER-001",
            json={"editor_id": "editor-001"},
        )
        assert resp.status_code == 200
        assert resp.json()["editor_id"] == "editor-001"


class TestListAssignments:
    def test_returns_paginated_assignments(self, admin_client, mock_db):
        row = MagicMock()
        row._mapping = {
            "id": "assign-001",
            "order_id": "order-001",
            "editor_id": "editor-001",
            "qa_id": None,
            "proofreader_id": None,
            "status": "editing",
            "assigned_at": datetime.now(timezone.utc),
            "editor_submitted_at": None,
            "proofread_submitted_at": None,
            "qa_submitted_at": None,
            "editor_notes": None,
            "proofreader_notes": None,
        }
        
        res_list = MagicMock()
        res_list.fetchall.return_value = [row]
        res_count = MagicMock()
        res_count.scalar.return_value = 1
        mock_db.execute.side_effect = [res_list, res_count]

        resp = admin_client.get("/admin/assignments?status=editing&limit=5")
        assert resp.status_code == 200
        data = resp.json()
        assert "assignments" in data
        assert data["total"] == 1
        assert len(data["assignments"]) == 1
        assert data["assignments"][0]["order_id"] == "order-001"


class TestAdminListOrders:
    def test_returns_paginated_orders(self, admin_client, mock_db):
        from datetime import datetime, timezone
        row = MagicMock()
        row._mapping = {
            "id":              "order-001",
            "track_type":      "fast",
            "status":          "paid",
            "source_lang":     "zh-tw",
            "target_lang":     "en",
            "word_count":      1000,
            "price_ntd":       2000,
            "title":           None,
            "notes":           None,
            "created_at":      datetime(2026, 4, 27, tzinfo=timezone.utc),
            "deadline_at":     None,
            "delivered_at":    None,
            "payment_status":  "paid",
            "invoice_no":      None,
            "gcs_output_path": None,
        }
        
        res_list = MagicMock()
        res_list.fetchall.return_value = [row]
        res_count = MagicMock()
        res_count.scalar.return_value = 1
        mock_db.execute.side_effect = [res_list, res_count]

        resp = admin_client.get("/admin/orders?limit=10")
        assert resp.status_code == 200
        data = resp.json()
        assert "orders" in data
        assert data["total"] == 1
        assert len(data["orders"]) == 1


class TestQAReviewEditor:
    @patch("core.storage.read_temp_json")
    def test_get_segments_success(self, mock_read, admin_client, mock_db):
        mock_read.side_effect = [
            [{"index": 0, "text": "Hello"}],  # segments
            [{"index": 0, "translated": "你好"}], # translations
            [{"index": 0, "translated": "你好 (raw)"}], # translations_raw
        ]
        
        # Mock DB for flags
        mock_db.execute.return_value.fetchall.return_value = []
        
        resp = admin_client.get("/admin/orders/order-001/segments")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["segments"]) == 1
        assert data["segments"][0]["source"] == "Hello"
        assert data["segments"][0]["translated"] == "你好"

    @patch("core.storage.read_temp_json")
    @patch("core.storage.write_temp_json")
    def test_update_segments_success(self, mock_write, mock_read, admin_client):
        mock_read.return_value = [{"index": 0, "translated": "old", "comments": None}]
        
        resp = admin_client.patch(
            "/admin/orders/order-001/segments",
            json={"segments": [{"index": 0, "translated": "new", "comments": "fixed"}]}
        )
        assert resp.status_code == 200
        mock_write.assert_called_once()
        args = mock_write.call_args[0]
        # args[2] is the data written
        assert args[2][0]["translated"] == "new"

    def test_mark_qa_done_success(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = MagicMock()
        resp = admin_client.post("/admin/orders/order-001/qa-done")
        assert resp.status_code == 200
        assert "editor_verify" in resp.json()["message"].lower()

    def test_assign_editor_and_qa_success(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = MagicMock()
        resp = admin_client.patch(
            "/admin/orders/order-001/assign-editor", 
            json={"editor_id": "editor-001", "qa_id": "qa-001"}
        )
        assert resp.status_code == 200
        assert "editor/qa assigned" in resp.json()["message"].lower()

    def test_update_status_success(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = MagicMock()
        resp = admin_client.patch("/admin/orders/order-001/status", params={"status": "qa_review"})
        assert resp.status_code == 200
        assert "status updated" in resp.json()["message"].lower()


class TestUserLanguages:
    def test_update_languages_success(self, admin_client, mock_db):
        resp = admin_client.put(
            "/admin/users/user-001/languages",
            json={"languages": [{"source_lang": "zh-tw", "target_lang": "en"}]}
        )
        assert resp.status_code == 200
        assert resp.json()["message"] == "Languages updated"
        mock_db.commit.assert_awaited()


class TestListEligibleUsers:
    def test_success_returns_users(self, admin_client, mock_db):
        order_row = MagicMock()
        order_row.source_lang = "zh-tw"
        order_row.target_lang = "en"

        user_row = MagicMock()
        user_row._mapping = {
            "id": "user-001",
            "uid_firebase": "uid-001",
            "email": "editor@ots.tw",
            "client_type": "b2c",
            "disabled": False,
            "created_at": datetime.now(timezone.utc),
            "roles": ["editor"],
            "languages": [{"source_lang": "zh-tw", "target_lang": "en"}]
        }

        mock_db.execute.return_value.fetchone.return_value = order_row
        mock_db.execute.return_value.fetchall.return_value = [user_row]

        resp = admin_client.get("/admin/orders/order-001/eligible-users")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["users"]) == 1
        assert data["users"][0]["is_editor"] is True

    def test_order_not_found_returns_404(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = None

        resp = admin_client.get("/admin/orders/nonexistent/eligible-users")
        assert resp.status_code == 404

    def test_returns_empty_when_no_eligible_users(self, admin_client, mock_db):
        order_row = MagicMock()
        order_row.source_lang = "tai-lo"
        order_row.target_lang = "zh-tw"

        mock_db.execute.return_value.fetchone.return_value = order_row
        mock_db.execute.return_value.fetchall.return_value = []

        resp = admin_client.get("/admin/orders/order-001/eligible-users")
        assert resp.status_code == 200
        data = resp.json()
        assert data["users"] == []
        assert data["total"] == 0


class TestAssignEditor:
    def test_assign_editor_invalid_role_returns_400(self, admin_client, mock_db):
        """editor_id points to a user who lacks the editor role."""
        # First call (editor check) returns None → not an editor
        mock_db.execute.return_value.fetchone.return_value = None

        resp = admin_client.patch(
            "/admin/orders/order-001/assign-editor",
            json={"editor_id": "not-an-editor"}
        )
        assert resp.status_code == 400
        assert "not an editor" in resp.json()["detail"].lower()

    def test_assign_qa_invalid_role_returns_400(self, admin_client, mock_db):
        """qa_id points to a user who lacks the qa role."""
        mock_db.execute.return_value.fetchone.return_value = None

        resp = admin_client.patch(
            "/admin/orders/order-001/assign-editor",
            json={"qa_id": "not-a-qa"}
        )
        assert resp.status_code == 400
        assert "not a qa" in resp.json()["detail"].lower()


class TestMarkDelivered:
    def test_success(self, admin_client, mock_db):
        row = MagicMock()
        row.status = "editor_verify"
        mock_db.execute.return_value.fetchone.return_value = row

        resp = admin_client.post(
            "/admin/orders/order-001/deliver",
            params={"gcs_output_path": "orders/order-001/output.docx"}
        )
        assert resp.status_code == 200
        assert "delivered" in resp.json()["message"].lower()
        mock_db.commit.assert_awaited()

    def test_order_not_found_returns_404(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = None

        resp = admin_client.post(
            "/admin/orders/nonexistent/deliver",
            params={"gcs_output_path": "orders/x/output.docx"}
        )
        assert resp.status_code == 404

    def test_already_delivered_returns_400(self, admin_client, mock_db):
        row = MagicMock()
        row.status = "delivered"
        mock_db.execute.return_value.fetchone.return_value = row

        resp = admin_client.post(
            "/admin/orders/order-001/deliver",
            params={"gcs_output_path": "orders/order-001/output.docx"}
        )
        assert resp.status_code == 400
        assert "already delivered" in resp.json()["detail"].lower()


class TestSetOrderQuote:
    def test_quote_lt_order_awaiting_quote(self, admin_client, mock_db):
        row = MagicMock()
        row.id = "order-001"
        row.status = "awaiting_quote"
        row.track_type = "literary"
        row.price_ntd = 0
        mock_db.execute.return_value.fetchone.return_value = row

        resp = admin_client.post(
            "/admin/orders/order-001/quote",
            json={"quoted_price": 30000}
        )
        assert resp.status_code == 200
        assert "Quote set" in resp.json()["message"]
        mock_db.commit.assert_awaited()

    def test_quote_lt_order_already_quoted(self, admin_client, mock_db):
        row = MagicMock()
        row.id = "order-001"
        row.status = "quoted"
        row.track_type = "literary"
        row.price_ntd = 25000
        mock_db.execute.return_value.fetchone.return_value = row

        resp = admin_client.post(
            "/admin/orders/order-001/quote",
            json={"quoted_price": 35000}
        )
        assert resp.status_code == 200
        mock_db.commit.assert_awaited()

    def test_quote_fast_track_returns_400(self, admin_client, mock_db):
        row = MagicMock()
        row.id = "order-001"
        row.status = "pending_payment"
        row.track_type = "fast"
        row.price_ntd = 2000
        mock_db.execute.return_value.fetchone.return_value = row

        resp = admin_client.post(
            "/admin/orders/order-001/quote",
            json={"quoted_price": 3000}
        )
        assert resp.status_code == 400
        assert "Quote only applies" in resp.json()["detail"]

    def test_quote_wrong_status_returns_400(self, admin_client, mock_db):
        row = MagicMock()
        row.id = "order-001"
        row.status = "processing"
        row.track_type = "literary"
        row.price_ntd = 30000
        mock_db.execute.return_value.fetchone.return_value = row

        resp = admin_client.post(
            "/admin/orders/order-001/quote",
            json={"quoted_price": 35000}
        )
        assert resp.status_code == 400
        assert "Cannot set quote" in resp.json()["detail"]

    def test_quote_order_not_found_returns_404(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = None

        resp = admin_client.post(
            "/admin/orders/nonexistent/quote",
            json={"quoted_price": 3000}
        )
        assert resp.status_code == 404


class TestAssignLiteraryRole:
    def _user_row(self, user_id="editor-001", is_editor=True):
        row = MagicMock()
        row.id = user_id
        row.is_editor = is_editor
        return row

    def _assign_row(self, status="pending"):
        from datetime import datetime, timezone
        row = MagicMock()
        row.id = "assign-001"
        row.order_id = "order-001"
        row.editor_id = None
        row.qa_id = None
        row.proofreader_id = None
        row.status = status
        row.assigned_at = datetime.now(timezone.utc)
        row.editor_submitted_at = None
        row.proofread_submitted_at = None
        row.qa_submitted_at = None
        row.editor_notes = None
        row.proofreader_notes = None
        row._mapping = {
            "id": "assign-001",
            "order_id": "order-001",
            "editor_id": None,
            "qa_id": None,
            "proofreader_id": None,
            "status": status,
            "assigned_at": datetime.now(timezone.utc),
            "editor_submitted_at": None,
            "proofread_submitted_at": None,
            "qa_submitted_at": None,
            "editor_notes": None,
            "proofreader_notes": None,
        }
        return row

    def _make_execute_handler(self, results_by_keyword):
        """Create a callable that returns different results based on SQL content.
        Note: get_admin_user is overridden in admin_client, so no admin_users query.
        """
        def handler(*args, **kwargs):
            sql = str(args[0]).lower() if args else ''
            for keyword, result in results_by_keyword.items():
                if keyword in sql:
                    return result
            r = MagicMock()
            r.fetchone.return_value = None
            return r
        return handler

    def test_assign_editor_by_user_id(self, admin_client, mock_db):
        user_res = MagicMock()
        user_res.fetchone.return_value = self._user_row("editor-001", is_editor=True)
        assign_res = MagicMock()
        assign_res.fetchone.return_value = self._assign_row("pending")
        result_res = MagicMock()
        result_res.fetchone.return_value = self._assign_row("editing")

        mock_db.execute.side_effect = self._make_execute_handler({
            "select id, is_editor": user_res,
            "select status from assignments": assign_res,
            "select id, order_id, editor_id, qa_id, proofreader_id": result_res,
        })

        resp = admin_client.post(
            "/admin/assignments/order-001",
            json={"role": "editor", "user_id": "editor-001"}
        )
        assert resp.status_code == 200, resp.json()
        data = resp.json()
        assert data["status"] == "editing"

    def test_assign_editor_not_an_editor_returns_400(self, admin_client, mock_db):
        user_res = MagicMock()
        user_res.fetchone.return_value = self._user_row("qa-001", is_editor=False)

        mock_db.execute.side_effect = self._make_execute_handler({
            "select id, is_editor": user_res,
        })

        resp = admin_client.post(
            "/admin/assignments/order-001",
            json={"role": "editor", "user_id": "qa-001"}
        )
        assert resp.status_code == 400
        assert "does not have editor role" in resp.json()["detail"].lower()

    def test_assign_editor_wrong_status_returns_400(self, admin_client, mock_db):
        user_res = MagicMock()
        user_res.fetchone.return_value = self._user_row("editor-001", is_editor=True)
        assign_res = MagicMock()
        assign_res.fetchone.return_value = self._assign_row("editor_done")

        mock_db.execute.side_effect = self._make_execute_handler({
            "select id, is_editor": user_res,
            "select status from assignments": assign_res,
        })

        resp = admin_client.post(
            "/admin/assignments/order-001",
            json={"role": "editor", "user_id": "editor-001"}
        )
        assert resp.status_code == 400
        assert "Cannot assign editor" in resp.json()["detail"]

    def test_assign_proofreader_by_email(self, admin_client, mock_db):
        user_res = MagicMock()
        user_res.fetchone.return_value = self._user_row("proof-001", is_editor=True)
        assign_res = MagicMock()
        assign_res.fetchone.return_value = self._assign_row("editor_done")
        result_res = MagicMock()
        result_res.fetchone.return_value = self._assign_row("proofreading")

        mock_db.execute.side_effect = self._make_execute_handler({
            "select id, is_editor": user_res,
            "select status from assignments": assign_res,
            "select id, order_id, editor_id, qa_id, proofreader_id": result_res,
        })

        resp = admin_client.post(
            "/admin/assignments/order-001",
            json={"role": "proofreader", "email": "proof@ots.tw"}
        )
        assert resp.status_code == 200, resp.json()
        data = resp.json()
        assert data["status"] == "proofreading"

    def test_assign_proofreader_wrong_status_returns_400(self, admin_client, mock_db):
        user_res = MagicMock()
        user_res.fetchone.return_value = self._user_row("proof-001", is_editor=True)
        assign_res = MagicMock()
        assign_res.fetchone.return_value = self._assign_row("editing")

        mock_db.execute.side_effect = self._make_execute_handler({
            "select id, is_editor": user_res,
            "select status from assignments": assign_res,
        })

        resp = admin_client.post(
            "/admin/assignments/order-001",
            json={"role": "proofreader", "user_id": "proof-001"}
        )
        assert resp.status_code == 400
        assert "Cannot assign proofreader" in resp.json()["detail"]

    def test_assign_missing_role_returns_400(self, admin_client, mock_db):
        mock_db.execute.side_effect = self._make_execute_handler({})

    def test_assign_missing_user_id_and_email_returns_400(self, admin_client, mock_db):
        mock_db.execute.side_effect = self._make_execute_handler({})

        resp = admin_client.post(
            "/admin/assignments/order-001",
            json={"role": "translator", "user_id": "x"}
        )
        assert resp.status_code == 400

    def test_assign_missing_user_id_and_email_returns_400(self, admin_client, mock_db):
        admin_res = MagicMock()
        admin_res.fetchone.return_value = MagicMock(id="admin-id", role="admin", active=True)
        mock_db.execute.side_effect = self._make_execute_handler({
            "admin_users": admin_res,
        })

        resp = admin_client.post(
            "/admin/assignments/order-001",
            json={"role": "editor"}
        )
        assert resp.status_code == 400

    def test_assign_user_not_found_returns_404(self, admin_client, mock_db):
        user_res = MagicMock()
        user_res.fetchone.return_value = None

        mock_db.execute.side_effect = self._make_execute_handler({
            "select id, is_editor": user_res,
        })

        resp = admin_client.post(
            "/admin/assignments/order-001",
            json={"role": "editor", "user_id": "nonexistent"}
        )
        assert resp.status_code == 404

    def test_complete_editor(self, admin_client, mock_db):
        admin_res = MagicMock()
        admin_res.fetchone.return_value = MagicMock(id="admin-id", role="admin", active=True)
        assign_res = MagicMock()
        assign_res.fetchone.return_value = self._assign_row("editing")
        result_res = MagicMock()
        result_res.fetchone.return_value = self._assign_row("editor_done")

        mock_db.execute.side_effect = self._make_execute_handler({
            "admin_users": admin_res,
            "select id, status, editor_id, proofreader_id": assign_res,
            "select id, order_id, editor_id, qa_id, proofreader_id": result_res,
        })

        resp = admin_client.post(
            "/admin/assignments/order-001/complete",
            json={"role": "editor"}
        )
        assert resp.status_code == 200, resp.json()
        data = resp.json()
        assert data["status"] == "editor_done"

    def test_complete_proofreader(self, admin_client, mock_db):
        admin_res = MagicMock()
        admin_res.fetchone.return_value = MagicMock(id="admin-id", role="admin", active=True)
        assign_res = MagicMock()
        assign_res.fetchone.return_value = self._assign_row("proofreading")
        result_res = MagicMock()
        result_res.fetchone.return_value = self._assign_row("proofread_done")

        mock_db.execute.side_effect = self._make_execute_handler({
            "admin_users": admin_res,
            "select id, status, editor_id, proofreader_id": assign_res,
            "select id, order_id, editor_id, qa_id, proofreader_id": result_res,
        })

        resp = admin_client.post(
            "/admin/assignments/order-001/complete",
            json={"role": "proofreader"}
        )
        assert resp.status_code == 200, resp.json()
        data = resp.json()
        assert data["status"] == "proofread_done"

    def test_complete_editor_wrong_status_returns_400(self, admin_client, mock_db):
        admin_res = MagicMock()
        admin_res.fetchone.return_value = MagicMock(id="admin-id", role="admin", active=True)
        assign_res = MagicMock()
        assign_res.fetchone.return_value = self._assign_row("pending")

        mock_db.execute.side_effect = self._make_execute_handler({
            "admin_users": admin_res,
            "select id, status, editor_id, proofreader_id": assign_res,
        })

        resp = admin_client.post(
            "/admin/assignments/order-001/complete",
            json={"role": "editor"}
        )
        assert resp.status_code == 400
        assert "Editor can only complete" in resp.json()["detail"]

    def test_complete_proofreader_wrong_status_returns_400(self, admin_client, mock_db):
        admin_res = MagicMock()
        admin_res.fetchone.return_value = MagicMock(id="admin-id", role="admin", active=True)
        assign_res = MagicMock()
        assign_res.fetchone.return_value = self._assign_row("editing")

        mock_db.execute.side_effect = self._make_execute_handler({
            "admin_users": admin_res,
            "select id, status, editor_id, proofreader_id": assign_res,
        })

        resp = admin_client.post(
            "/admin/assignments/order-001/complete",
            json={"role": "proofreader"}
        )
        assert resp.status_code == 400
        assert "Proofreader can only complete" in resp.json()["detail"]

    def test_complete_assignment_not_found_returns_404(self, admin_client, mock_db):
        admin_res = MagicMock()
        admin_res.fetchone.return_value = MagicMock(id="admin-id", role="admin", active=True)
        assign_res = MagicMock()
        assign_res.fetchone.return_value = None

        mock_db.execute.side_effect = self._make_execute_handler({
            "admin_users": admin_res,
            "select id, status, editor_id, proofreader_id": assign_res,
        })

        resp = admin_client.post(
            "/admin/assignments/nonexistent/complete",
            json={"role": "editor"}
        )
        assert resp.status_code == 404


class TestCompleteAssignment:
    def _assign_row(self, status="editing"):
        from datetime import datetime, timezone
        row = MagicMock()
        row.id = "assign-001"
        row.order_id = "order-001"
        row.editor_id = "editor-001"
        row.qa_id = None
        row.proofreader_id = None
        row.status = status
        row.assigned_at = datetime.now(timezone.utc)
        row.editor_submitted_at = None
        row.proofread_submitted_at = None
        row.qa_submitted_at = None
        row.editor_notes = None
        row.proofreader_notes = None
        row._mapping = {
            "id": "assign-001",
            "order_id": "order-001",
            "editor_id": "editor-001",
            "qa_id": None,
            "proofreader_id": None,
            "status": status,
            "assigned_at": datetime.now(timezone.utc),
            "editor_submitted_at": None,
            "proofread_submitted_at": None,
            "qa_submitted_at": None,
            "editor_notes": None,
            "proofreader_notes": None,
        }
        return row

    def test_complete_editor(self, admin_client, mock_db):
        assign_res = MagicMock()
        assign_res.fetchone.return_value = self._assign_row("editing")
        update_res = MagicMock()
        result_res = MagicMock()
        result_res.fetchone.return_value = self._assign_row("editor_done")

        mock_db.execute.side_effect = [assign_res, update_res, result_res]

        resp = admin_client.post(
            "/admin/assignments/order-001/complete",
            json={"role": "editor"}
        )
        assert resp.status_code == 200, resp.json()
        data = resp.json()
        assert data["status"] == "editor_done"

    def test_complete_proofreader(self, admin_client, mock_db):
        assign_res = MagicMock()
        assign_res.fetchone.return_value = self._assign_row("proofreading")
        update_res = MagicMock()
        result_res = MagicMock()
        result_res.fetchone.return_value = self._assign_row("proofread_done")

        mock_db.execute.side_effect = [assign_res, update_res, result_res]

        resp = admin_client.post(
            "/admin/assignments/order-001/complete",
            json={"role": "proofreader"}
        )
        assert resp.status_code == 200, resp.json()
        data = resp.json()
        assert data["status"] == "proofread_done"

    def test_complete_editor_wrong_status_returns_400(self, admin_client, mock_db):
        assign_res = MagicMock()
        assign_res.fetchone.return_value = self._assign_row("pending")

        mock_db.execute.side_effect = [assign_res, MagicMock()]

        resp = admin_client.post(
            "/admin/assignments/order-001/complete",
            json={"role": "editor"}
        )
        assert resp.status_code == 400
        assert "Editor can only complete" in resp.json()["detail"]

    def test_complete_proofreader_wrong_status_returns_400(self, admin_client, mock_db):
        assign_res = MagicMock()
        assign_res.fetchone.return_value = self._assign_row("editing")

        mock_db.execute.side_effect = [assign_res, MagicMock()]

        resp = admin_client.post(
            "/admin/assignments/order-001/complete",
            json={"role": "proofreader"}
        )
        assert resp.status_code == 400
        assert "Proofreader can only complete" in resp.json()["detail"]

    def test_complete_assignment_not_found_returns_404(self, admin_client, mock_db):
        assign_res = MagicMock()
        assign_res.fetchone.return_value = None

        mock_db.execute.side_effect = [assign_res, MagicMock()]

        resp = admin_client.post(
            "/admin/assignments/nonexistent/complete",
            json={"role": "editor"}
        )
        assert resp.status_code == 404

    def test_complete_invalid_role_returns_400(self, admin_client):
        resp = admin_client.post(
            "/admin/assignments/order-001/complete",
            json={"role": "translator"}
        )
        assert resp.status_code == 400


class TestGetAssignment:
    def test_get_assignment_not_found_returns_404(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = None

        resp = admin_client.get("/admin/assignments/nonexistent")
        assert resp.status_code == 404

    def test_get_assignment_success(self, admin_client, mock_db):
        from datetime import datetime, timezone
        row = MagicMock()
        row._mapping = {
            "id": "assign-001",
            "order_id": "order-001",
            "editor_id": "editor-001",
            "qa_id": None,
            "proofreader_id": None,
            "status": "editing",
            "assigned_at": datetime.now(timezone.utc),
            "editor_submitted_at": None,
            "proofread_submitted_at": None,
            "qa_submitted_at": None,
            "editor_notes": None,
            "proofreader_notes": None,
        }
        mock_db.execute.return_value.fetchone.return_value = row

        resp = admin_client.get("/admin/assignments/order-001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["order_id"] == "order-001"
        assert data["status"] == "editing"


class TestAdminOrderStatus:
    def test_order_not_found_returns_404(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = None

        resp = admin_client.patch(
            "/admin/orders/nonexistent/status",
            params={"status": "qa_review"}
        )
        assert resp.status_code == 404

    def test_update_to_each_valid_status(self, admin_client, mock_db):
        for status in ("qa_review", "editor_verify", "delivered", "processing"):
            mock_db.reset_mock()
            mock_db.execute.return_value.fetchone.return_value = MagicMock()
            resp = admin_client.patch(
                "/admin/orders/order-001/status",
                params={"status": status}
            )
            assert resp.status_code == 200
            assert status in resp.json()["message"]


class TestAdminRetranslate:
    def test_order_not_found_returns_404(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = None

        with patch("routers.admin.trigger_pipeline", new_callable=AsyncMock):
            resp = admin_client.post("/admin/orders/nonexistent/retranslate")

        assert resp.status_code == 404

    def test_success_resets_status_and_triggers_pipeline(self, admin_client, mock_db):
        row = MagicMock()
        row.id = "order-001"
        row.status = "qa_review"
        mock_db.execute.return_value.fetchone.return_value = row

        with patch("routers.admin.trigger_pipeline", new_callable=AsyncMock) as mock_trigger:
            mock_trigger.return_value = "msg-123"
            with patch("routers.admin.svc_save_version", new_callable=AsyncMock) as mock_save:
                mock_save.return_value = None
                resp = admin_client.post("/admin/orders/order-001/retranslate")

        assert resp.status_code == 200
        assert "re-triggered" in resp.json()["message"]
        mock_db.commit.assert_awaited()
        mock_trigger.assert_awaited_once_with("order-001")


class TestAdminSupportFiles:
    def test_list_empty_when_no_files(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchall.return_value = []

        resp = admin_client.get("/admin/orders/order-001/support-files")

        assert resp.status_code == 200
        data = resp.json()
        assert data["files"] == []
        assert data["total"] == 0

    def test_list_returns_files(self, admin_client, mock_db):
        row = MagicMock()
        row._mapping = {
            "id": "file-001",
            "order_id": "order-001",
            "filename": "glossary.pdf",
            "content_type": "application/pdf",
            "file_size": 12345,
            "gcs_path": "orders/order-001/support/glossary.pdf",
            "file_role": "glossary",
            "created_at": "2026-05-09T00:00:00Z",
        }
        mock_db.execute.return_value.fetchall.return_value = [row]

        resp = admin_client.get("/admin/orders/order-001/support-files")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["files"]) == 1
        assert data["total"] == 1
        assert data["files"][0]["filename"] == "glossary.pdf"
        assert data["files"][0]["file_role"] == "glossary"

    def test_get_content_returns_html(self, admin_client, mock_db):
        row = MagicMock()
        row.gcs_path = "orders/order-001/support/doc.txt"
        row.filename = "doc.txt"
        row.content_type = "text/plain"
        mock_db.execute.return_value.fetchone.return_value = row

        with patch("routers.admin.read_blob") as mock_read:
            mock_read.return_value = (b"Hello World", "doc.txt")
            with patch("routers.admin.convert_document") as mock_convert:
                mock_convert.return_value = MagicMock(
                    filename="doc.txt",
                    content_type="text/plain",
                    html="<pre>Hello World</pre>",
                )
                resp = admin_client.get("/admin/orders/order-001/support-files/file-001/content")

        assert resp.status_code == 200
        data = resp.json()
        assert data["html"] == "<pre>Hello World</pre>"

    def test_get_content_not_found(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = None

        resp = admin_client.get("/admin/orders/order-001/support-files/nonexistent/content")

        assert resp.status_code == 404


class TestAdminTokenUsage:

    def _make_fetchall_row(self, job_type, model, prompt, candidates, cost, input_rate=0, output_rate=0):
        row = MagicMock()
        row.job_type = job_type
        row.model = model
        row.prompt_tokens = prompt
        row.candidates_tokens = candidates
        row.total_tokens = prompt + candidates
        row.cost_usd = cost
        row.input_rate = input_rate
        row.output_rate = output_rate
        return row

    def test_returns_aggregated_data(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchall.return_value = [
            self._make_fetchall_row("nmt", "gemini-2.5-pro", 12000, 6000, 0.075, input_rate=1.25, output_rate=10.0),
            self._make_fetchall_row("qa_auto", "gemini-2.5-flash", 340, 780, 0.00069, input_rate=0.30, output_rate=2.50),
        ]

        resp = admin_client.get("/admin/orders/order-001/token-usage")

        assert resp.status_code == 200
        data = resp.json()
        assert data["order_id"] == "order-001"
        assert data["total_prompt"] == 12340
        assert data["total_candidates"] == 6780
        assert data["total_tokens"] == 19120
        assert data["total_cost_usd"] == pytest.approx(0.07569, rel=1e-4)
        assert len(data["breakdown"]) == 2

        nmt = [b for b in data["breakdown"] if b["job_type"] == "nmt"][0]
        assert nmt["model"] == "gemini-2.5-pro"
        assert nmt["prompt_tokens"] == 12000
        assert nmt["candidates_tokens"] == 6000
        assert nmt["total_tokens"] == 18000
        assert nmt["input_rate"] == 1.25
        assert nmt["output_rate"] == 10.0
        assert nmt["cost_usd"] == pytest.approx(0.075, rel=1e-4)

        qa = [b for b in data["breakdown"] if b["job_type"] == "qa_auto"][0]
        assert qa["input_rate"] == 0.30
        assert qa["output_rate"] == 2.50

    def test_no_data_returns_404(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchall.return_value = []

        resp = admin_client.get("/admin/orders/order-002/token-usage")

        assert resp.status_code == 404
        assert "No token usage data" in resp.json()["detail"]

    def test_missing_table_returns_404(self, admin_client, mock_db):
        mock_db.execute.side_effect = ProgrammingError(
            statement="SELECT ... FROM token_usage",
            params={},
            orig=Exception("relation \"token_usage\" does not exist"),
        )

        resp = admin_client.get("/admin/orders/order-003/token-usage")

        assert resp.status_code == 404
        assert "No token usage data" in resp.json()["detail"]


class TestAdminTokenUsageDetail:

    def _make_row(self, job_type, model, prompt, candidates, cost,
                  input_rate=0, output_rate=0, created_at=None):
        from datetime import datetime, timezone
        row = MagicMock()
        row.job_type = job_type
        row.model = model
        row.prompt_tokens = prompt
        row.candidates_tokens = candidates
        row.total_tokens = prompt + candidates
        row.input_rate = input_rate
        row.output_rate = output_rate
        row.cost_usd = cost
        row.created_at = created_at or datetime.now(timezone.utc)
        return row

    def _make_handler(self, rows, limit=None, offset=None):
        """Return an execute side_effect handler: first call returns COUNT, second returns data rows."""
        count_mock = MagicMock()
        count_mock.scalar.return_value = len(rows)
        data_mock = MagicMock()
        if limit is not None:
            data_mock.fetchall.return_value = rows[offset:offset + limit]
        else:
            data_mock.fetchall.return_value = rows
        calls = [count_mock, data_mock]

        def handler(*args, **kwargs):
            return calls.pop(0)
        return handler, len(rows)

    def test_returns_detail_rows(self, admin_client, mock_db):
        rows = [
            self._make_row("nmt", "gemini-2.5-pro", 100, 50, 0.001, 1.25, 10.0),
            self._make_row("nmt", "gemini-2.5-pro", 200, 80, 0.002, 1.25, 10.0),
            self._make_row("qa_auto", "gemini-2.5-flash", 30, 60, 0.0001, 0.30, 2.50),
        ]
        handler, total = self._make_handler(rows)
        mock_db.execute.side_effect = handler

        resp = admin_client.get("/admin/orders/order-001/token-usage-detail")

        assert resp.status_code == 200
        data = resp.json()
        assert data["order_id"] == "order-001"
        assert data["total"] == total
        assert len(data["items"]) == 3

        first = data["items"][0]
        assert first["job_type"] == "nmt"
        assert first["prompt_tokens"] == 100
        assert first["candidates_tokens"] == 50
        assert first["total_tokens"] == 150
        assert first["input_rate"] == 1.25
        assert first["output_rate"] == 10.0
        assert first["cost_usd"] == pytest.approx(0.001, rel=1e-4)

    def test_detail_pagination(self, admin_client, mock_db):
        rows = [self._make_row("nmt", "gemini-2.5-pro", i * 100, i * 50, 0.001, 1.25, 10.0)
                for i in range(20)]
        handler, total = self._make_handler(rows, limit=5, offset=5)
        mock_db.execute.side_effect = handler

        resp = admin_client.get("/admin/orders/order-001/token-usage-detail?limit=5&offset=5")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 20
        assert len(data["items"]) == 5
        assert data["items"][0]["prompt_tokens"] == 500  # row index 5: 5*100 = 500

    def test_no_data_returns_404(self, admin_client, mock_db):
        count_mock = MagicMock()
        count_mock.scalar.return_value = 0
        mock_db.execute.return_value = count_mock

        resp = admin_client.get("/admin/orders/order-002/token-usage-detail")

        assert resp.status_code == 404
        assert "No token usage data" in resp.json()["detail"]

    def test_missing_table_returns_404(self, admin_client, mock_db):
        mock_db.execute.side_effect = ProgrammingError(
            statement="SELECT ... FROM token_usage",
            params={},
            orig=Exception("relation \"token_usage\" does not exist"),
        )

        resp = admin_client.get("/admin/orders/order-003/token-usage-detail")

        assert resp.status_code == 404
        assert "No token usage data" in resp.json()["detail"]


class TestTranslationVersions:
    def _version_row(self, v=1, source="nmt", label=None):
        from datetime import datetime, timezone
        row = MagicMock()
        row.id = f"ver-{v:03d}"
        row.version = v
        row.label = label
        row.source = source
        row.created_at = datetime.now(timezone.utc)
        row.segment_count = 10
        row.gcs_path = f"pipeline/order-001/versions/v{v}.json"
        row.created_by_email = "admin@ots.tw"
        row._mapping = {
            "id": row.id,
            "version": row.version,
            "label": row.label,
            "source": row.source,
            "created_at": row.created_at,
            "segment_count": row.segment_count,
            "gcs_path": row.gcs_path,
            "created_by_email": row.created_by_email,
        }
        return row

    def _next_ver_row(self, next_ver=1):
        row = MagicMock()
        row.next_ver = next_ver
        return row

    def _insert_return_row(self, v=1, source="manual"):
        from datetime import datetime, timezone
        row = MagicMock()
        row.id = f"ver-{v:03d}"
        row.version = v
        row.label = None
        row.created_by = "admin-db-id-001"
        row.created_at = datetime.now(timezone.utc)
        row.gcs_path = f"pipeline/order-001/versions/v{v}.json"
        row.segment_count = 10
        row.source = source
        row._mapping = {
            "id": row.id,
            "version": row.version,
            "label": row.label,
            "created_by": row.created_by,
            "created_at": row.created_at,
            "gcs_path": row.gcs_path,
            "segment_count": row.segment_count,
            "source": row.source,
        }
        return row

    def _make_handler(self, results_by_keyword):
        def handler(*args, **kwargs):
            sql = str(args[0]).lower() if args else ''
            for keyword, result in results_by_keyword.items():
                if keyword in sql:
                    return result
            r = MagicMock()
            r.fetchone.return_value = None
            r.fetchall.return_value = []
            return r
        return handler

    # ── List Versions ──────────────────────────────────────────────────────
    def test_list_versions_empty(self, admin_client, mock_db):
        list_res = MagicMock()
        list_res.fetchall.return_value = []
        mock_db.execute.return_value = list_res

        resp = admin_client.get("/admin/orders/order-001/versions")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_versions_success(self, admin_client, mock_db):
        v1 = self._version_row(1)
        v2 = self._version_row(2)
        list_res = MagicMock()
        list_res.fetchall.return_value = [v2, v1]
        mock_db.execute.return_value = list_res

        resp = admin_client.get("/admin/orders/order-001/versions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["version"] == 2
        assert data[1]["version"] == 1

    # ── Save Version ───────────────────────────────────────────────────────
    def test_save_version_no_translations_returns_404(self, admin_client, mock_db):
        with patch("routers.admin.svc_save_version", new_callable=AsyncMock) as mock_save:
            mock_save.return_value = None
            resp = admin_client.post("/admin/orders/order-001/versions")
        assert resp.status_code == 404
        assert "No translations.json found" in resp.json()["detail"]

    def test_save_version_success(self, admin_client, mock_db):
        version = {"id": "ver-001", "version": 1, "source": "manual"}
        with patch("routers.admin.svc_save_version", new_callable=AsyncMock) as mock_save:
            mock_save.return_value = version
            resp = admin_client.post("/admin/orders/order-001/versions")
        assert resp.status_code == 200
        assert resp.json()["version"] == 1

    def test_save_version_with_label(self, admin_client, mock_db):
        version = {"id": "ver-001", "version": 1, "label": "before edit", "source": "manual"}
        with patch("routers.admin.svc_save_version", new_callable=AsyncMock) as mock_save:
            mock_save.return_value = version
            resp = admin_client.post("/admin/orders/order-001/versions?label=before+edit")
        assert resp.status_code == 200
        assert resp.json()["label"] == "before edit"

    # ── Restore Version ────────────────────────────────────────────────────
    def test_restore_version_not_found_returns_404(self, admin_client, mock_db):
        with patch("routers.admin.svc_restore_version", new_callable=AsyncMock) as mock_restore:
            mock_restore.return_value = None
            resp = admin_client.post("/admin/orders/order-001/versions/ver-999/restore")
        assert resp.status_code == 404

    def test_restore_version_success(self, admin_client, mock_db):
        version = {"id": "ver-002", "version": 2, "source": "restored"}
        with patch("routers.admin.svc_restore_version", new_callable=AsyncMock) as mock_restore:
            mock_restore.return_value = version
            resp = admin_client.post("/admin/orders/order-001/versions/ver-001/restore")
        assert resp.status_code == 200
        assert resp.json()["source"] == "restored"
        assert resp.json()["version"] == 2

    # ── Diff Versions ──────────────────────────────────────────────────────
    def test_diff_versions_success(self, admin_client, mock_db):
        diff_result = {"changed": [], "added": [], "removed": []}
        with patch("routers.admin.svc_diff_versions", new_callable=AsyncMock) as mock_diff:
            mock_diff.return_value = diff_result
            resp = admin_client.get(
                "/admin/orders/order-001/versions/11111111-1111-1111-1111-111111111001/diff",
                params={"against": "11111111-1111-1111-1111-111111111002"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "changed" in data

    def test_diff_versions_auto_latest(self, admin_client, mock_db):
        """When against is omitted, it should use the latest version."""
        latest = MagicMock()
        latest.id = "11111111-1111-1111-1111-111111111002"
        latest_res = MagicMock()
        latest_res.fetchone.return_value = latest

        diff_result = {"changed": [], "added": [], "removed": []}
        mock_db.execute.return_value = latest_res

        with patch("routers.admin.svc_diff_versions", new_callable=AsyncMock) as mock_diff:
            mock_diff.return_value = diff_result
            resp = admin_client.get(
                "/admin/orders/order-001/versions/11111111-1111-1111-1111-111111111001/diff",
            )
        assert resp.status_code == 200

    def test_diff_versions_no_other_version_returns_404(self, admin_client, mock_db):
        none_res = MagicMock()
        none_res.fetchone.return_value = None
        mock_db.execute.return_value = none_res

        resp = admin_client.get(
            "/admin/orders/order-001/versions/11111111-1111-1111-1111-111111111001/diff",
        )
        assert resp.status_code == 404
        assert "No other version" in resp.json()["detail"]

    def test_diff_versions_not_found_returns_404(self, admin_client, mock_db):
        diff_res = MagicMock()
        diff_res.fetchall.return_value = []
        mock_db.execute.return_value = diff_res

        with patch("routers.admin.svc_diff_versions") as mock_diff:
            mock_diff.side_effect = ValueError("One or both versions not found")
            resp = admin_client.get(
                "/admin/orders/order-001/versions/11111111-1111-1111-1111-111111111999/diff",
                params={"against": "11111111-1111-1111-1111-111111111001"},
            )
        assert resp.status_code == 404

    def test_diff_live_not_found_returns_404(self, admin_client, mock_db):
        with patch("core.storage.read_temp_json") as mock_read:
            mock_read.return_value = None
            resp = admin_client.get(
                "/admin/orders/order-001/versions/live/diff",
                params={"against": "11111111-1111-1111-1111-111111111001"},
            )
        assert resp.status_code == 404

    def test_diff_live_version_not_found_returns_404(self, admin_client, mock_db):
        with patch("core.storage.read_temp_json") as mock_read:
            mock_read.return_value = [{"index": 0, "translated": "Hello"}]
            none_res = MagicMock()
            none_res.fetchone.return_value = None
            mock_db.execute.return_value = none_res
            resp = admin_client.get(
                "/admin/orders/order-001/versions/live/diff",
                params={"against": "11111111-1111-1111-1111-111111111999"},
            )
        assert resp.status_code == 404
