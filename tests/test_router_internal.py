import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.database import get_db
from routers.internal import router, verify_oidc_token

MOCK_CALLER = {"email": "ots-workflow-dev@ots-translation.iam.gserviceaccount.com", "sub": "12345"}


@pytest.fixture
def internal_client(mock_db):
    app = FastAPI()
    app.include_router(router)

    async def override_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[verify_oidc_token] = lambda: MOCK_CALLER

    yield TestClient(app)


def _order_row(order_id="order-001"):
    row = MagicMock()
    row._mapping = {
        "id":          order_id,
        "track_type":  "fast",
        "status":      "processing",
        "source_lang": "zh-tw",
        "target_lang": "en",
    }
    return row


# ── GET /internal/orders/{order_id} ──────────────────────────────────────────

class TestGetOrderInternal:
    def test_order_not_found_returns_404(self, internal_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = None

        resp = internal_client.get("/internal/orders/nonexistent")
        assert resp.status_code == 404

    def test_success_returns_order_fields(self, internal_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = _order_row()

        resp = internal_client.get("/internal/orders/order-001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "order-001"
        assert data["track_type"] == "fast"
        assert data["status"] == "processing"


# ── POST /internal/notify ─────────────────────────────────────────────────────

class TestNotifyInternal:
    def test_unknown_type_returns_ok(self, internal_client):
        resp = internal_client.post("/internal/notify", json={
            "type": "some_event",
            "order_id": "order-001",
        })
        assert resp.status_code == 200
        assert resp.json()["type"] == "some_event"

    def test_pipeline_error_updates_status(self, internal_client, mock_db):
        resp = internal_client.post("/internal/notify", json={
            "type": "pipeline_error",
            "order_id": "order-001",
        })
        assert resp.status_code == 200
        mock_db.execute.assert_awaited()
        mock_db.commit.assert_awaited()

    def test_non_error_type_does_not_commit(self, internal_client, mock_db):
        resp = internal_client.post("/internal/notify", json={
            "type": "info",
            "order_id": "order-001",
        })
        assert resp.status_code == 200
        mock_db.commit.assert_not_awaited()


# ── GET /internal/qa-flags ────────────────────────────────────────────────────

class TestGetQaFlagsInternal:
    def test_returns_total_count(self, internal_client, mock_db):
        mock_db.execute.return_value.scalar.return_value = 3

        resp = internal_client.get("/internal/qa-flags", params={"order_id": "order-001"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert data["order_id"] == "order-001"
        assert data["flag_level"] is None

    def test_flag_level_filter(self, internal_client, mock_db):
        mock_db.execute.return_value.scalar.return_value = 1

        resp = internal_client.get("/internal/qa-flags", params={
            "order_id": "order-001",
            "flag_level": "must_fix",
        })
        assert resp.status_code == 200
        assert resp.json()["flag_level"] == "must_fix"

    def test_resolved_filter_defaults_false(self, internal_client, mock_db):
        mock_db.execute.return_value.scalar.return_value = 0

        resp = internal_client.get("/internal/qa-flags", params={"order_id": "order-001"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


# ── verify_oidc_token (auth dependency) ──────────────────────────────────────

class TestVerifyOidcToken:
    """Tests run directly against the dependency, no router needed."""

    @pytest.mark.asyncio
    async def test_missing_bearer_prefix_raises_401(self):
        from fastapi import HTTPException
        from routers.internal import verify_oidc_token

        with pytest.raises(HTTPException) as exc:
            await verify_oidc_token(authorization="Token abc123")
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_token_raises_401(self, monkeypatch):
        from fastapi import HTTPException
        from routers.internal import verify_oidc_token
        import routers.internal as internal_mod

        monkeypatch.setattr(
            internal_mod.google.oauth2.id_token,
            "verify_oauth2_token",
            lambda *a, **kw: (_ for _ in ()).throw(ValueError("bad token")),
        )

        with pytest.raises(HTTPException) as exc:
            await verify_oidc_token(authorization="Bearer bad.token.here")
        assert exc.value.status_code == 401
