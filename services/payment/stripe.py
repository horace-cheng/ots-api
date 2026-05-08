"""
services/payment/stripe.py

Stripe 金流實作。
使用 Stripe Checkout Sessions 建立一次性付款。

需設定環境變數：
  PAYMENT_GATEWAY=stripe
  STRIPE_SECRET_KEY=sk_live_xxxx
  STRIPE_WEBHOOK_SECRET=whsec_xxxx

Webhook 注意：
  Stripe 的簽章驗證需要原始 request body（bytes）與 Stripe-Signature header，
  與 ECPay / PAYUNi（簽章在 form body 內）不同。路由層需在呼叫 parse_webhook()
  之前先用 stripe.Webhook.construct_event() 驗證簽章，再將解析後的 event dict
  傳入 parse_webhook()。
"""

import stripe
from datetime import datetime, timezone
from .base import (
    PaymentGateway, PaymentRequest, PaymentResult,
    WebhookPayload, InvoiceRequest, InvoiceResult,
    PaymentStatus, PaymentError, InvoiceError
)
from core.config import settings


class StripeGateway(PaymentGateway):

    def __init__(self):
        stripe.api_key = settings.stripe_secret_key

    def create_payment(self, req: PaymentRequest) -> PaymentResult:
        try:
            session = stripe.checkout.Session.create(
                mode="payment",
                line_items=[{
                    "price_data": {
                        "currency": "twd",
                        "product_data": {
                            "name": req.description or f"OTS 翻譯訂單 #{req.order_id[-8:]}",
                        },
                        "unit_amount": req.amount_ntd,
                    },
                    "quantity": 1,
                }],
                metadata={"order_id": req.order_id},
                success_url=req.return_url,
                cancel_url=req.return_url,
            )
            return PaymentResult(
                gateway_trade_no=session.id,
                payment_url=session.url or "",
                raw=session.to_dict(),
            )
        except stripe.error.StripeError as e:
            raise PaymentError(str(e), code="STRIPE_CREATE_FAILED", raw={"type": type(e).__name__})

    def parse_webhook(self, raw_body: dict) -> WebhookPayload:
        """
        解析 Stripe event dict（簽章應由路由層預先驗證）。
        raw_body 為 stripe.Event 的 dict 表示。
        """
        event_type = raw_body.get("type", "")
        session    = raw_body.get("data", {}).get("object", {})

        if event_type == "checkout.session.completed":
            order_id = session.get("metadata", {}).get("order_id", "")
            amount   = session.get("amount_total", 0)
            return WebhookPayload(
                order_id         = order_id,
                gateway_trade_no = session.get("id", ""),
                status           = PaymentStatus.PAID,
                amount_ntd       = amount,
                paid_at          = datetime.now(timezone.utc).isoformat(),
                raw              = raw_body,
            )

        return WebhookPayload(
            order_id         = "",
            gateway_trade_no = "",
            status           = PaymentStatus.FAILED,
            amount_ntd       = 0,
            paid_at          = None,
            raw              = raw_body,
        )

    def issue_invoice(self, req: InvoiceRequest) -> InvoiceResult:
        raise InvoiceError(
            "StripeGateway: invoice must be issued via external system.",
            code="STRIPE_INVOICE_NOT_SUPPORTED"
        )

    def refund(self, gateway_trade_no: str, amount_ntd: int) -> bool:
        try:
            stripe.Refund.create(
                payment_intent=gateway_trade_no,
                amount=amount_ntd,
            )
            return True
        except stripe.error.StripeError as e:
            raise PaymentError(str(e), code="STRIPE_REFUND_FAILED", raw={"type": type(e).__name__})
