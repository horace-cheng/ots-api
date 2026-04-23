"""
services/payment/factory.py

金流廠商工廠函式。
整個系統只有這一個地方知道「現在用哪家金流」。
切換廠商只需要改 PAYMENT_GATEWAY 環境變數，程式碼不動。

支援的值：
  manual   → 手動匯款（Year 1 過渡，無需申請任何金流）
  ecpay    → 綠界科技（申請後啟用）
  payuni   → 統一金流（申請後啟用）
"""

from functools import lru_cache
import os
from .base import PaymentGateway


@lru_cache(maxsize=1)
def get_payment_gateway() -> PaymentGateway:
    """
    回傳當前環境設定的金流實作。
    lru_cache 確保整個應用生命週期只初始化一次。
    """
    provider = os.environ.get("PAYMENT_GATEWAY", "manual").lower().strip()

    if provider == "ecpay":
        from .ecpay import ECPayGateway
        return ECPayGateway()

    elif provider == "payuni":
        from .payuni import PAYUNiGateway
        return PAYUNiGateway()

    elif provider == "manual":
        from .manual import ManualPaymentGateway
        return ManualPaymentGateway()

    else:
        raise ValueError(
            f"Unknown PAYMENT_GATEWAY: '{provider}'. "
            f"Supported values: manual, ecpay, payuni"
        )
