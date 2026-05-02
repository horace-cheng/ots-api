import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient

from core.database import get_db
from routers.auth import get_current_user, get_admin_user

DECODED_TOKEN = {
    "uid": "firebase-uid-001",
    "email": "user@ots.tw",
    "email_verified": True,
}


@pytest.fixture
def client(mock_db):
    app = FastAPI()

    async def override_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_db

    @app.get("/me")
    async def me(user: dict = Depends(get_current_user)):
        return user

    @app.get("/admin-only")
    async def admin_only(user: dict = Depends(get_admin_user)):
        return user

    return TestClient(app)


class TestGetCurrentUser:
    def _user_row(self, disabled=False):
        row = MagicMock()
        row.id = "db-id-001"
        row.client_type = "b2c"
        row.disabled = disabled
        return row

    def test_valid_token_returns_user(self, client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = self._user_row()

        with patch("routers.auth.verify_firebase_token", return_value=DECODED_TOKEN):
            resp = client.get("/me", headers={"Authorization": "Bearer valid-token"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["uid"] == "firebase-uid-001"
        assert data["email"] == "user@ots.tw"
        assert data["client_type"] == "b2c"

    def test_new_user_inserted_on_first_login(self, client, mock_db):
        new_row = MagicMock()
        new_row.id = "new-db-id"
        new_row.client_type = "b2c"
        new_row.disabled = False
        # First SELECT returns None, second SELECT (after INSERT) returns the new row
        mock_db.execute.return_value.fetchone.side_effect = [None, new_row]

        with patch("routers.auth.verify_firebase_token", return_value=DECODED_TOKEN):
            resp = client.get("/me", headers={"Authorization": "Bearer valid-token"})

        assert resp.status_code == 200
        assert mock_db.execute.call_count >= 2
        mock_db.commit.assert_called()

    def test_disabled_account_returns_403(self, client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = self._user_row(disabled=True)

        with patch("routers.auth.verify_firebase_token", return_value=DECODED_TOKEN):
            resp = client.get("/me", headers={"Authorization": "Bearer valid-token"})

        assert resp.status_code == 403
        assert "disabled" in resp.json()["detail"]

    def test_email_synced_on_subsequent_login(self, client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = self._user_row()

        with patch("routers.auth.verify_firebase_token", return_value=DECODED_TOKEN):
            resp = client.get("/me", headers={"Authorization": "Bearer valid-token"})

        assert resp.status_code == 200
        # email UPDATE + commit should have been called
        mock_db.commit.assert_called()

    def test_missing_auth_header_401(self, client):
        resp = client.get("/me")
        assert resp.status_code == 401

    def test_invalid_format_not_bearer_401(self, client):
        resp = client.get("/me", headers={"Authorization": "Token not-a-bearer"})
        assert resp.status_code == 401

    def test_expired_token_401(self, client):
        with patch("routers.auth.verify_firebase_token", side_effect=ValueError("Token expired")):
            resp = client.get("/me", headers={"Authorization": "Bearer expired"})
        assert resp.status_code == 401
        assert "Token expired" in resp.json()["detail"]

    def test_invalid_token_401(self, client):
        with patch("routers.auth.verify_firebase_token", side_effect=ValueError("Invalid token")):
            resp = client.get("/me", headers={"Authorization": "Bearer garbage"})
        assert resp.status_code == 401


class TestGetAdminUser:
    def _user_row(self):
        row = MagicMock()
        row.id = "admin-db-id"
        row.client_type = "b2c"
        row.disabled = False
        return row

    def _admin_row(self, active=True):
        row = MagicMock()
        row.id = "admin-table-id"
        row.role = "admin"
        row.active = active
        return row

    def test_valid_admin_passes(self, client, mock_db):
        mock_db.execute.return_value.fetchone.side_effect = [
            self._user_row(), self._admin_row(active=True)
        ]
        with patch("routers.auth.verify_firebase_token", return_value=DECODED_TOKEN):
            resp = client.get("/admin-only", headers={"Authorization": "Bearer admin-token"})
        assert resp.status_code == 200

    def test_uid_not_in_admin_users_403(self, client, mock_db):
        mock_db.execute.return_value.fetchone.side_effect = [
            self._user_row(), None
        ]
        with patch("routers.auth.verify_firebase_token", return_value=DECODED_TOKEN):
            resp = client.get("/admin-only", headers={"Authorization": "Bearer user-token"})
        assert resp.status_code == 403
        assert "Admin access required" in resp.json()["detail"]

    def test_disabled_admin_403(self, client, mock_db):
        mock_db.execute.return_value.fetchone.side_effect = [
            self._user_row(), self._admin_row(active=False)
        ]
        with patch("routers.auth.verify_firebase_token", return_value=DECODED_TOKEN):
            resp = client.get("/admin-only", headers={"Authorization": "Bearer token"})
        assert resp.status_code == 403
        assert "disabled" in resp.json()["detail"]
