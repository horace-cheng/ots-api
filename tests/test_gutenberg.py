import pytest
from unittest.mock import AsyncMock, MagicMock, patch
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

class TestImportGutenbergBook:
    def test_import_success(self, admin_client, mock_db):
        order_id = "order-gutenberg-1342"
        mock_order_row = MagicMock()
        mock_order_row.__getitem__.side_effect = lambda i: order_id if i == 0 else None
        
        mock_db.execute.side_effect = [
            MagicMock(), # INSERT
            MagicMock(fetchone=lambda: mock_order_row), # SELECT id
            MagicMock(), # UPDATE notes
        ]
        
        with patch("routers.admin.trigger_pipeline", new_callable=AsyncMock) as mock_trigger:
            resp = admin_client.post("/admin/gutenberg/1342")
        
        assert resp.status_code == 200
        assert "Gutenberg book 1342 import triggered" in resp.json()["message"]
        assert order_id in resp.json()["message"]
        mock_trigger.assert_awaited_once_with(order_id)
        
    def test_import_db_error_returns_500(self, admin_client, mock_db):
        mock_db.execute.side_effect = [
            MagicMock(), # INSERT
            MagicMock(fetchone=lambda: None), # SELECT id fails
        ]
        
        resp = admin_client.post("/admin/gutenberg/1342")
        assert resp.status_code == 500
        # The error message should be in "detail" for 500 errors, not "message"
        assert "Failed to create Gutenberg order" in resp.json()["detail"]