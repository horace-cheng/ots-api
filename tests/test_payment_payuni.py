import pytest
from services.payment.payuni import PAYUNiGateway
from services.payment.base import PaymentStatus


@pytest.fixture
def gateway(monkeypatch):
    from core.config import settings
    monkeypatch.setattr(settings, "ecpay_merchant_id", "merchant-001")
    monkeypatch.setattr(settings, "ecpay_hash_key", "1234567890123456")
    monkeypatch.setattr(settings, "ecpay_hash_iv", "1234567890123456")
    monkeypatch.setattr(settings, "ecpay_sandbox", True)
    return PAYUNiGateway()


class TestParseWebhook:
    def test_success_status_maps_to_paid(self, gateway):
        raw = {
            "Status":     "SUCCESS",
            "MerTradeNo": "order-001",
            "TradeNo":    "payuni-trade-001",
            "Amt":        "2000",
            "PayTime":    "2026-04-27 10:00:00",
        }
        payload = gateway.parse_webhook(raw)
        assert payload.status == PaymentStatus.PAID

    def test_non_success_status_maps_to_failed(self, gateway):
        for status in ("FAIL", "CANCEL", "EXPIRE", ""):
            raw = {
                "Status":     status,
                "MerTradeNo": "order-001",
                "TradeNo":    "payuni-trade-001",
                "Amt":        "2000",
            }
            payload = gateway.parse_webhook(raw)
            assert payload.status == PaymentStatus.FAILED, f"Expected FAILED for status={status!r}"

    def test_field_mapping(self, gateway):
        raw = {
            "Status":     "SUCCESS",
            "MerTradeNo": "order-abc",
            "TradeNo":    "payuni-xyz",
            "Amt":        "3500",
            "PayTime":    "2026-04-27 12:00:00",
        }
        payload = gateway.parse_webhook(raw)
        assert payload.order_id == "order-abc"
        assert payload.gateway_trade_no == "payuni-xyz"
        assert payload.amount_ntd == 3500
        assert payload.paid_at == "2026-04-27 12:00:00"

    def test_amount_parsed_as_int(self, gateway):
        raw = {"Status": "SUCCESS", "MerTradeNo": "", "TradeNo": "", "Amt": "9999"}
        payload = gateway.parse_webhook(raw)
        assert isinstance(payload.amount_ntd, int)
        assert payload.amount_ntd == 9999

    def test_missing_fields_default_gracefully(self, gateway):
        payload = gateway.parse_webhook({})
        assert payload.order_id == ""
        assert payload.gateway_trade_no == ""
        assert payload.amount_ntd == 0
        assert payload.status == PaymentStatus.FAILED


class TestHashInfo:
    def test_deterministic(self, gateway):
        result1 = gateway._hash_info("some_encrypted_data")
        result2 = gateway._hash_info("some_encrypted_data")
        assert result1 == result2

    def test_different_inputs_produce_different_hashes(self, gateway):
        assert gateway._hash_info("data_a") != gateway._hash_info("data_b")

    def test_returns_uppercase_hex(self, gateway):
        result = gateway._hash_info("test")
        assert result == result.upper()
        assert all(c in "0123456789ABCDEF" for c in result)
