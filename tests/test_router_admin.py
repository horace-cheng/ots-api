import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.database import get_db
from routers.auth import get_admin_user
from routers.admin import router
from tests.factories import MOCK_ADMIN_USER


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


class TestAdminGetOrder:
    def _order_row(self, qa_result=None, gcs_output_path=None):
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
            "notes":           None,
            "created_at":      datetime(2026, 4, 27, tzinfo=timezone.utc),
            "deadline_at":     None,
            "delivered_at":    None,
            "gcs_output_path": gcs_output_path,
            "payment_status":  "paid",
            "invoice_no":      None,
            "qa_result":       qa_result,
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

        resp = admin_client.get("/admin/users")
        assert resp.status_code == 200
        assert resp.json() == []

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
            "is_admin":     False,
            "admin_role":   None,
        }
        mock_db.execute.return_value.fetchall.return_value = [row]

        resp = admin_client.get("/admin/users")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["email"] == "user@ots.tw"
        assert data[0]["is_admin"] is False


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
            "proofreader_id": None,
            "status": "editing",
            "assigned_at": now,
            "editor_submitted_at": None,
            "proofread_submitted_at": None,
        }
        mock_db.execute.return_value.fetchone.return_value = row

        resp = admin_client.patch(
            "/admin/assignments/ORDER-001",
            json={"editor_id": "editor-001"},
        )
        assert resp.status_code == 200
        assert resp.json()["editor_id"] == "editor-001"
