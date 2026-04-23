"""
services/payment/manual.py

手動匯款實作（Year 1 過渡方案）。
不實際串接任何金流，只產生付款說明頁面，
由出納人工確認匯款後，在 Admin Dashboard 手動標記為 paid。
"""

from datetime import datetime, timezone
from .base import (
    PaymentGateway, PaymentRequest, PaymentResult,
    WebhookPayload, InvoiceRequest, InvoiceResult,
    PaymentStatus, PaymentError, InvoiceError
)
from core.config import settings


class ManualPaymentGateway(PaymentGateway):
    """
    手動匯款金流（無自動 webhook）。

    付款流程：
    1. create_payment() 回傳靜態付款說明頁面 URL
    2. 客戶看到銀行帳號，完成匯款
    3. 出納在 Admin Dashboard 手動確認，呼叫 /admin/payments/{id}/confirm
    4. 後端直接更新 payment_status = 'paid' 並觸發 pipeline

    invoice 在出納手動確認付款的同時，由後端呼叫
    ECPay / PAYUNi 的 invoice API 開立（或出納進後台手動開）。
    """

    # 公司銀行帳戶資訊（存 Secret Manager，由 config 注入）
    BANK_INFO = {
        "bank_name":    "玉山銀行",
        "bank_code":    "808",
        "branch":       "信義分行",
        "account_name": "木典股份有限公司",
        "account_no":   "XXXX-XXXX-XXXXXX",  # 由 config 注入
    }

    def create_payment(self, req: PaymentRequest) -> PaymentResult:
        # 產生一個假的 gateway_trade_no（格式：MANUAL-{order_id 前 8 碼}-{timestamp}）
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        trade_no = f"MANUAL-{req.order_id[:8].upper()}-{ts}"

        # 付款說明頁：指向 Web Portal 的靜態說明頁，帶上 order_id
        payment_url = f"{settings.web_portal_url}/payment/wire?order_id={req.order_id}"

        return PaymentResult(
            gateway_trade_no=trade_no,
            payment_url=payment_url,
            raw={
                "method":   "wire_transfer",
                "trade_no": trade_no,
                "bank":     self.BANK_INFO,
                "amount":   req.amount_ntd,
            }
        )

    def parse_webhook(self, raw_body: dict) -> WebhookPayload:
        # 手動匯款沒有 webhook，Admin Dashboard 呼叫 /admin/payments/{id}/confirm
        # 這個方法不應被呼叫，若被呼叫視為錯誤
        raise ValueError("ManualPaymentGateway does not support webhooks. "
                         "Use /admin/payments/{id}/confirm instead.")

    def issue_invoice(self, req: InvoiceRequest) -> InvoiceResult:
        # Year 1：出納手動在金流後台開立，這裡只回傳一個 placeholder
        # Year 2 換成真實金流後，這裡改為呼叫 ECPay / PAYUNi Invoice API
        raise InvoiceError(
            "ManualPaymentGateway: invoice must be issued manually via admin dashboard.",
            code="MANUAL_INVOICE"
        )

    def refund(self, gateway_trade_no: str, amount_ntd: int) -> bool:
        # 手動退款：出納操作銀行，無法自動化
        raise PaymentError(
            "ManualPaymentGateway: refund must be processed manually.",
            code="MANUAL_REFUND"
        )
