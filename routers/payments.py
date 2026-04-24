"""
routers/payments.py

付款相關端點。
完全依賴 services.payment 抽象層，不直接碰任何金流 SDK。
切換金流廠商只需要改環境變數 PAYMENT_GATEWAY，這個檔案不需要動。
"""

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from core.database import get_db
from services.payment import (
    get_payment_gateway,
    PaymentStatus,
    InvoiceRequest,
    InvoiceType,
    PaymentError,
    InvoiceError,
)
from services.pipeline import trigger_pipeline
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/payments", tags=["payments"])


@router.post("/webhook")
async def payment_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    金流 Webhook 回調。
    ECPay / PAYUNi 在付款完成後 POST 到這個端點。
    手動匯款不走這裡，走 /admin/payments/{id}/confirm。
    """
    form = await request.form()
    raw_body = dict(form)

    gateway = get_payment_gateway()

    try:
        payload = gateway.parse_webhook(raw_body)
    except ValueError as e:
        logger.warning(f"Webhook verification failed: {e}")
        return PlainTextResponse("0|Error")

    if payload.status != PaymentStatus.PAID:
        logger.info(f"Webhook: order {payload.order_id} not paid (status={payload.status})")
        return PlainTextResponse("1|OK")

    # 更新付款記錄
    await db.execute(text("""
        UPDATE payments
        SET payment_status   = 'paid',
            ecpay_trade_no   = :trade_no,
            paid_at          = NOW()
        WHERE order_id = :order_id
    """), {"trade_no": payload.gateway_trade_no, "order_id": payload.order_id})

    await db.execute(text("""
        UPDATE orders SET status = 'paid' WHERE id = :order_id
    """), {"order_id": payload.order_id})

    await db.commit()

    # 觸發 Pipeline
    await trigger_pipeline(payload.order_id)

    # 自動開立 B2C 電子發票
    await _try_issue_invoice(db, gateway, payload.order_id, payload.amount_ntd)

    return PlainTextResponse("1|OK")


async def _try_issue_invoice(db, gateway, order_id: str, amount_ntd: int):
    """付款成功後嘗試自動開立 B2C 電子發票"""
    try:
        result = await db.execute(text("""
            SELECT u.invoice_carrier, u.client_type, u.tax_id, u.company_name
            FROM orders o
            JOIN users u ON u.id = o.user_id
            WHERE o.id = :order_id
        """), {"order_id": order_id})
        row = result.fetchone()
        if not row:
            return

        if row.client_type == "b2b":
            # B2B 三聯式由出納手動開立，這裡不處理
            return

        req = InvoiceRequest(
            order_id     = order_id,
            amount_ntd   = amount_ntd,
            invoice_type = InvoiceType.B2C_CLOUD,
            carrier      = row.invoice_carrier,
        )
        result_inv = gateway.issue_invoice(req)

        await db.execute(text("""
            UPDATE payments
            SET invoice_no        = :invoice_no,
                invoice_type      = 'b2c_cloud',
                invoice_status    = 'issued',
                invoice_issued_at = NOW()
            WHERE order_id = :order_id
        """), {"invoice_no": result_inv.invoice_no, "order_id": order_id})
        await db.commit()

        logger.info(f"Invoice issued: {result_inv.invoice_no} for order {order_id}")

    except InvoiceError as e:
        # 發票失敗不中斷付款流程，記 log 後人工補開
        logger.error(f"Invoice failed for order {order_id}: {e} (code={e.code})")
    except Exception as e:
        logger.error(f"Unexpected invoice error for order {order_id}: {e}")
