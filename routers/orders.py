"""
routers/orders.py

訂單相關端點。
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from datetime import datetime, timezone, timedelta
import uuid
import logging

from core.database import get_db
from core.config import settings
from routers.auth import get_current_user
from models.schemas import (
    OrderCreate, OrderResponse, OrderDetail, OrderListResponse, MessageResponse
)
from services.payment import get_payment_gateway, PaymentRequest, PaymentMethod

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/orders", tags=["orders"])


def _calc_deadline(track_type: str) -> datetime:
    """Fast Track: +48hr；Literary Track: +30天（協議後可延長）"""
    now = datetime.now(timezone.utc)
    if track_type == "fast":
        return now + timedelta(hours=48)
    return now + timedelta(days=30)


def _calc_price(track_type: str, word_count: int, target_lang: str) -> int:
    """
    簡易報價計算（實際報價依業務規則調整）。
    Fast Track:    NT$2/字，最低 NT$2,000
    Literary Track: NT$6/字，最低 NT$20,000
    日文加成 20%
    """
    base_rate = {"fast": 2, "literary": 6}.get(track_type, 2)
    lang_multiplier = 1.2 if target_lang == "ja" else 1.0
    price = int(word_count * base_rate * lang_multiplier)
    minimum = {"fast": 2000, "literary": 20000}.get(track_type, 2000)
    return max(price, minimum)


# ── POST /orders ──────────────────────────────────────────────────────────────
@router.post("", response_model=OrderResponse, status_code=201)
async def create_order(
    body: OrderCreate,
    user: dict = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    建立翻譯訂單。
    回傳 order_id 和 payment_url（付款頁面）。
    """
    order_id = str(uuid.uuid4())
    price    = _calc_price(body.track_type, body.word_count, body.target_lang)
    deadline = _calc_deadline(body.track_type)
    now      = datetime.now(timezone.utc)

    # 建立訂單
    await db.execute(text("""
        INSERT INTO orders (
            id, user_id, track_type, status,
            source_lang, target_lang, word_count, price_ntd,
            notes, created_at, deadline_at
        )
        SELECT
            :id, u.id, :track_type, 'pending_payment',
            :source_lang, :target_lang, :word_count, :price_ntd,
            :notes, :now, :deadline
        FROM users u WHERE u.uid_firebase = :uid
    """), {
        "id":          order_id,
        "track_type":  body.track_type,
        "source_lang": body.source_lang,
        "target_lang": body.target_lang,
        "word_count":  body.word_count,
        "price_ntd":   price,
        "notes":       body.notes,
        "now":         now,
        "deadline":    deadline,
        "uid":         user["uid"],
    })

    # 建立付款記錄
    await db.execute(text("""
        INSERT INTO payments (order_id, amount_ntd, payment_status)
        VALUES (:order_id, :amount, 'pending')
    """), {"order_id": order_id, "amount": price})

    # Literary Track：建立指派記錄（待 admin 指派編輯）
    if body.track_type == "literary":
        await db.execute(text("""
            INSERT INTO literary_assignments (order_id, status)
            VALUES (:order_id, 'pending')
        """), {"order_id": order_id})

    # 語料 log（預設 consent = false，待客戶確認）
    await db.execute(text("""
        INSERT INTO corpus_log (order_id, consent_given)
        VALUES (:order_id, false)
    """), {"order_id": order_id})

    await db.commit()

    # 建立付款 URL
    gateway = get_payment_gateway()
    base_url = settings.web_portal_url
    payment_req = PaymentRequest(
        order_id    = order_id,
        amount_ntd  = price,
        description = f"OTS {body.track_type.upper()} 翻譯服務 ({body.word_count}字)",
        return_url  = f"{base_url}/orders/{order_id}",
        notify_url  = f"{base_url}/payments/webhook",
        method      = PaymentMethod.CREDIT_CARD,
    )
    payment_result = gateway.create_payment(payment_req)

    # 回存 gateway_trade_no
    await db.execute(text("""
        UPDATE payments SET ecpay_trade_no = :trade_no WHERE order_id = :order_id
    """), {"trade_no": payment_result.gateway_trade_no, "order_id": order_id})
    await db.commit()

    logger.info(f"Order created: {order_id} ({body.track_type}, {body.word_count}字, NT${price})")

    return OrderResponse(
        order_id    = order_id,
        status      = "pending_payment",
        payment_url = payment_result.payment_url,
        track_type  = body.track_type,
        word_count  = body.word_count,
        price_ntd   = price,
        created_at  = now,
    )


