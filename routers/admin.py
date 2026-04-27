"""
routers/admin.py

Admin Dashboard 端點。
QA 審閱、手動付款確認、Literary Track 指派。
所有端點需要 admin 權限（get_admin_user）。
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from datetime import datetime, timezone
import logging

from core.database import get_db
from routers.auth import get_admin_user
from models.schemas import (
    QAFlagResponse, QAFlagResolve,
    AssignmentUpdate, AssignmentResponse,
    PaymentConfirm, MessageResponse,
    OrderDetail, OrderListResponse,
)
from services.payment import (
    get_payment_gateway, InvoiceRequest, InvoiceType, InvoiceError
)
from services.pipeline import trigger_pipeline

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


# ── QA Flags ──────────────────────────────────────────────────────────────────
@router.get("/qa-flags", response_model=list[QAFlagResponse])
async def list_qa_flags(
    flag_level: str | None = Query(None, description="must_fix / review / pass"),
    resolved:   bool | None = Query(False),
    limit:      int          = Query(50, ge=1, le=200),
    offset:     int          = Query(0, ge=0),
    admin: dict              = Depends(get_admin_user),
    db:   AsyncSession       = Depends(get_db),
):
    """列出 QA 標記（預設只顯示未處理項目）"""
    conditions = []
    params: dict = {"limit": limit, "offset": offset}

    if flag_level:
        conditions.append("qf.flag_level = :flag_level")
        params["flag_level"] = flag_level
    if resolved is not None:
        conditions.append("qf.resolved = :resolved")
        params["resolved"] = resolved

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    result = await db.execute(text(f"""
        SELECT
            qf.id, qf.job_id, pj.order_id,
            qf.paragraph_index, qf.flag_level, qf.flag_type,
            qf.source_segment, qf.translated_segment,
            qf.reviewer_note, qf.resolved, qf.flagged_at
        FROM qa_flags qf
        JOIN pipeline_jobs pj ON pj.id = qf.job_id
        {where}
        ORDER BY qf.flagged_at DESC
        LIMIT :limit OFFSET :offset
    """), params)

    rows = result.fetchall()
    return [QAFlagResponse(**dict(r._mapping)) for r in rows]


@router.patch("/qa-flags/{flag_id}", response_model=MessageResponse)
async def resolve_qa_flag(
    flag_id: str,
    body:  QAFlagResolve,
    admin: dict          = Depends(get_admin_user),
    db:   AsyncSession   = Depends(get_db),
):
    """標記 QA flag 已處理，填寫審閱備注"""
    result = await db.execute(
        text("SELECT id FROM qa_flags WHERE id = :id"),
        {"id": flag_id}
    )
    if not result.fetchone():
        raise HTTPException(status_code=404, detail="QA flag not found")

    await db.execute(text("""
        UPDATE qa_flags
        SET resolved      = true,
            reviewer_note = :note,
            resolved_at   = NOW()
        WHERE id = :id
    """), {"note": body.reviewer_note, "id": flag_id})
    await db.commit()

    # 確認該 job 的所有 must_fix flags 是否都已解決
    job_result = await db.execute(text("""
        SELECT pj.order_id, pj.id AS job_id,
               COUNT(*) FILTER (WHERE qf.flag_level = 'must_fix' AND NOT qf.resolved) AS unresolved
        FROM qa_flags qf
        JOIN pipeline_jobs pj ON pj.id = qf.job_id
        WHERE qf.id = :id
        GROUP BY pj.order_id, pj.id
    """), {"id": flag_id})
    job_row = job_result.fetchone()

    if job_row and job_row.unresolved == 0:
        # 所有 must_fix 都解決了，更新 job 狀態為 success
        await db.execute(text("""
            UPDATE pipeline_jobs SET status = 'success' WHERE id = :job_id
        """), {"job_id": str(job_row.job_id)})
        await db.commit()
        logger.info(f"All must_fix flags resolved for job {job_row.job_id}, order {job_row.order_id}")

    return MessageResponse(message="QA flag resolved")


# ── 手動付款確認（ManualPaymentGateway 用）───────────────────────────────────
@router.post("/payments/{order_id}/confirm", response_model=MessageResponse)
async def confirm_manual_payment(
    order_id: str,
    body:  PaymentConfirm,
    admin: dict           = Depends(get_admin_user),
    db:   AsyncSession    = Depends(get_db),
):
    """
    出納確認匯款到帳後，手動標記付款完成並觸發 pipeline。
    僅適用於 ManualPaymentGateway（PAYMENT_GATEWAY=manual）。
    """
    result = await db.execute(text("""
        SELECT o.id, o.status, o.price_ntd, p.payment_status
        FROM orders o
        JOIN payments p ON p.order_id = o.id
        WHERE o.id = :order_id
    """), {"order_id": order_id})

    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    if row.payment_status == "paid":
        raise HTTPException(status_code=400, detail="Payment already confirmed")
    if row.status == "cancelled":
        raise HTTPException(status_code=400, detail="Order is cancelled")

    # 金額驗證
    if body.confirmed_amount_ntd != row.price_ntd:
        raise HTTPException(
            status_code=400,
            detail=f"Amount mismatch: expected {row.price_ntd}, got {body.confirmed_amount_ntd}"
        )

    now = datetime.now(timezone.utc)
    await db.execute(text("""
        UPDATE payments
        SET payment_status = 'paid', paid_at = :now
        WHERE order_id = :order_id
    """), {"now": now, "order_id": order_id})

    await db.execute(text("""
        UPDATE orders SET status = 'paid' WHERE id = :order_id
    """), {"order_id": order_id})

    await db.commit()

    # 觸發 Pipeline
    await trigger_pipeline(order_id)

    logger.info(f"Manual payment confirmed: order={order_id}, amount={body.confirmed_amount_ntd}")
    return MessageResponse(message=f"Payment confirmed and pipeline triggered for order {order_id}")


# ── 電子發票手動開立（B2B 三聯式）────────────────────────────────────────────
@router.post("/payments/{order_id}/invoice", response_model=MessageResponse)
async def issue_b2b_invoice(
    order_id: str,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """出納手動為 B2B 訂單開立三聯式電子發票"""
    result = await db.execute(text("""
        SELECT o.price_ntd, u.tax_id, u.company_name, p.invoice_status
        FROM orders o
        JOIN users u ON u.id = o.user_id
        JOIN payments p ON p.order_id = o.id
        WHERE o.id = :order_id
    """), {"order_id": order_id})

    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    if row.invoice_status == "issued":
        raise HTTPException(status_code=400, detail="Invoice already issued")
    if not row.tax_id:
        raise HTTPException(status_code=400, detail="No tax_id for this user")

    gateway = get_payment_gateway()
    req = InvoiceRequest(
        order_id     = order_id,
        amount_ntd   = row.price_ntd,
        invoice_type = InvoiceType.B2B_TRIPLICATE,
        tax_id       = row.tax_id,
        company_name = row.company_name,
    )

    try:
        inv_result = gateway.issue_invoice(req)
    except InvoiceError as e:
        raise HTTPException(status_code=502, detail=f"Invoice error: {e}")

    await db.execute(text("""
        UPDATE payments
        SET invoice_no        = :invoice_no,
            invoice_type      = 'b2b_triplicate',
            invoice_status    = 'issued',
            invoice_issued_at = NOW()
        WHERE order_id = :order_id
    """), {"invoice_no": inv_result.invoice_no, "order_id": order_id})
    await db.commit()

    return MessageResponse(message=f"Invoice issued: {inv_result.invoice_no}")


# ── Literary Track 指派管理 ───────────────────────────────────────────────────
@router.get("/assignments", response_model=list[AssignmentResponse])
async def list_assignments(
    status: str | None = Query(None),
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """列出 Literary Track 指派狀態"""
    conditions = []
    params: dict = {}

    if status:
        conditions.append("la.status = :status")
        params["status"] = status

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    result = await db.execute(text(f"""
        SELECT
            la.id, la.order_id, la.editor_id, la.proofreader_id,
            la.status, la.assigned_at,
            la.editor_submitted_at, la.proofread_submitted_at
        FROM literary_assignments la
        {where}
        ORDER BY la.assigned_at DESC
    """), params)

    rows = result.fetchall()
    return [AssignmentResponse(**dict(r._mapping)) for r in rows]


@router.patch("/assignments/{order_id}", response_model=AssignmentResponse)
async def update_assignment(
    order_id: str,
    body:  AssignmentUpdate,
    admin: dict             = Depends(get_admin_user),
    db:   AsyncSession      = Depends(get_db),
):
    """指派或更換編輯 / 校對"""
    updates = []
    params: dict = {"order_id": order_id}

    if body.editor_id is not None:
        updates.append("editor_id = :editor_id")
        params["editor_id"] = body.editor_id
    if body.proofreader_id is not None:
        updates.append("proofreader_id = :proofreader_id")
        params["proofreader_id"] = body.proofreader_id

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    # 指派後狀態改為 editing
    updates.append("status = 'editing'")
    set_clause = ", ".join(updates)

    await db.execute(text(f"""
        UPDATE literary_assignments
        SET {set_clause}
        WHERE order_id = :order_id
    """), params)
    await db.commit()

    result = await db.execute(text("""
        SELECT id, order_id, editor_id, proofreader_id,
               status, assigned_at, editor_submitted_at, proofread_submitted_at
        FROM literary_assignments WHERE order_id = :order_id
    """), {"order_id": order_id})

    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Assignment not found")
    return AssignmentResponse(**dict(row._mapping))


# ── Admin: 所有訂單列表 ───────────────────────────────────────────────────────
@router.get("/orders", response_model=OrderListResponse)
async def admin_list_orders(
    status:     str | None = Query(None),
    track_type: str | None = Query(None),
    limit:      int         = Query(50, ge=1, le=200),
    offset:     int         = Query(0, ge=0),
    admin: dict             = Depends(get_admin_user),
    db:   AsyncSession      = Depends(get_db),
):
    """Admin 用：列出所有客戶的訂單（不限於當前用戶）"""
    conditions = []
    params: dict = {"limit": limit, "offset": offset}

    if status:
        conditions.append("o.status = :status")
        params["status"] = status
    if track_type:
        conditions.append("o.track_type = :track_type")
        params["track_type"] = track_type

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    result = await db.execute(text(f"""
        SELECT
            o.id, o.track_type, o.status, o.source_lang, o.target_lang,
            o.word_count, o.price_ntd, o.notes,
            o.created_at, o.deadline_at, o.delivered_at,
            o.gcs_output_path,
            p.payment_status, p.invoice_no
        FROM orders o
        LEFT JOIN payments p ON p.order_id = o.id
        {where}
        ORDER BY o.created_at DESC
        LIMIT :limit OFFSET :offset
    """), params)

    rows = result.fetchall()
    orders = [OrderDetail(**dict(r._mapping)) for r in rows]

    count_result = await db.execute(text(f"""
        SELECT COUNT(*) FROM orders o {where}
    """), {k: v for k, v in params.items() if k not in ("limit", "offset")})
    total = count_result.scalar()

    return OrderListResponse(orders=orders, total=total)


# ── Admin: 取得單一訂單詳情 ───────────────────────────────────────────────────
@router.get("/orders/{order_id}", response_model=OrderDetail)
async def admin_get_order(
    order_id: str,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    result = await db.execute(text("""
        SELECT
            o.id, o.track_type, o.status, o.source_lang, o.target_lang,
            o.word_count, o.price_ntd, o.notes,
            o.created_at, o.deadline_at, o.delivered_at,
            o.gcs_output_path,
            p.payment_status, p.invoice_no
        FROM orders o
        LEFT JOIN payments p ON p.order_id = o.id
        WHERE o.id = :order_id
    """), {"order_id": order_id})
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    return OrderDetail(**dict(row._mapping))


# ── Admin: 標記訂單已交付 ─────────────────────────────────────────────────────
@router.post("/orders/{order_id}/deliver", response_model=MessageResponse)
async def mark_delivered(
    order_id:       str,
    gcs_output_path: str,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    標記訂單已交付，設定 gcs_output_path 讓客戶可以下載。
    """
    result = await db.execute(
        text("SELECT id, status FROM orders WHERE id = :id"),
        {"id": order_id}
    )
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    if row.status == "delivered":
        raise HTTPException(status_code=400, detail="Already delivered")

    await db.execute(text("""
        UPDATE orders
        SET status          = 'delivered',
            gcs_output_path = :path,
            delivered_at    = NOW()
        WHERE id = :order_id
    """), {"path": gcs_output_path, "order_id": order_id})
    await db.commit()

    logger.info(f"Order delivered: {order_id}")
    return MessageResponse(message=f"Order {order_id} marked as delivered")
