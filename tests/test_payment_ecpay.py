import pytest
from services.payment.ecpay import ECPayGateway
from services.payment.base import PaymentStatus


@pytest.fixture
def gw(ecpay_settings):
    return ECPayGateway()


def _signed_webhook(gw: ECPayGateway, **overrides) -> dict:
    """Build a webhook dict with a correct CheckMacValue."""
    params = {
        "MerchantID":      "2000132",
        "MerchantTradeNo": "TESTORDER001",
        "TradeNo":         "ECPAY2026001",
        "RtnCode":         "1",
        "RtnMsg":          "Succeeded",
        "TradeAmt":        "1500",
        "PaymentDate":     "2026/04/24 15:00:00",
        "PaymentType":     "Credit_CreditCard",
        **overrides,
    }
    params["CheckMacValue"] = gw._mac(params)
    return params


class TestMac:
    def test_deterministic(self, gw):
        params = {"MerchantID": "2000132", "TradeAmt": "500"}
        assert gw._mac(params) == gw._mac(params)

    def test_verify_valid(self, gw):
        params = {"MerchantID": "2000132", "TradeAmt": "500"}
        params["CheckMacValue"] = gw._mac(params)
        assert gw._verify_mac(params) is True

    def test_verify_tampered_value(self, gw):
        params = {"MerchantID": "2000132", "TradeAmt": "500", "CheckMacValue": "BADHASH"}
        assert gw._verify_mac(params) is False

    def test_verify_missing_value(self, gw):
        params = {"MerchantID": "2000132", "TradeAmt": "500"}
        assert gw._verify_mac(params) is False


class TestParseWebhook:
    def test_paid_status(self, gw):
        result = gw.parse_webhook(_signed_webhook(gw, RtnCode="1"))
        assert result.status == PaymentStatus.PAID
        assert result.order_id == "TESTORDER001"
        assert result.gateway_trade_no == "ECPAY2026001"
        assert result.amount_ntd == 1500

    def test_failed_status(self, gw):
        result = gw.parse_webhook(_signed_webhook(gw, RtnCode="10200047"))
        assert result.status == PaymentStatus.FAILED

    def test_invalid_mac_raises(self, gw):
        payload = _signed_webhook(gw)
        payload["CheckMacValue"] = "TAMPERED"
        with pytest.raises(ValueError, match="CheckMacValue verification failed"):
            gw.parse_webhook(payload)

    def test_missing_mac_raises(self, gw):
        payload = _signed_webhook(gw)
        del payload["CheckMacValue"]
        with pytest.raises(ValueError):
            gw.parse_webhook(payload)

    def test_amount_parsed_as_int(self, gw):
        result = gw.parse_webhook(_signed_webhook(gw, TradeAmt="2500"))
        assert result.amount_ntd == 2500
        assert isinstance(result.amount_ntd, int)