# ── GET /orders ───────────────────────────────────────────────────────────────
@router.get("", response_model=OrderListResponse)
async def list_orders(
    status:     str | None = Query(None, description="篩選訂單狀態"),
    track_type: str | None = Query(None, description="篩選軌道類型"),
    limit:      int        = Query(20, ge=1, le=100),
    offset:     int        = Query(0, ge=0),
    user: dict             = Depends(get_current_user),
    db:   AsyncSession     = Depends(get_db),
):
    """列出當前用戶的訂單"""
    conditions = ["u.uid_firebase = :uid"]
    params: dict = {"uid": user["uid"], "limit": limit, "offset": offset}

    if status:
        conditions.append("o.status = :status")
        params["status"] = status
    if track_type:
        conditions.append("o.track_type = :track_type")
        params["track_type"] = track_type

    where = " AND ".join(conditions)

    result = await db.execute(text(f"""
        SELECT
            o.id, o.track_type, o.status, o.source_lang, o.target_lang,
            o.word_count, o.price_ntd, o.notes,
            o.created_at, o.deadline_at, o.delivered_at,
            o.gcs_output_path,
            p.payment_status, p.invoice_no
        FROM orders o
        JOIN users u ON u.id = o.user_id
        LEFT JOIN payments p ON p.order_id = o.id
        WHERE {where}
        ORDER BY o.created_at DESC
        LIMIT :limit OFFSET :offset
    """), params)

    rows = result.fetchall()

    count_result = await db.execute(text(f"""
        SELECT COUNT(*) FROM orders o
        JOIN users u ON u.id = o.user_id
        WHERE {where}
    """), {k: v for k, v in params.items() if k not in ("limit", "offset")})
    total = count_result.scalar()

    orders = [OrderDetail(**dict(r._mapping)) for r in rows]
    return OrderListResponse(orders=orders, total=total)


# ── GET /orders/{order_id} ────────────────────────────────────────────────────
@router.get("/{order_id}", response_model=OrderDetail)
async def get_order(
    order_id: str,
    user: dict         = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    """取得單筆訂單詳情"""
    result = await db.execute(text("""
        SELECT
            o.id, o.track_type, o.status, o.source_lang, o.target_lang,
            o.word_count, o.price_ntd, o.notes,
            o.created_at, o.deadline_at, o.delivered_at,
            o.gcs_output_path,
            p.payment_status, p.invoice_no
        FROM orders o
        JOIN users u ON u.id = o.user_id
        LEFT JOIN payments p ON p.order_id = o.id
        WHERE o.id = :order_id AND u.uid_firebase = :uid
    """), {"order_id": order_id, "uid": user["uid"]})

    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")

    return OrderDetail(**dict(row._mapping))


# ── DELETE /orders/{order_id} ─────────────────────────────────────────────────
@router.delete("/{order_id}", response_model=MessageResponse)
async def cancel_order(
    order_id: str,
    user: dict         = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    取消訂單。
    只有 pending_payment 狀態可以取消。
    """
    result = await db.execute(text("""
        SELECT o.id, o.status FROM orders o
        JOIN users u ON u.id = o.user_id
        WHERE o.id = :order_id AND u.uid_firebase = :uid
    """), {"order_id": order_id, "uid": user["uid"]})

    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    if row.status != "pending_payment":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot cancel order with status '{row.status}'"
        )

    await db.execute(text("""
        UPDATE orders SET status = 'cancelled' WHERE id = :order_id
    """), {"order_id": order_id})
    await db.commit()

    logger.info(f"Order cancelled: {order_id}")
    return MessageResponse(message="Order cancelled")
