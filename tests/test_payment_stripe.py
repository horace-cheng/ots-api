"""
Tests for Stripe payment gateway.
Uses mock objects to avoid real API calls.
"""

import pytest
from unittest.mock import patch, MagicMock

from services.payment.stripe import StripeGateway
from services.payment.base import (
    PaymentRequest, PaymentResult,
    WebhookPayload, InvoiceRequest,
    PaymentStatus, PaymentError, InvoiceError,
)


@pytest.fixture
def gateway():
    with patch.dict("os.environ", {
        "STRIPE_SECRET_KEY": "sk_test_xxxx",
        "STRIPE_WEBHOOK_SECRET": "whsec_xxxx",
    }):
        with patch("services.payment.stripe.settings") as mock_settings:
            mock_settings.stripe_secret_key = "sk_test_xxxx"
            mock_settings.stripe_webhook_secret = "whsec_xxxx"
            yield StripeGateway()


class TestCreatePayment:
    def test_success_returns_payment_result(self, gateway):
        fake_session = MagicMock()
        fake_session.id = "cs_test_abc123"
        fake_session.url = "https://checkout.stripe.com/pay/cs_test_abc123"
        fake_session.to_dict.return_value = {"id": "cs_test_abc123"}

        with patch("stripe.checkout.Session.create", return_value=fake_session):
            result = gateway.create_payment(PaymentRequest(
                order_id="order-001",
                amount_ntd=50000,
                description="OTS 翻譯服務 (1000字)",
                return_url="https://ots.tw/orders/order-001",
                notify_url="https://ots.tw/payments/webhook",
            ))

        assert isinstance(result, PaymentResult)
        assert result.gateway_trade_no == "cs_test_abc123"
        assert result.payment_url == "https://checkout.stripe.com/pay/cs_test_abc123"

    def test_stripe_error_raises_payment_error(self, gateway):
        import stripe
        with patch("stripe.checkout.Session.create",
                   side_effect=stripe.error.StripeError("API error")):
            with pytest.raises(PaymentError, match="API error"):
                gateway.create_payment(PaymentRequest(
                    order_id="order-001",
                    amount_ntd=50000,
                    description="test",
                    return_url="https://ots.tw/orders/order-001",
                    notify_url="https://ots.tw/payments/webhook",
                ))

    def test_success_url_uses_return_url(self, gateway):
        fake_session = MagicMock()
        fake_session.id = "cs_test_456"
        fake_session.url = "https://checkout.stripe.com/pay/cs_test_456"
        fake_session.to_dict.return_value = {"id": "cs_test_456"}

        with patch("stripe.checkout.Session.create", return_value=fake_session) as mock_create:
            gateway.create_payment(PaymentRequest(
                order_id="order-002",
                amount_ntd=2000,
                description="test",
                return_url="https://ots.tw/orders/order-002",
                notify_url="https://ots.tw/payments/webhook",
            ))

        _, kwargs = mock_create.call_args
        assert kwargs["success_url"] == "https://ots.tw/orders/order-002"

    def test_order_id_in_metadata(self, gateway):
        fake_session = MagicMock()
        fake_session.id = "cs_test_789"
        fake_session.url = "https://checkout.stripe.com/pay/cs_test_789"
        fake_session.to_dict.return_value = {"id": "cs_test_789"}

        with patch("stripe.checkout.Session.create", return_value=fake_session) as mock_create:
            gateway.create_payment(PaymentRequest(
                order_id="order-003",
                amount_ntd=3000,
                description="test",
                return_url="https://ots.tw/orders/order-003",
                notify_url="https://ots.tw/payments/webhook",
            ))

        _, kwargs = mock_create.call_args
        assert kwargs["metadata"]["order_id"] == "order-003"


class TestParseWebhook:
    def test_completed_session_returns_paid(self, gateway):
        event = {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_completed",
                    "metadata": {"order_id": "order-001"},
                    "amount_total": 50000,
                }
            },
        }

        payload = gateway.parse_webhook(event)

        assert payload.status == PaymentStatus.PAID
        assert payload.order_id == "order-001"
        assert payload.gateway_trade_no == "cs_test_completed"
        assert payload.amount_ntd == 50000

    def test_other_event_returns_failed(self, gateway):
        event = {
            "type": "checkout.session.expired",
            "data": {"object": {}},
        }

        payload = gateway.parse_webhook(event)

        assert payload.status == PaymentStatus.FAILED


class TestIssueInvoice:
    def test_raises_invoice_error(self, gateway):
        with pytest.raises(InvoiceError, match="must be issued via external"):
            gateway.issue_invoice(InvoiceRequest(
                order_id="order-001",
                amount_ntd=50000,
                invoice_type="b2c_cloud",
                email="user@test.com",
            ))


class TestRefund:
    def test_success_returns_true(self, gateway):
        with patch("stripe.Refund.create", return_value=MagicMock()):
            result = gateway.refund("pi_test_001", 50000)
        assert result is True

    def test_stripe_error_raises_payment_error(self, gateway):
        import stripe
        with patch("stripe.Refund.create",
                   side_effect=stripe.error.StripeError("refund failed")):
            with pytest.raises(PaymentError, match="refund failed"):
                gateway.refund("pi_test_001", 50000)
