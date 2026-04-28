import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.database import get_db
from routers.auth import get_current_user
from routers.orders import router
from tests.factories import MOCK_USER


@pytest.fixture
def mock_gateway():
    gw = MagicMock()
    result = MagicMock()
    result.gateway_trade_no = "TRADE-001"
    result.payment_url = "https://payment.ecpay.com.tw/checkout"
    gw.create_payment.return_value = result
    return gw


@pytest.fixture
def orders_client(mock_db, mock_gateway):
    app = FastAPI()
    app.include_router(router)

    async def override_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: MOCK_USER

    with patch("routers.orders.get_payment_gateway", return_value=mock_gateway):
        yield TestClient(app)


class TestCreateOrder:
    def test_returns_201_with_payment_url(self, orders_client):
        resp = orders_client.post("/orders", json={
            "track_type": "fast",
            "source_lang": "zh-tw",
            "target_lang": "en",
            "word_count": 1000,
            "price_ntd": 2000,
        })
        assert resp.status_code == 201
        data = resp.json()
        assert "order_id" in data
        assert data["status"] == "pending_payment"
        assert data["payment_url"] == "https://payment.ecpay.com.tw/checkout"

    def test_price_recalculated_server_side(self, orders_client):
        resp = orders_client.post("/orders", json={
            "track_type": "fast",
            "source_lang": "zh-tw",
            "target_lang": "en",
            "word_count": 1000,
            "price_ntd": 99,  # ignored — server recalculates
        })
        assert resp.status_code == 201
        assert resp.json()["price_ntd"] == 2000  # max(1000*2, 2000)

    def test_literary_track_price(self, orders_client):
        resp = orders_client.post("/orders", json={
            "track_type": "literary",
            "source_lang": "tai-lo",
            "target_lang": "zh-tw",
            "word_count": 5000,
            "price_ntd": 1,
        })
        assert resp.status_code == 201
        assert resp.json()["price_ntd"] == 30000  # 5000 * 6

    def test_title_stored_when_provided(self, orders_client):
        resp = orders_client.post("/orders", json={
            "track_type": "fast",
            "source_lang": "zh-tw",
            "target_lang": "en",
            "word_count": 1000,
            "title": "My Translation Project",
        })
        assert resp.status_code == 201

    def test_title_optional_when_not_provided(self, orders_client):
        resp = orders_client.post("/orders", json={
            "track_type": "fast",
            "source_lang": "zh-tw",
            "target_lang": "en",
            "word_count": 1000,
        })
        assert resp.status_code == 201

    def test_same_lang_returns_422(self, orders_client):
        resp = orders_client.post("/orders", json={
            "track_type": "fast",
            "source_lang": "zh-tw",
            "target_lang": "zh-tw",
            "word_count": 1000,
            "price_ntd": 2000,
        })
        assert resp.status_code == 422

    def test_zero_word_count_returns_422(self, orders_client):
        resp = orders_client.post("/orders", json={
            "track_type": "fast",
            "source_lang": "zh-tw",
            "target_lang": "en",
            "word_count": 0,
            "price_ntd": 2000,
        })
        assert resp.status_code == 422


class TestCancelOrder:
    def test_cancels_pending_payment_order(self, orders_client, mock_db):
        row = MagicMock()
        row.id = "order-001"
        row.status = "pending_payment"
        mock_db.execute.return_value.fetchone.return_value = row

        resp = orders_client.delete("/orders/order-001")
        assert resp.status_code == 200
        assert resp.json()["message"] == "Order cancelled"

    def test_cannot_cancel_paid_order(self, orders_client, mock_db):
        row = MagicMock()
        row.id = "order-001"
        row.status = "paid"
        mock_db.execute.return_value.fetchone.return_value = row

        resp = orders_client.delete("/orders/order-001")
        assert resp.status_code == 400
        assert "Cannot cancel" in resp.json()["detail"]

    def test_cannot_cancel_processing_order(self, orders_client, mock_db):
        row = MagicMock()
        row.id = "order-001"
        row.status = "processing"
        mock_db.execute.return_value.fetchone.return_value = row

        resp = orders_client.delete("/orders/order-001")
        assert resp.status_code == 400

    def test_order_not_found_404(self, orders_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = None
        resp = orders_client.delete("/orders/nonexistent")
        assert resp.status_code == 404


class TestGetOrder:
    def test_order_not_found_404(self, orders_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = None
        resp = orders_client.get("/orders/nonexistent")
        assert resp.status_code == 404

    def test_success_returns_order_detail(self, orders_client, mock_db):
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
        mock_db.execute.return_value.fetchone.return_value = row

        resp = orders_client.get("/orders/order-001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "order-001"
        assert data["status"] == "paid"
        assert data["track_type"] == "fast"


class TestListOrders:
    def test_empty_list_returns_zero_total(self, orders_client, mock_db):
        mock_db.execute.return_value.fetchall.return_value = []
        mock_db.execute.return_value.scalar.return_value = 0

        resp = orders_client.get("/orders")
        assert resp.status_code == 200
        data = resp.json()
        assert data["orders"] == []
        assert data["total"] == 0

    def test_status_filter_accepted(self, orders_client, mock_db):
        mock_db.execute.return_value.fetchall.return_value = []
        mock_db.execute.return_value.scalar.return_value = 0

        resp = orders_client.get("/orders", params={"status": "paid"})
        assert resp.status_code == 200

    def test_track_type_filter_accepted(self, orders_client, mock_db):
        mock_db.execute.return_value.fetchall.return_value = []
        mock_db.execute.return_value.scalar.return_value = 0

        resp = orders_client.get("/orders", params={"track_type": "literary"})
        assert resp.status_code == 200

    def test_pagination_params_accepted(self, orders_client, mock_db):
        mock_db.execute.return_value.fetchall.return_value = []
        mock_db.execute.return_value.scalar.return_value = 0

        resp = orders_client.get("/orders", params={"limit": 5, "offset": 10})
        assert resp.status_code == 200
