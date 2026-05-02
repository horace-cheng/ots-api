import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.database import get_db
from routers.auth import get_current_user
from routers.users import router

MOCK_USER = {
    "uid": "user-uid",
    "email": "user@ots.tw",
    "user_id": "user-db-id",
    "client_type": "b2c",
    "is_editor": False
}

@pytest.fixture
def user_client(mock_db):
    app = FastAPI()
    app.include_router(router)

    async def override_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: MOCK_USER

    return TestClient(app)

class TestUsersMe:
    def test_get_me_success(self, user_client, mock_db):
        row = MagicMock()
        row._mapping = {
            "id": "user-db-id",
            "uid_firebase": "user-uid",
            "client_type": "b2c",
            "company_name": None,
            "tax_id": None,
            "invoice_carrier": None,
            "created_at": datetime.now(timezone.utc),
            "roles": ["editor"],
            "languages": [{"source_lang": "zh-tw", "target_lang": "en"}]
        }
        mock_db.execute.return_value.fetchone.return_value = row
        
        resp = user_client.get("/users/me")
        assert resp.status_code == 200
        data = resp.json()
        assert data["uid_firebase"] == "user-uid"
        assert data["is_admin"] is False
        assert data["is_editor"] is True
        assert data["languages"][0]["source_lang"] == "zh-tw"

    def test_get_me_no_roles(self, user_client, mock_db):
        """Regression: roles=None from DB (user with no assigned roles) must not crash.
        Previously caused: pydantic ValidationError 'roles: Input should be a valid list'"""
        row = MagicMock()
        row._mapping = {
            "id": "user-db-id",
            "uid_firebase": "user-uid",
            "client_type": "b2c",
            "company_name": None,
            "tax_id": None,
            "invoice_carrier": None,
            "created_at": datetime.now(timezone.utc),
            "roles": None,      # array_agg returns NULL when no roles exist
            "languages": None,  # same for languages
        }
        mock_db.execute.return_value.fetchone.return_value = row

        resp = user_client.get("/users/me")
        assert resp.status_code == 200
        data = resp.json()
        assert data["roles"] == []
        assert data["languages"] == []
        assert data["is_admin"] is False
        assert data["is_editor"] is False
        assert data["is_qa"] is False

    def test_get_me_admin_role(self, user_client, mock_db):
        row = MagicMock()
        row._mapping = {
            "id": "user-db-id",
            "uid_firebase": "user-uid",
            "client_type": "b2c",
            "company_name": None,
            "tax_id": None,
            "invoice_carrier": None,
            "created_at": datetime.now(timezone.utc),
            "roles": ["admin", "editor"],
            "languages": [],
        }
        mock_db.execute.return_value.fetchone.return_value = row

        resp = user_client.get("/users/me")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_admin"] is True
        assert data["is_editor"] is True
        assert data["is_qa"] is False

    def test_get_me_qa_role(self, user_client, mock_db):
        row = MagicMock()
        row._mapping = {
            "id": "user-db-id",
            "uid_firebase": "user-uid",
            "client_type": "b2c",
            "company_name": None,
            "tax_id": None,
            "invoice_carrier": None,
            "created_at": datetime.now(timezone.utc),
            "roles": ["qa"],
            "languages": [{"source_lang": "zh-tw", "target_lang": "en"}],
        }
        mock_db.execute.return_value.fetchone.return_value = row

        resp = user_client.get("/users/me")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_qa"] is True
        assert data["is_editor"] is False
        assert data["is_admin"] is False

class TestInvitations:
    def test_create_invitation_editor_by_admin(self, user_client, mock_db):
        row = MagicMock()
        row._mapping = {
            "id": "invite-id",
            "email": "new@ots.tw",
            "role": "editor",
            "token": "token-123",
            "status": "pending",
            "created_at": datetime.now(timezone.utc),
            "expires_at": datetime.now(timezone.utc)
        }
        mock_db.execute.return_value.fetchone.return_value = row

        user_client.app.dependency_overrides[get_current_user] = lambda: {**MOCK_USER, "is_admin": True}
        try:
            resp = user_client.post("/users/invite", json={"email": "new@ots.tw", "role": "editor"})
        finally:
            user_client.app.dependency_overrides[get_current_user] = lambda: MOCK_USER

        assert resp.status_code == 200
        assert resp.json()["token"] == "token-123"

    def test_create_invitation_qa_by_editor(self, user_client, mock_db):
        """Editor can invite QA."""
        row = MagicMock()
        row._mapping = {
            "id": "invite-id-2",
            "email": "qa@ots.tw",
            "role": "qa",
            "token": "token-qa",
            "status": "pending",
            "created_at": datetime.now(timezone.utc),
            "expires_at": datetime.now(timezone.utc)
        }
        mock_db.execute.return_value.fetchone.return_value = row

        user_client.app.dependency_overrides[get_current_user] = lambda: {**MOCK_USER, "is_editor": True}
        try:
            resp = user_client.post("/users/invite", json={"email": "qa@ots.tw", "role": "qa"})
        finally:
            user_client.app.dependency_overrides[get_current_user] = lambda: MOCK_USER

        assert resp.status_code == 200
        assert resp.json()["role"] == "qa"

    def test_create_invitation_editor_by_non_admin_returns_403(self, user_client, mock_db):
        """Regular user (no admin flag) cannot invite editors."""
        resp = user_client.post("/users/invite", json={"email": "x@ots.tw", "role": "editor"})
        assert resp.status_code == 403

    def test_create_invitation_invalid_role_returns_400(self, user_client, mock_db):
        """Only 'editor' and 'qa' are valid invitation roles."""
        user_client.app.dependency_overrides[get_current_user] = lambda: {**MOCK_USER, "is_admin": True}
        try:
            resp = user_client.post("/users/invite", json={"email": "x@ots.tw", "role": "superadmin"})
        finally:
            user_client.app.dependency_overrides[get_current_user] = lambda: MOCK_USER
        assert resp.status_code == 400

    def test_accept_invitation_success(self, user_client, mock_db):
        from datetime import timedelta
        invite_row = MagicMock()
        invite_row.id = "invite-id"
        invite_row.role = "qa"
        invite_row.status = "pending"
        invite_row.expires_at = datetime.now(timezone.utc) + timedelta(days=1)

        mock_db.execute.return_value.fetchone.return_value = invite_row

        resp = user_client.post("/users/accept-invite", json={"token": "token-123"})
        assert resp.status_code == 200
        assert "You are now a qa" in resp.json()["message"]
        mock_db.commit.assert_awaited()

    def test_accept_expired_invitation_returns_400(self, user_client, mock_db):
        from datetime import timedelta
        invite_row = MagicMock()
        invite_row.id = "invite-id"
        invite_row.role = "editor"
        invite_row.status = "pending"
        invite_row.expires_at = datetime.now(timezone.utc) - timedelta(days=1)  # expired

        mock_db.execute.return_value.fetchone.return_value = invite_row

        resp = user_client.post("/users/accept-invite", json={"token": "expired-token"})
        assert resp.status_code == 400
        assert "expired" in resp.json()["detail"].lower()

    def test_accept_nonexistent_invitation_returns_404(self, user_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = None

        resp = user_client.post("/users/accept-invite", json={"token": "bad-token"})
        assert resp.status_code == 404
