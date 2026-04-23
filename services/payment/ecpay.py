"""
services/payment/ecpay.py

綠界科技（ECPay）金流實作。
申請取得 MerchantID / HashKey / HashIV 後，把 core/config.py 的
ecpay_* 變數填入 Secret Manager 即可啟用。

Sandbox 測試憑證（綠界官方提供）：
  MerchantID: 2000132
  HashKey:    5294y06JbISpM5x9
  HashIV:     v77hoKGq4kWxNNIS
  測試信用卡:  4311-9522-2222-2222 / 任意有效期 / CVV 任意
"""

import hashlib
import urllib.parse
from datetime import datetime, timezone
from .base import (
    PaymentGateway, PaymentRequest, PaymentResult,
    WebhookPayload, InvoiceRequest, InvoiceResult,
    PaymentStatus, PaymentError, InvoiceError
)
from core.config import settings


class ECPayGateway(PaymentGateway):

    BASE_URL     = "https://payment.ecpay.com.tw"
    SANDBOX_URL  = "https://payment-stage.ecpay.com.tw"

    @property
    def _base(self):
        return self.SANDBOX_URL if settings.ecpay_sandbox else self.BASE_URL

    # ── 簽章工具 ──────────────────────────────────────────────────────────────
    def _mac(self, params: dict) -> str:
        """計算 CheckMacValue（SHA256）"""
        sorted_params = sorted(params.items())
        raw = "&".join(f"{k}={v}" for k, v in sorted_params)
        raw = f"HashKey={settings.ecpay_hash_key}&{raw}&HashIV={settings.ecpay_hash_iv}"
        raw = urllib.parse.quote_plus(raw).lower()
        return hashlib.sha256(raw.encode()).hexdigest().upper()

    def _verify_mac(self, params: dict) -> bool:
        received_mac = params.pop("CheckMacValue", "")
        expected_mac = self._mac(params)
        return received_mac.upper() == expected_mac.upper()

    # ── create_payment ────────────────────────────────────────────────────────
    def create_payment(self, req: PaymentRequest) -> PaymentResult:
        import httpx

        trade_date = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        params = {
            "MerchantID":        settings.ecpay_merchant_id,
            "MerchantTradeNo":   req.order_id.replace("-", "")[:20],
            "MerchantTradeDate": trade_date,
            "PaymentType":       "aio",
            "TotalAmount":       str(req.amount_ntd),
            "TradeDesc":         urllib.parse.quote(req.description),
            "ItemName":          req.description[:50],
            "ReturnURL":         req.notify_url,
            "ClientBackURL":     req.return_url,
            "ChoosePayment":     "Credit",
            "EncryptType":       "1",
        }
        params["CheckMacValue"] = self._mac(params)

        resp = httpx.post(f"{self._base}/Cashier/AioCheckOut/V5", data=params)
        if resp.status_code != 200:
            raise PaymentError(f"ECPay error: {resp.status_code}", raw={"body": resp.text})

        # ECPay 回傳 HTML 表單頁面（自動 submit），這裡直接回傳 URL
        # 實務上 Web Portal 以 POST form 方式提交到 ECPay
        payment_url = f"{self._base}/Cashier/AioCheckOut/V5"

        return PaymentResult(
            gateway_trade_no=params["MerchantTradeNo"],
            payment_url=payment_url,
            raw=params
        )

    # ── parse_webhook ─────────────────────────────────────────────────────────
    def parse_webhook(self, raw_body: dict) -> WebhookPayload:
        params = dict(raw_body)
        if not self._verify_mac(params):
            raise ValueError("ECPay CheckMacValue verification failed")

        rtn_code = raw_body.get("RtnCode", "")
        status   = PaymentStatus.PAID if rtn_code == "1" else PaymentStatus.FAILED

        return WebhookPayload(
            order_id         = raw_body.get("MerchantTradeNo", ""),
            gateway_trade_no = raw_body.get("TradeNo", ""),
            status           = status,
            amount_ntd       = int(raw_body.get("TradeAmt", 0)),
            paid_at          = raw_body.get("PaymentDate"),
            raw              = raw_body,
        )

    # ── issue_invoice ─────────────────────────────────────────────────────────
    def issue_invoice(self, req: InvoiceRequest) -> InvoiceResult:
        import httpx

        invoice_base = (
            "https://einvoice-stage.ecpay.com.tw"
            if settings.ecpay_sandbox else
            "https://einvoice.ecpay.com.tw"
        )

        params = {
            "MerchantID":     settings.ecpay_merchant_id,
            "RelateNumber":   req.order_id.replace("-", "")[:30],
            "TaxType":        "1",   # 應稅
            "SalesAmount":    str(req.amount_ntd),
            "InvType":        "07",  # 一般稅額
            "Items[0][ItemSeq]":   "1",
            "Items[0][ItemName]":  "OTS 翻譯服務",
            "Items[0][ItemCount]": "1",
            "Items[0][ItemWord]":  "式",
            "Items[0][ItemPrice]": str(req.amount_ntd),
            "Items[0][ItemTaxType]": "1",
            "Items[0][ItemAmount]":  str(req.amount_ntd),
        }

        if req.invoice_type.value == "b2c_cloud":
            params["CarrierType"] = "1"   # 會員載具
            if req.carrier:
                params["CarrierType"] = "2"   # 手機條碼
                params["CarrierNum"]  = req.carrier
            if req.email:
                params["CustomerEmail"] = req.email
        else:
            # B2B 三聯式
            params["CustomerIdentifier"] = req.tax_id or ""
            params["CustomerName"]       = req.company_name or ""
            params["Print"]              = "1"

        params["CheckMacValue"] = self._mac(params)

        resp = httpx.post(f"{invoice_base}/B2CInvoice/Issue", data=params)
        data = dict(urllib.parse.parse_qsl(resp.text))

        if data.get("RtnCode") != "1":
            raise InvoiceError(
                f"ECPay invoice error: {data.get('RtnMsg')}",
                code=data.get("RtnCode", ""),
                raw=data
            )

        return InvoiceResult(
            invoice_no = data.get("InvoiceNo", ""),
            issued_at  = data.get("InvoiceDate", ""),
            raw        = data,
        )

    # ── refund ────────────────────────────────────────────────────────────────
    def refund(self, gateway_trade_no: str, amount_ntd: int) -> bool:
        import httpx

        params = {
            "MerchantID":  settings.ecpay_merchant_id,
            "MerchantTradeNo": gateway_trade_no,
            "Action":      "R",   # 退款
            "TotalAmount": str(amount_ntd),
        }
        params["CheckMacValue"] = self._mac(params)

        resp = httpx.post(f"{self._base}/CreditDetail/DoAction", data=params)
        data = dict(urllib.parse.parse_qsl(resp.text))

        if data.get("RtnCode") != "1":
            raise PaymentError(
                f"ECPay refund error: {data.get('RtnMsg')}",
                code=data.get("RtnCode", ""),
                raw=data
            )
        return True
