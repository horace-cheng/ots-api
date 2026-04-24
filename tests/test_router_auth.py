import os
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
    def test_valid_token_returns_user(self, client, mock_db):
        user_row = MagicMock()
        user_row.id = "db-id-001"
        user_row.client_type = "b2c"
        mock_db.execute.return_value.fetchone.return_value = user_row

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
        # First SELECT returns None, second SELECT (after INSERT) returns the new row
        mock_db.execute.return_value.fetchone.side_effect = [None, new_row]

        with patch("routers.auth.verify_firebase_token", return_value=DECODED_TOKEN):
            resp = client.get("/me", headers={"Authorization": "Bearer valid-token"})

        assert resp.status_code == 200
        assert mock_db.execute.call_count >= 2
        mock_db.commit.assert_called()

    def test_missing_auth_header_422(self, client):
        resp = client.get("/me")
        assert resp.status_code == 422

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
    def _setup_user(self, mock_db, uid="admin-uid-001"):
        row = MagicMock()
        row.id = "admin-db-id"
        row.client_type = "b2c"
        mock_db.execute.return_value.fetchone.return_value = row
        return {**DECODED_TOKEN, "uid": uid}

    def test_uid_in_allowlist_passes(self, client, mock_db):
        token = self._setup_user(mock_db, uid="admin-uid-001")
        with patch("routers.auth.verify_firebase_token", return_value=token), \
             patch.dict(os.environ, {"ADMIN_UIDS": "admin-uid-001,admin-uid-002"}):
            resp = client.get("/admin-only", headers={"Authorization": "Bearer admin-token"})
        assert resp.status_code == 200

    def test_uid_not_in_allowlist_403(self, client, mock_db):
        token = self._setup_user(mock_db, uid="regular-uid")
        with patch("routers.auth.verify_firebase_token", return_value=token), \
             patch.dict(os.environ, {"ADMIN_UIDS": "admin-uid-001"}):
            resp = client.get("/admin-only", headers={"Authorization": "Bearer user-token"})
        assert resp.status_code == 403
        assert "Admin access required" in resp.json()["detail"]

    def test_empty_admin_uids_403(self, client, mock_db):
        token = self._setup_user(mock_db)
        with patch("routers.auth.verify_firebase_token", return_value=token), \
             patch.dict(os.environ, {"ADMIN_UIDS": ""}):
            resp = client.get("/admin-only", headers={"Authorization": "Bearer token"})
        assert resp.status_code == 403
