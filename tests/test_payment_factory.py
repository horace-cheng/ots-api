import pytest
from unittest.mock import patch
from services.payment.factory import get_payment_gateway
from services.payment.manual import ManualPaymentGateway
from services.payment.ecpay import ECPayGateway
from services.payment.payuni import PAYUNiGateway


@pytest.fixture(autouse=True)
def clear_cache():
    get_payment_gateway.cache_clear()
    yield
    get_payment_gateway.cache_clear()


def test_factory_manual():
    with patch.dict("os.environ", {"PAYMENT_GATEWAY": "manual"}):
        assert isinstance(get_payment_gateway(), ManualPaymentGateway)


def test_factory_ecpay():
    with patch.dict("os.environ", {"PAYMENT_GATEWAY": "ecpay"}):
        assert isinstance(get_payment_gateway(), ECPayGateway)


def test_factory_payuni():
    with patch.dict("os.environ", {"PAYMENT_GATEWAY": "payuni"}):
        assert isinstance(get_payment_gateway(), PAYUNiGateway)


def test_factory_unknown_raises():
    with patch.dict("os.environ", {"PAYMENT_GATEWAY": "stripe"}):
        with pytest.raises(ValueError, match="Unknown PAYMENT_GATEWAY"):
            get_payment_gateway()


def test_factory_case_insensitive():
    with patch.dict("os.environ", {"PAYMENT_GATEWAY": "MANUAL"}):
        assert isinstance(get_payment_gateway(), ManualPaymentGateway)


def test_factory_is_cached():
    with patch.dict("os.environ", {"PAYMENT_GATEWAY": "manual"}):
        assert get_payment_gateway() is get_payment_gateway()
