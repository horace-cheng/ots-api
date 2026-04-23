"""
services/payment/payuni.py

PAYUNi（統一金流）實作。
申請取得 MerID / HashKey / HashIV 後填入 Secret Manager 即可啟用。
API 文件：https://www.payuni.com.tw/docs/web/
"""

import hashlib
import hmac
import json
import urllib.parse
from datetime import datetime, timezone
from .base import (
    PaymentGateway, PaymentRequest, PaymentResult,
    WebhookPayload, InvoiceRequest, InvoiceResult,
    PaymentStatus, PaymentError, InvoiceError
)
from core.config import settings


class PAYUNiGateway(PaymentGateway):

    BASE_URL    = "https://api.payuni.com.tw"
    SANDBOX_URL = "https://sandbox-api.payuni.com.tw"

    @property
    def _base(self):
        return self.SANDBOX_URL if settings.ecpay_sandbox else self.BASE_URL

    def _encrypt(self, params: dict) -> str:
        """AES-256-CBC 加密（PAYUNi 使用加密後的 EncryptInfo）"""
        # PAYUNi 使用 AES 加密，實際實作需要 pycryptodome
        # pip install pycryptodome
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import pad
        import base64

        data = urllib.parse.urlencode(sorted(params.items()))
        key  = settings.ecpay_hash_key.encode()[:32]
        iv   = settings.ecpay_hash_iv.encode()[:16]
        cipher = AES.new(key, AES.MODE_CBC, iv)
        encrypted = cipher.encrypt(pad(data.encode(), AES.block_size))
        return base64.b64encode(encrypted).decode()

    def _hash_info(self, encrypt_info: str) -> str:
        """計算 HashInfo（SHA256）"""
        raw = f"{settings.ecpay_hash_key}{encrypt_info}{settings.ecpay_hash_iv}"
        return hashlib.sha256(raw.encode()).hexdigest().upper()

    def create_payment(self, req: PaymentRequest) -> PaymentResult:
        import httpx

        trade_no = req.order_id.replace("-", "")[:30]
        params = {
            "MerID":       settings.ecpay_merchant_id,
            "Timestamp":   str(int(datetime.now().timestamp())),
            "TradeNo":     trade_no,
            "Amt":         str(req.amount_ntd),
            "ItemDesc":    req.description[:50],
            "NotifyURL":   req.notify_url,
            "ReturnURL":   req.return_url,
            "PayType":     "credit",
        }
        encrypt_info = self._encrypt(params)
        hash_info    = self._hash_info(encrypt_info)

        resp = httpx.post(f"{self._base}/api/trade/create", json={
            "MerID":       settings.ecpay_merchant_id,
            "EncryptInfo": encrypt_info,
            "HashInfo":    hash_info,
        })
        data = resp.json()

        if data.get("Status") != "SUCCESS":
            raise PaymentError(
                f"PAYUNi error: {data.get('Message')}",
                code=data.get("Status", ""),
                raw=data
            )

        return PaymentResult(
            gateway_trade_no=trade_no,
            payment_url=data.get("PayURL", ""),
            raw=data
        )

    def parse_webhook(self, raw_body: dict) -> WebhookPayload:
        # PAYUNi webhook 回傳 EncryptInfo，需解密後驗證
        # 實作略（結構與 ECPay 類似，解密後取 Status / TradeNo / Amt）
        status_raw = raw_body.get("Status", "")
        status     = PaymentStatus.PAID if status_raw == "SUCCESS" else PaymentStatus.FAILED

        return WebhookPayload(
            order_id         = raw_body.get("MerTradeNo", ""),
            gateway_trade_no = raw_body.get("TradeNo", ""),
            status           = status,
            amount_ntd       = int(raw_body.get("Amt", 0)),
            paid_at          = raw_body.get("PayTime"),
            raw              = raw_body,
        )

    def issue_invoice(self, req: InvoiceRequest) -> InvoiceResult:
        # PAYUNi 電子發票 API（略，結構類似 ECPay）
        raise InvoiceError("PAYUNi invoice not yet implemented", code="NOT_IMPL")

    def refund(self, gateway_trade_no: str, amount_ntd: int) -> bool:
        raise PaymentError("PAYUNi refund not yet implemented", code="NOT_IMPL")
