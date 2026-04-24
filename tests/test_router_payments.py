import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.database import get_db
from routers.payments import router
from services.payment.base import PaymentStatus, WebhookPayload


def _webhook_payload(
    status: PaymentStatus = PaymentStatus.PAID,
    order_id: str = "ORDER-001",
) -> WebhookPayload:
    return WebhookPayload(
        order_id=order_id,
        gateway_trade_no="ECPAY2026001",
        status=status,
        amount_ntd=1500,
        paid_at="2026/04/24 15:00:00",
        raw={},
    )


@pytest.fixture
def payments_client(mock_db):
    app = FastAPI()
    app.include_router(router)

    async def override_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


class TestPaymentWebhook:
    def test_invalid_signature_returns_plain_text_0_error(self, payments_client):
        mock_gw = MagicMock()
        mock_gw.parse_webhook.side_effect = ValueError("bad mac")

        with patch("routers.payments.get_payment_gateway", return_value=mock_gw):
            resp = payments_client.post("/payments/webhook", data={"foo": "bar"})

        assert resp.text == "0|Error"
        assert "text/plain" in resp.headers["content-type"]

    def test_non_paid_returns_1_ok_with_no_db_write(self, payments_client, mock_db):
        mock_gw = MagicMock()
        mock_gw.parse_webhook.return_value = _webhook_payload(status=PaymentStatus.PENDING)

        with patch("routers.payments.get_payment_gateway", return_value=mock_gw):
            resp = payments_client.post("/payments/webhook", data={})

        assert resp.text == "1|OK"
        mock_db.commit.assert_not_called()

    def test_failed_status_returns_1_ok_with_no_db_write(self, payments_client, mock_db):
        mock_gw = MagicMock()
        mock_gw.parse_webhook.return_value = _webhook_payload(status=PaymentStatus.FAILED)

        with patch("routers.payments.get_payment_gateway", return_value=mock_gw):
            resp = payments_client.post("/payments/webhook", data={})

        assert resp.text == "1|OK"
        mock_db.commit.assert_not_called()

    def test_paid_updates_db_and_returns_1_ok(self, payments_client, mock_db):
        mock_gw = MagicMock()
        mock_gw.parse_webhook.return_value = _webhook_payload(order_id="ORDER-XYZ")

        with patch("routers.payments.get_payment_gateway", return_value=mock_gw), \
             patch("routers.payments.trigger_pipeline", new_callable=AsyncMock):
            resp = payments_client.post("/payments/webhook", data={})

        assert resp.text == "1|OK"
        assert mock_db.execute.call_count >= 2   # UPDATE payments + UPDATE orders
        mock_db.commit.assert_called()

    def test_paid_triggers_pipeline_with_order_id(self, payments_client, mock_db):
        mock_gw = MagicMock()
        mock_gw.parse_webhook.return_value = _webhook_payload(order_id="ORDER-XYZ")

        with patch("routers.payments.get_payment_gateway", return_value=mock_gw), \
             patch("routers.payments.trigger_pipeline", new_callable=AsyncMock) as mock_trigger:
            payments_client.post("/payments/webhook", data={})

        mock_trigger.assert_awaited_once_with("ORDER-XYZ")

    def test_b2b_client_skips_auto_invoice(self, payments_client, mock_db):
        mock_gw = MagicMock()
        mock_gw.parse_webhook.return_value = _webhook_payload()
        row = MagicMock()
        row.client_type = "b2b"
        mock_db.execute.return_value.fetchone.return_value = row

        with patch("routers.payments.get_payment_gateway", return_value=mock_gw), \
             patch("routers.payments.trigger_pipeline", new_callable=AsyncMock):
            payments_client.post("/payments/webhook", data={})

        mock_gw.issue_invoice.assert_not_called()

    def test_invoice_failure_is_non_fatal(self, payments_client, mock_db):
        mock_gw = MagicMock()
        mock_gw.parse_webhook.return_value = _webhook_payload()
        row = MagicMock()
        row.client_type = "b2c"
        row.invoice_carrier = "/ABC1234"
        mock_db.execute.return_value.fetchone.return_value = row
        mock_gw.issue_invoice.side_effect = Exception("invoice API down")

        with patch("routers.payments.get_payment_gateway", return_value=mock_gw), \
             patch("routers.payments.trigger_pipeline", new_callable=AsyncMock):
            resp = payments_client.post("/payments/webhook", data={})

        assert resp.text == "1|OK"

    def test_no_user_row_skips_invoice_gracefully(self, payments_client, mock_db):
        mock_gw = MagicMock()
        mock_gw.parse_webhook.return_value = _webhook_payload()
        mock_db.execute.return_value.fetchone.return_value = None

        with patch("routers.payments.get_payment_gateway", return_value=mock_gw), \
             patch("routers.payments.trigger_pipeline", new_callable=AsyncMock):
            resp = payments_client.post("/payments/webhook", data={})

        assert resp.text == "1|OK"
        mock_gw.issue_invoice.assert_not_called()
