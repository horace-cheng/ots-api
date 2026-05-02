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

class TestInvitations:
    def test_create_invitation_success(self, user_client, mock_db):
        # We assume the mock user is admin for this test
        # In a real test we might want to override dependency per test
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
        
        # We need to ensure get_current_user returns is_admin=True
        user_client.app.dependency_overrides[get_current_user] = lambda: {**MOCK_USER, "is_admin": True}
        try:
            resp = user_client.post("/users/invite", json={"email": "new@ots.tw", "role": "editor"})
        finally:
            user_client.app.dependency_overrides[get_current_user] = lambda: MOCK_USER
    
        assert resp.status_code == 200
        assert resp.json()["token"] == "token-123"

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
