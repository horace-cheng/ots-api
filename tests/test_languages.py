import pytest
from unittest.mock import MagicMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.database import get_db
from routers.auth import get_admin_user
from routers.languages import router, admin_router

@pytest.fixture
def languages_client(mock_db):
    app = FastAPI()
    app.include_router(router)
    app.include_router(admin_router)

    async def override_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_admin_user] = lambda: {"uid": "admin-001", "is_admin": True}

    return TestClient(app)

class TestLanguages:
    def test_list_active_languages(self, languages_client, mock_db):
        row = MagicMock()
        row._mapping = {
            "id": 1,
            "code": "zh-tw",
            "label_zh": "繁體中文",
            "label_en": "Traditional Chinese",
            "direction": "both",
            "is_active": True,
            "sort_order": 10,
            "price_multiplier": 1.0,
            "created_at": "2026-05-26T00:00:00Z"
        }
        res = MagicMock()
        res.fetchall.return_value = [row]
        mock_db.execute.return_value = res

        resp = languages_client.get("/languages")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["languages"]) == 1
        assert data["languages"][0]["code"] == "zh-tw"

    def test_list_supported_languages(self, languages_client):
        resp = languages_client.get("/languages/supported")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) > 0
        assert data[0]["code"] == "tai-lo"

    def test_admin_list_languages(self, languages_client, mock_db):
        res = MagicMock()
        res.fetchall.return_value = []
        mock_db.execute.return_value = res

        resp = languages_client.get("/admin/languages")
        assert resp.status_code == 200
        assert resp.json()["languages"] == []

    def test_admin_add_language_unsupported_code(self, languages_client, mock_db):
        resp = languages_client.post("/admin/languages", json={
            "code": "unsupported",
            "direction": "both"
        })
        assert resp.status_code == 400
        assert "not supported" in resp.json()["detail"]

    def test_admin_add_language_success(self, languages_client, mock_db):
        # mock check existing
        res_check = MagicMock()
        res_check.fetchall.return_value = []
        
        # mock insert
        row = MagicMock()
        row._mapping = {
            "id": 1,
            "code": "cs",
            "label_zh": "捷克語",
            "label_en": "Czech",
            "direction": "target",
            "is_active": True,
            "sort_order": 0,
            "price_multiplier": 1.0,
            "created_at": "2026-05-26T00:00:00Z"
        }
        res_insert = MagicMock()
        res_insert.fetchone.return_value = row

        mock_db.execute.side_effect = [res_check, res_insert]

        resp = languages_client.post("/admin/languages", json={
            "code": "cs",
            "direction": "target"
        })
        assert resp.status_code == 200
        assert resp.json()["code"] == "cs"

    def test_admin_delete_language_with_orders(self, languages_client, mock_db):
        lang_row = MagicMock()
        lang_row.code = "zh-tw"
        lang_row.direction = "both"
        res_check = MagicMock()
        res_check.fetchone.return_value = lang_row

        res_count = MagicMock()
        res_count.scalar.return_value = 1 # orders exist

        mock_db.execute.side_effect = [res_check, res_count]

        resp = languages_client.delete("/admin/languages/1")
        assert resp.status_code == 400
        assert "Cannot delete" in resp.json()["detail"]
