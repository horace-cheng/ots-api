import pytest
from services.payment.manual import ManualPaymentGateway
from services.payment.base import (
    PaymentRequest, PaymentMethod,
    InvoiceRequest, InvoiceType,
    PaymentError, InvoiceError,
)


@pytest.fixture
def gw(monkeypatch):
    from core.config import settings
    monkeypatch.setattr(settings, "web_portal_url", "http://localhost:3000")
    return ManualPaymentGateway()


@pytest.fixture
def payment_req():
    return PaymentRequest(
        order_id="abc12345-0000-0000-0000-000000000001",
        amount_ntd=3000,
        description="OTS Translation",
        return_url="http://localhost:3000/payment/complete",
        notify_url="http://localhost:8080/payments/webhook",
        method=PaymentMethod.WIRE,
    )


class TestCreatePayment:
    def test_returns_wire_url_with_order_id(self, gw, payment_req):
        result = gw.create_payment(payment_req)
        assert "localhost:3000/payment/wire" in result.payment_url
        assert payment_req.order_id in result.payment_url

    def test_trade_no_has_manual_prefix(self, gw, payment_req):
        result = gw.create_payment(payment_req)
        assert result.gateway_trade_no.startswith("MANUAL-")

    def test_raw_contains_bank_and_amount(self, gw, payment_req):
        result = gw.create_payment(payment_req)
        assert result.raw["method"] == "wire_transfer"
        assert "bank" in result.raw
        assert result.raw["amount"] == payment_req.amount_ntd


class TestUnsupportedOperations:
    def test_parse_webhook_raises_value_error(self, gw):
        with pytest.raises(ValueError, match="does not support webhooks"):
            gw.parse_webhook({})

    def test_issue_invoice_raises_invoice_error(self, gw):
        req = InvoiceRequest(
            order_id="test-order",
            amount_ntd=1000,
            invoice_type=InvoiceType.B2C_CLOUD,
        )
        with pytest.raises(InvoiceError) as exc_info:
            gw.issue_invoice(req)
        assert exc_info.value.code == "MANUAL_INVOICE"

    def test_refund_raises_payment_error(self, gw):
        with pytest.raises(PaymentError) as exc_info:
            gw.refund("MANUAL-TRADE-001", 1000)
        assert exc_info.value.code == "MANUAL_REFUND"
