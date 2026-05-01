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
            "is_editor": False,
            "is_admin": False,
            "created_at": datetime.now(timezone.utc)
        }
        mock_db.execute.return_value.fetchone.return_value = row
        
        resp = user_client.get("/users/me")
        assert resp.status_code == 200
        data = resp.json()
        assert data["uid_firebase"] == "user-uid"
        assert data["is_admin"] is False
        assert data["is_editor"] is False
