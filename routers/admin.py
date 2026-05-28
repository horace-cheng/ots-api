"""
routers/admin.py

Admin Dashboard 端點。
QA 審閱、手動付款確認、Literary Track 指派。
所有端點需要 admin 權限（get_admin_user）。
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from datetime import datetime, timezone
import logging

from core.database import get_db
from core import storage
from core.storage import generate_download_signed_url, read_blob
from services.document_converter import convert_document
from routers.auth import get_admin_user
from models.schemas import (
    QAFlagResponse, QAFlagListResponse, QAFlagResolve,
    AssignmentUpdate, AssignmentResponse, AssignmentListResponse,
    AssignmentAction, AssignmentComplete,
    PaymentConfirm, MessageResponse, QuoteUpdate,
    OrderDetail, AdminOrderDetail, OrderListResponse,
    DownloadUrlResponse, OriginalContentResponse,
    UserListItem, UserListResponse, UserUpdateRequest, UserLanguageUpdate, UserLanguage,
    QASegment, QASegmentListResponse, QASegmentsBatchUpdate,
    EditorAssignRequest,
    SupportFileResponse, SupportFileListResponse,
    TokenUsageResponse, TokenUsageItem,
    TokenUsageDetailResponse, TokenUsageDetailItem,
)
from services.payment import (
    get_payment_gateway, InvoiceRequest, InvoiceType, InvoiceError
)
from services.pipeline import trigger_pipeline
from services.notification import publish_event_sync, EventType

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


# ── QA Flags ──────────────────────────────────────────────────────────────────
@router.get("/qa-flags", response_model=QAFlagListResponse)
async def list_qa_flags(
    flag_level: str | None = Query(None, description="must_fix / review / pass"),
    resolved:   bool | None = Query(None),
    order_id:   str | None = Query(None),
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
    if order_id:
        conditions.append("pj.order_id = :order_id")
        params["order_id"] = order_id

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
    flags = [QAFlagResponse(**dict(r._mapping)) for r in rows]

    count_result = await db.execute(text(f"""
        SELECT COUNT(*) FROM qa_flags qf
        JOIN pipeline_jobs pj ON pj.id = qf.job_id
        {where}
    """), {k: v for k, v in params.items() if k not in ("limit", "offset")})
    total = count_result.scalar()

    return QAFlagListResponse(flags=flags, total=total)


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


# ── Literary Track 報價 ──────────────────────────────────────────────────────
@router.post("/orders/{order_id}/quote", response_model=MessageResponse)
async def set_order_quote(
    order_id: str,
    body:  QuoteUpdate,
    admin: dict           = Depends(get_admin_user),
    db:   AsyncSession    = Depends(get_db),
):
    """
    Admin issues or revises a quote for a Literary Track order.
    Only allowed when status is 'awaiting_quote' or 'quoted' (before payment).
    """
    result = await db.execute(text("""
        SELECT o.id, o.status, o.track_type, o.price_ntd
        FROM orders o
        WHERE o.id = :order_id
    """), {"order_id": order_id})

    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    if row.track_type != "literary":
        raise HTTPException(status_code=400, detail="Quote only applies to Literary Track orders")
    if row.status not in ("awaiting_quote", "quoted"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot set quote for order with status '{row.status}'"
        )

    now = datetime.now(timezone.utc)
    await db.execute(text("""
        UPDATE orders
        SET quoted_price  = :price,
            quoted_at     = :now,
            price_ntd     = :price,
            status        = 'quoted'
        WHERE id = :order_id
    """), {"price": body.quoted_price, "now": now, "order_id": order_id})

    # 若付款記錄已存在（首次報價），更新金額
    await db.execute(text("""
        UPDATE payments
        SET amount_ntd = :price
        WHERE order_id = :order_id AND payment_status = 'pending'
    """), {"price": body.quoted_price, "order_id": order_id})

    # 若付款記錄不存在（首次報價），建立付款記錄
    result = await db.execute(text("""
        SELECT id FROM payments WHERE order_id = :order_id
    """), {"order_id": order_id})
    if not result.fetchone():
        await db.execute(text("""
            INSERT INTO payments (order_id, amount_ntd, payment_status)
            VALUES (:order_id, :amount, 'pending')
        """), {"order_id": order_id, "amount": body.quoted_price})

    await db.commit()

    await publish_event_sync(
        event_type=EventType.QUOTE_SET,
        order_id=order_id,
        data={"quoted_price": body.quoted_price},
    )
    logger.info(f"Quote set: order={order_id}, price={body.quoted_price}")
    return MessageResponse(message=f"Quote set: NT${body.quoted_price}")


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
        SELECT o.id, o.status, o.price_ntd, o.quoted_price, o.track_type, p.payment_status
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

    # LT 使用 quoted_price，FT 使用 price_ntd
    expected_amount = row.quoted_price if row.track_type == "literary" else row.price_ntd
    if body.confirmed_amount_ntd != expected_amount:
        raise HTTPException(
            status_code=400,
            detail=f"Amount mismatch: expected {expected_amount}, got {body.confirmed_amount_ntd}"
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

    await publish_event_sync(
        event_type=EventType.PAYMENT_CONFIRMED,
        order_id=order_id,
        data={"amount": body.confirmed_amount_ntd},
    )

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
@router.get("/assignments", response_model=AssignmentListResponse)
async def list_assignments(
    status: str | None = Query(None),
    limit:  int        = Query(50, ge=1, le=200),
    offset: int        = Query(0, ge=0),
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """列出 Literary Track 指派狀態"""
    conditions = []
    params: dict = {"limit": limit, "offset": offset}

    if status:
        conditions.append("la.status = :status")
        params["status"] = status

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    result = await db.execute(text(f"""
        SELECT
            la.id, la.order_id, la.editor_id, la.qa_id, la.proofreader_id,
            la.status, la.assigned_at,
            la.editor_submitted_at, la.proofread_submitted_at, la.qa_submitted_at,
            la.editor_notes, la.proofreader_notes
        FROM assignments la
        {where}
        ORDER BY la.assigned_at DESC
        LIMIT :limit OFFSET :offset
    """), params)

    rows = result.fetchall()
    assignments = [AssignmentResponse(**dict(r._mapping)) for r in rows]

    count_result = await db.execute(text(f"""
        SELECT COUNT(*) FROM assignments la {where}
    """), {k: v for k, v in params.items() if k not in ("limit", "offset")})
    total = count_result.scalar()

    return AssignmentListResponse(assignments=assignments, total=total)


@router.get("/assignments/{order_id}")
async def get_assignment(
    order_id: str,
    admin: dict         = Depends(get_admin_user),
    db:   AsyncSession  = Depends(get_db),
):
    """
    Get assignment status for a single order.
    Called by Cloud Workflows to poll editor/proofreader completion.
    """
    result = await db.execute(text("""
        SELECT
            la.id, la.order_id, la.editor_id, la.qa_id, la.proofreader_id,
            la.status, la.assigned_at,
            la.editor_submitted_at, la.proofread_submitted_at, la.qa_submitted_at,
            la.editor_notes, la.proofreader_notes
        FROM assignments la
        WHERE la.order_id = :order_id
    """), {"order_id": order_id})

    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Assignment not found")

    return AssignmentResponse(**dict(row._mapping))


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
         UPDATE assignments
        SET {set_clause}
        WHERE order_id = :order_id
    """), params)
    await db.commit()

    result = await db.execute(text("""
        SELECT id, order_id, editor_id, qa_id, proofreader_id,
               status, assigned_at, editor_submitted_at, proofread_submitted_at, qa_submitted_at,
               editor_notes, proofreader_notes
        FROM assignments WHERE order_id = :order_id
    """), {"order_id": order_id})

    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Assignment not found")
    return AssignmentResponse(**dict(row._mapping))


# ── Literary Track: Role-based Assignment ────────────────────────────────────
@router.post("/assignments/{order_id}", response_model=AssignmentResponse)
async def assign_literary_role(
    order_id: str,
    body:  AssignmentAction,
    admin: dict             = Depends(get_admin_user),
    db:   AsyncSession      = Depends(get_db),
):
    """
    Assign an editor or proofreader by user_id or email.
    Sets the appropriate timestamp and transitions status.
    """
    if body.role not in ("editor", "proofreader"):
        raise HTTPException(status_code=400, detail="role must be 'editor' or 'proofreader'")
    if not body.user_id and not body.email:
        raise HTTPException(status_code=400, detail="user_id or email is required")

    # Find user
    if body.user_id:
        user_result = await db.execute(text("""
            SELECT id, is_editor FROM users WHERE id = :user_id
        """), {"user_id": body.user_id})
    else:
        user_result = await db.execute(text("""
            SELECT id, is_editor FROM users WHERE email = :email
        """), {"email": body.email})

    user_row = user_result.fetchone()
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")

    if body.role == "editor" and not user_row.is_editor:
        raise HTTPException(status_code=400, detail="User does not have editor role")

    now = datetime.now(timezone.utc)

    if body.role == "editor":
        assign_result = await db.execute(text("""
            SELECT status FROM assignments WHERE order_id = :order_id
        """), {"order_id": order_id})
        assign_row = assign_result.fetchone()
        if not assign_row:
            await db.execute(text("""
                INSERT INTO assignments (order_id, status)
                VALUES (:order_id, 'pending')
            """), {"order_id": order_id})
        elif assign_row.status not in ("pending", "editing"):
            raise HTTPException(status_code=400, detail=f"Cannot assign editor when status is '{assign_row.status}'")

        await db.execute(text("""
            UPDATE assignments
            SET editor_id = :user_id,
                status = 'editing',
                editor_assigned_at = :now
            WHERE order_id = :order_id
        """), {"user_id": str(user_row.id), "now": now, "order_id": order_id})

        # Transition order status: awaiting_quote/quoted/paid → processing
        await db.execute(text("""
            UPDATE orders SET status = 'processing'
            WHERE id = :order_id AND status IN ('awaiting_quote', 'quoted', 'paid')
        """), {"order_id": order_id})

    else:
        # proofreader
        assign_result = await db.execute(text("""
            SELECT status FROM assignments WHERE order_id = :order_id
        """), {"order_id": order_id})
        assign_row = assign_result.fetchone()
        if not assign_row:
            await db.execute(text("""
                INSERT INTO assignments (order_id, status)
                VALUES (:order_id, 'pending')
            """), {"order_id": order_id})
        elif assign_row.status not in ("editor_done", "proofreading"):
            raise HTTPException(status_code=400, detail=f"Cannot assign proofreader when status is '{assign_row.status}'")

        await db.execute(text("""
            UPDATE assignments
            SET proofreader_id = :user_id,
                status = 'proofreading',
                proofreader_assigned_at = :now
            WHERE order_id = :order_id
         """), {"user_id": str(user_row.id), "now": now, "order_id": order_id})

    await db.commit()

    event_type = EventType.EDITOR_ASSIGNED if body.role == "editor" else EventType.PROOFREADER_ASSIGNED
    await publish_event_sync(
        event_type=event_type,
        order_id=order_id,
        recipient_email=getattr(user_row, "email", None),
        data={"role": body.role},
    )

    result = await db.execute(text("""
        SELECT id, order_id, editor_id, qa_id, proofreader_id,
               status, assigned_at, editor_submitted_at, proofread_submitted_at, qa_submitted_at,
               editor_notes, proofreader_notes
        FROM assignments WHERE order_id = :order_id
    """), {"order_id": order_id})

    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Assignment not found")
    return AssignmentResponse(**dict(row._mapping))


# ── Literary Track: Mark Editor/Proofreader Complete ─────────────────────────
@router.post("/assignments/{order_id}/complete", response_model=AssignmentResponse)
async def complete_assignment(
    order_id: str,
    body:  AssignmentComplete,
    admin: dict             = Depends(get_admin_user),
    db:   AsyncSession      = Depends(get_db),
):
    """
    Mark editor or proofreader work as complete.
    Transitions: editing → editor_done, proofreading → proofread_done.
    """
    if body.role not in ("editor", "proofreader"):
        raise HTTPException(status_code=400, detail="role must be 'editor' or 'proofreader'")

    assign_result = await db.execute(text("""
        SELECT id, status, editor_id, proofreader_id
        FROM assignments WHERE order_id = :order_id
    """), {"order_id": order_id})

    assign_row = assign_result.fetchone()
    if not assign_row:
        raise HTTPException(status_code=404, detail="Assignment not found")

    now = datetime.now(timezone.utc)

    if body.role == "editor":
        if assign_row.status != "editing":
            raise HTTPException(status_code=400, detail=f"Editor can only complete when status is 'editing', got '{assign_row.status}'")
        await db.execute(text("""
            UPDATE assignments
            SET status = 'editor_done',
                editor_submitted_at = :now,
                editor_completed_at = :now
            WHERE order_id = :order_id
        """), {"now": now, "order_id": order_id})

    else:
        if assign_row.status not in ("proofreading", "editor_done"):
            raise HTTPException(status_code=400, detail=f"Proofreader can only complete when status is 'proofreading', got '{assign_row.status}'")
        await db.execute(text("""
            UPDATE assignments
            SET status = 'proofread_done',
                proofread_submitted_at = :now,
                proofreader_completed_at = :now
            WHERE order_id = :order_id
        """), {"now": now, "order_id": order_id})

    await db.commit()

    result = await db.execute(text("""
        SELECT id, order_id, editor_id, qa_id, proofreader_id,
               status, assigned_at, editor_submitted_at, proofread_submitted_at, qa_submitted_at,
               editor_notes, proofreader_notes
        FROM assignments WHERE order_id = :order_id
    """), {"order_id": order_id})

    row = result.fetchone()
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
            o.word_count, o.price_ntd, o.title, o.notes,
            o.has_sample_package,
            o.created_at, o.deadline_at, o.delivered_at,
            o.gcs_output_path, o.gcs_upload_path,
            p.payment_status, p.invoice_no,
            a.editor_id, a.qa_id, a.proofreader_id, a.status AS assignment_status
        FROM orders o
        LEFT JOIN payments p ON p.order_id = o.id
        LEFT JOIN assignments a ON a.order_id = o.id
        {where}
        ORDER BY o.created_at DESC
        LIMIT :limit OFFSET :offset
    """), params)

    rows = result.fetchall()
    orders = [AdminOrderDetail(**dict(r._mapping)) for r in rows]

    count_result = await db.execute(text(f"""
        SELECT COUNT(*) FROM orders o {where}
    """), {k: v for k, v in params.items() if k not in ("limit", "offset")})
    total = count_result.scalar()

    return OrderListResponse(orders=orders, total=total)


# ── Admin: 取得單一訂單詳情 ───────────────────────────────────────────────────
@router.get("/orders/{order_id}", response_model=AdminOrderDetail)
async def admin_get_order(
    order_id: str,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    result = await db.execute(text("""
        SELECT
            o.id, o.track_type, o.status, o.source_lang, o.target_lang,
            o.word_count, o.price_ntd, o.title, o.notes,
            o.has_sample_package,
            o.created_at, o.deadline_at, o.delivered_at,
            o.gcs_output_path, o.gcs_upload_path,
            p.payment_status, p.invoice_no,
            pj.qa_result,
            a.editor_id, a.qa_id, a.proofreader_id, a.status AS assignment_status
        FROM orders o
        LEFT JOIN payments p ON p.order_id = o.id
        LEFT JOIN pipeline_jobs pj ON pj.order_id = o.id AND pj.job_type = 'qa_auto'
        LEFT JOIN assignments a ON a.order_id = o.id
        WHERE o.id = :order_id
    """), {"order_id": order_id})
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    return AdminOrderDetail(**dict(row._mapping))


# ── Admin: 取得譯文下載 URL ───────────────────────────────────────────────────
@router.get("/orders/{order_id}/download-url", response_model=DownloadUrlResponse)
async def admin_get_download_url(
    order_id: str,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    result = await db.execute(text("""
        SELECT o.gcs_output_path FROM orders o WHERE o.id = :order_id
    """), {"order_id": order_id})
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    if not row.gcs_output_path:
        raise HTTPException(status_code=404, detail="Output file not found")
    signed_url = generate_download_signed_url(row.gcs_output_path)
    return DownloadUrlResponse(signed_url=signed_url, expires_in=3600)


@router.get("/orders/{order_id}/bilingual-download-url", response_model=DownloadUrlResponse)
async def admin_get_bilingual_download_url(
    order_id: str,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    result = await db.execute(text("""
        SELECT o.gcs_bilingual_output_path FROM orders o WHERE o.id = :order_id
    """), {"order_id": order_id})
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    if not row.gcs_bilingual_output_path:
        raise HTTPException(status_code=404, detail="Bilingual output file not found")
    signed_url = generate_download_signed_url(row.gcs_bilingual_output_path)
    return DownloadUrlResponse(signed_url=signed_url, expires_in=3600)


@router.get("/orders/{order_id}/plain-text-download-url", response_model=DownloadUrlResponse)
async def admin_get_plain_text_download_url(
    order_id: str,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    result = await db.execute(text("""
        SELECT o.gcs_plain_text_output_path FROM orders o WHERE o.id = :order_id
    """), {"order_id": order_id})
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    if not row.gcs_plain_text_output_path:
        raise HTTPException(status_code=404, detail="Plain text output file not found")
    signed_url = generate_download_signed_url(row.gcs_plain_text_output_path)
    return DownloadUrlResponse(signed_url=signed_url, expires_in=3600)


# ── Admin: Token Usage ───────────────────────────────────────────────────────
@router.get("/orders/{order_id}/token-usage", response_model=TokenUsageResponse)
async def admin_get_token_usage(
    order_id: str,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """Return aggregated token usage and cost for an order, grouped by job_type and model."""
    try:
        result = await db.execute(text("""
            SELECT
                job_type,
                model,
                SUM(prompt_tokens)     AS prompt_tokens,
                SUM(candidates_tokens) AS candidates_tokens,
                SUM(total_tokens)      AS total_tokens,
                SUM(cost_usd)          AS cost_usd,
                MAX(input_rate)        AS input_rate,
                MAX(output_rate)       AS output_rate
            FROM token_usage
            WHERE order_id = :order_id
            GROUP BY job_type, model
            ORDER BY job_type
        """), {"order_id": order_id})
    except ProgrammingError:
        raise HTTPException(status_code=404, detail="No token usage data for this order")
    items = result.fetchall()
    if not items:
        raise HTTPException(status_code=404, detail="No token usage data for this order")

    total_prompt = total_candidates = total_tokens = 0
    total_cost = 0.0
    breakdown = []
    for r in items:
        total_prompt += r.prompt_tokens
        total_candidates += r.candidates_tokens
        total_tokens += r.total_tokens
        total_cost += float(r.cost_usd)
        breakdown.append(TokenUsageItem(
            job_type=r.job_type,
            model=r.model,
            prompt_tokens=r.prompt_tokens,
            candidates_tokens=r.candidates_tokens,
            total_tokens=r.total_tokens,
            input_rate=float(r.input_rate),
            output_rate=float(r.output_rate),
            cost_usd=round(float(r.cost_usd), 6),
        ))

    return TokenUsageResponse(
        order_id=order_id,
        total_prompt=total_prompt,
        total_candidates=total_candidates,
        total_tokens=total_tokens,
        total_cost_usd=round(total_cost, 6),
        breakdown=breakdown,
    )


@router.get("/orders/{order_id}/token-usage-detail", response_model=TokenUsageDetailResponse)
async def admin_get_token_usage_detail(
    order_id: str,
    limit:  int            = Query(50, ge=1, le=500),
    offset: int            = Query(0, ge=0),
    admin:  dict           = Depends(get_admin_user),
    db:     AsyncSession   = Depends(get_db),
):
    """Return individual token-usage rows for an order with pagination."""
    try:
        count_res = await db.execute(text("""
            SELECT COUNT(*) FROM token_usage WHERE order_id = :order_id
        """), {"order_id": order_id})
        total = count_res.scalar() or 0

        if total == 0:
            raise HTTPException(status_code=404, detail="No token usage data for this order")

        result = await db.execute(text("""
            SELECT
                job_type, model,
                prompt_tokens, candidates_tokens, total_tokens,
                input_rate, output_rate, cost_usd, created_at
            FROM token_usage
            WHERE order_id = :order_id
            ORDER BY created_at
            LIMIT :limit OFFSET :offset
        """), {"order_id": order_id, "limit": limit, "offset": offset})
    except ProgrammingError:
        raise HTTPException(status_code=404, detail="No token usage data for this order")
    items = result.fetchall()

    return TokenUsageDetailResponse(
        order_id=order_id,
        total=total,
        items=[
            TokenUsageDetailItem(
                job_type=r.job_type,
                model=r.model,
                prompt_tokens=r.prompt_tokens,
                candidates_tokens=r.candidates_tokens,
                total_tokens=r.total_tokens,
                input_rate=float(r.input_rate),
                output_rate=float(r.output_rate),
                cost_usd=round(float(r.cost_usd), 6),
                created_at=r.created_at,
            )
            for r in items
        ],
    )


# ── Admin: 標記訂單已交付 ────────────────────────────────────────────────────
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


# ── Admin: 取得原始檔案內容 ─────────────────────────────────────────────────
@router.get("/orders/{order_id}/original-content", response_model=OriginalContentResponse)
async def admin_get_original_content(
    order_id: str,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    從 GCS 讀取客戶上傳的原始檔案，轉換為 HTML 供審查員對照。
    不產生 signed URL，檔案不離開伺服器。
    """
    result = await db.execute(text("""
        SELECT gcs_upload_path FROM orders WHERE id = :order_id
    """), {"order_id": order_id})
    row = result.fetchone()
    if not row or not row.gcs_upload_path:
        raise HTTPException(status_code=404, detail="No original file found for this order")

    try:
        raw_bytes, filename = read_blob(row.gcs_upload_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Original file not found in storage")

    doc = convert_document(raw_bytes, filename)
    return OriginalContentResponse(filename=doc.filename, content_type=doc.content_type, html=doc.html)


# ── Admin: 列出支援文件 ──────────────────────────────────────────────────────
@router.get("/orders/{order_id}/support-files", response_model=SupportFileListResponse)
async def admin_list_support_files(
    order_id: str,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """列出訂單的參考文件"""
    rows = await db.execute(text("""
        SELECT sf.id, sf.order_id, sf.filename, sf.content_type,
               sf.file_size, sf.gcs_path, sf.file_role, sf.created_at
        FROM order_support_files sf
        WHERE sf.order_id = :order_id
        ORDER BY sf.created_at ASC
    """), {"order_id": order_id})
    files = [SupportFileResponse(**dict(r._mapping)) for r in rows.fetchall()]
    return SupportFileListResponse(files=files, total=len(files))


@router.get("/orders/{order_id}/support-files/{file_id}/content", response_model=OriginalContentResponse)
async def admin_get_support_file_content(
    order_id: str,
    file_id: str,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """讀取特定支援檔案內容，轉換為 HTML"""
    result = await db.execute(text("""
        SELECT sf.gcs_path, sf.filename, sf.content_type
        FROM order_support_files sf
        WHERE sf.id = :file_id AND sf.order_id = :order_id
    """), {"file_id": file_id, "order_id": order_id})
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Support file not found")
    try:
        raw_bytes, filename = read_blob(row.gcs_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Support file not found in storage")
    doc = convert_document(raw_bytes, row.filename)
    return OriginalContentResponse(filename=doc.filename, content_type=doc.content_type, html=doc.html)


# ── Admin: 帳號管理 ───────────────────────────────────────────────────────────
@router.get("/users", response_model=UserListResponse)
async def list_users(
    limit:  int          = Query(50, ge=1, le=200),
    offset: int          = Query(0, ge=0),
    admin: dict          = Depends(get_admin_user),
    db:   AsyncSession   = Depends(get_db),
):
    """列出所有使用者帳號及其 admin 狀態"""
    params = {"limit": limit, "offset": offset}
    result = await db.execute(text("""
        SELECT
            u.id, u.uid_firebase, u.email, u.client_type,
            u.disabled, u.created_at,
            array_agg(DISTINCT ur.role) FILTER (WHERE ur.role IS NOT NULL) as roles,
            json_agg(DISTINCT jsonb_build_object('source_lang', ul.source_lang, 'target_lang', ul.target_lang)) FILTER (WHERE ul.source_lang IS NOT NULL) as languages
        FROM users u
        LEFT JOIN user_roles ur ON ur.user_id = u.id
        LEFT JOIN user_languages ul ON ul.user_id = u.id
        GROUP BY u.id
        ORDER BY u.created_at DESC
        LIMIT :limit OFFSET :offset
    """), params)
    rows = result.fetchall()
    users = []
    for r in rows:
        d = dict(r._mapping)
        roles = d.get("roles") or []
        langs = d.get("languages") or []
        users.append(UserListItem(
            **{**d, 
               "is_admin": "admin" in roles, 
               "is_editor": "editor" in roles,
               "is_qa": "qa" in roles,
               "admin_role": "admin" if "admin" in roles else None,
               "languages": langs}
        ))

    count_result = await db.execute(text("SELECT COUNT(*) FROM users"))
    total = count_result.scalar()

    return UserListResponse(users=users, total=total)


@router.patch("/users/{user_id}", response_model=MessageResponse)
async def update_user(
    user_id: str,
    body:  UserUpdateRequest,
    admin: dict             = Depends(get_admin_user),
    db:   AsyncSession      = Depends(get_db),
):
    """停用/啟用帳號；指派或撤銷 admin 權限"""
    result = await db.execute(
        text("SELECT id, uid_firebase, email FROM users WHERE id = :id"),
        {"id": user_id}
    )
    user_row = result.fetchone()
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")

    # 防止 admin 停用自己
    if body.disabled is True and str(user_row.uid_firebase) == admin["uid"]:
        raise HTTPException(status_code=400, detail="Cannot disable your own account")

    if body.disabled is not None:
        await db.execute(
            text("UPDATE users SET disabled = :disabled WHERE id = :id"),
            {"disabled": body.disabled, "id": user_id}
        )
        event_type = EventType.USER_DISABLED if body.disabled else EventType.USER_ENABLED
        await publish_event_sync(
            event_type=event_type,
            user_id=user_id,
            recipient_email=user_row.email,
        )

    if body.is_editor is not None:
        if body.is_editor:
            await db.execute(text("INSERT INTO user_roles (user_id, role) VALUES (:id, 'editor') ON CONFLICT DO NOTHING"), {"id": user_id})
        else:
            await db.execute(text("DELETE FROM user_roles WHERE user_id = :id AND role = 'editor'"), {"id": user_id})

    if body.is_qa is not None:
        if body.is_qa:
            await db.execute(text("INSERT INTO user_roles (user_id, role) VALUES (:id, 'qa') ON CONFLICT DO NOTHING"), {"id": user_id})
        else:
            await db.execute(text("DELETE FROM user_roles WHERE user_id = :id AND role = 'qa'"), {"id": user_id})

    if body.is_admin is True:
        await db.execute(text("INSERT INTO user_roles (user_id, role) VALUES (:id, 'admin') ON CONFLICT DO NOTHING"), {"id": user_id})
        # Keep admin_users table for now for backward compatibility or extra metadata
        await db.execute(text("""
            INSERT INTO admin_users (uid_firebase, email, role, active)
            VALUES (:uid, :email, 'admin', true)
            ON CONFLICT (uid_firebase) DO UPDATE SET active = true
        """), {"uid": user_row.uid_firebase, "email": user_row.email or ""})

    elif body.is_admin is False:
        # 防止 superadmin 自降
        if str(user_row.uid_firebase) == admin["uid"]:
            raise HTTPException(status_code=400, detail="Cannot remove your own admin role")
        await db.execute(text("DELETE FROM user_roles WHERE user_id = :id AND role = 'admin'"), {"id": user_id})
        await db.execute(
            text("UPDATE admin_users SET active = false WHERE uid_firebase = :uid"),
            {"uid": user_row.uid_firebase}
        )

    await db.commit()
    logger.info(f"User {user_id} updated by admin {admin['uid']}: {body.model_dump(exclude_none=True)}")
    return MessageResponse(message="User updated")


# ── Admin: QA Review Editor ──────────────────────────────────────────────────
@router.get("/orders/{order_id}/segments", response_model=QASegmentListResponse)
async def get_order_segments(
    order_id: str,
    limit:  int        = Query(50, ge=1, le=200),
    offset: int        = Query(0, ge=0),
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    獲取訂單的所有段落（原文、譯文、Raw 譯文、QA Flags）
    供 QA Review Editor 使用。
    """
    # 1. 從 GCS 讀取資料
    segments_raw = storage.read_temp_json(order_id, "segments.json")
    translations = storage.read_temp_json(order_id, "translations.json")
    trans_raw    = storage.read_temp_json(order_id, "translations_raw.json")

    if not segments_raw or not translations:
        raise HTTPException(status_code=404, detail="翻譯段落尚未產生，請等待 pipeline 完成後再試")

    # 2. 從 DB 讀取 QA Flags
    result = await db.execute(text("""
        SELECT qf.id, qf.job_id, pj.order_id,
               qf.paragraph_index, qf.flag_level, qf.flag_type,
               qf.source_segment, qf.translated_segment,
               qf.reviewer_note, qf.resolved, qf.flagged_at
        FROM qa_flags qf
        JOIN pipeline_jobs pj ON pj.id = qf.job_id
        WHERE pj.order_id = :order_id
    """), {"order_id": order_id})
    flags_rows = result.fetchall()
    
    # 建立 index 到 flags 的 mapping
    flags_map: dict[int, list] = {}
    for r in flags_rows:
        idx = r.paragraph_index
        if idx not in flags_map:
            flags_map[idx] = []
        flags_map[idx].append(QAFlagResponse(**dict(r._mapping)))

    # 建立 index 到 raw translation 的 mapping
    raw_map = {t["index"]: t["translated"] for t in trans_raw} if isinstance(trans_raw, list) else {}

    # 3. 合併資料
    res_segments = []
    # 譯文列表可能是 list of dict: [{"index": 0, "translated": "...", "comments": "..."}, ...]
    trans_map = {t["index"]: t for t in translations} if isinstance(translations, list) else {}

    for s in segments_raw:
        idx = s["index"]
        t = trans_map.get(idx, {})
        
        res_segments.append(QASegment(
            index      = idx,
            source     = s["text"],
            translated      = t.get("translated", ""),
            raw             = raw_map.get(idx),
            comments        = t.get("comments"),
            editor_comments = t.get("editor_comments"),
            flags           = flags_map.get(idx, []),
        ))

    total = len(res_segments)
    sliced = res_segments[offset:offset + limit]
    return QASegmentListResponse(segments=sliced, total=total)


@router.patch("/orders/{order_id}/segments", response_model=MessageResponse)
async def update_order_segments(
    order_id: str,
    body:  QASegmentsBatchUpdate,
    admin: dict             = Depends(get_admin_user),
):
    """
    批量更新段落譯文與備註（Save as Draft / Save）。
    """
    translations = storage.read_temp_json(order_id, "translations.json")
    if not translations:
         raise HTTPException(status_code=404, detail="Translations not found")

    trans_map = {t["index"]: t for t in translations}
    for up in body.segments:
        if up.index in trans_map:
            trans_map[up.index]["translated"] = up.translated
            if up.comments is not None:
                trans_map[up.index]["comments"] = up.comments
            if up.editor_comments is not None:
                trans_map[up.index]["editor_comments"] = up.editor_comments

    storage.write_temp_json(order_id, "translations.json", list(trans_map.values()))
    return MessageResponse(message="Segments updated")


@router.post("/orders/{order_id}/qa-done", response_model=MessageResponse)
async def mark_qa_done(
    order_id: str,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    完成 QA 審閱，將訂單狀態改為 delivered。
    """
    # 檢查該訂單是否存在
    result = await db.execute(text("SELECT id FROM orders WHERE id = :id"), {"id": order_id})
    if not result.fetchone():
        raise HTTPException(status_code=404, detail="Order not found")

    await db.execute(text("""
        UPDATE orders SET status = 'editor_verify' WHERE id = :id
    """), {"id": order_id})
    await db.commit()
    return MessageResponse(message="QA Review completed, order moved to editor_verify")


@router.patch("/orders/{order_id}/assign-editor", response_model=MessageResponse)
async def assign_editor(
    order_id: str,
    body:     EditorAssignRequest,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """指派或更換 Editor / QA"""
    editor_id = body.editor_id
    qa_id     = body.qa_id

    # 驗證 editor_id 是否合法
    if editor_id:
        res = await db.execute(text("""
            SELECT user_id FROM user_roles WHERE user_id = :id AND role = 'editor'
        """), {"id": editor_id})
        if not res.fetchone():
            raise HTTPException(status_code=400, detail="User is not an editor or not found")

    # 驗證 qa_id 是否合法
    if qa_id:
        res = await db.execute(text("""
            SELECT user_id FROM user_roles WHERE user_id = :id AND role = 'qa'
        """), {"id": qa_id})
        if not res.fetchone():
            raise HTTPException(status_code=400, detail="User is not a QA or not found")

    updates = []
    params: dict = {"order_id": order_id}
    if editor_id is not None:
        updates.append("editor_id = :editor_id")
        params["editor_id"] = editor_id
    if qa_id is not None:
        updates.append("qa_id = :qa_id")
        params["qa_id"] = qa_id

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    await db.execute(text(f"""
        UPDATE assignments SET {', '.join(updates)} WHERE order_id = :order_id
    """), params)
    await db.commit()
    return MessageResponse(message="Editor/QA assigned")


@router.patch("/orders/{order_id}/status", response_model=MessageResponse)
async def update_order_status(
    order_id: str,
    status:   str,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    手動更新訂單狀態（例如從 delivered 改回 qa_review）。
    """
    result = await db.execute(text("SELECT id FROM orders WHERE id = :id"), {"id": order_id})
    if not result.fetchone():
        raise HTTPException(status_code=404, detail="Order not found")

    await db.execute(text("""
        UPDATE orders SET status = :status WHERE id = :id
    """), {"status": status, "id": order_id})
    await db.commit()
    return MessageResponse(message=f"Order status updated to {status}")


@router.delete("/orders/{order_id}", response_model=MessageResponse)
async def admin_cancel_order(
    order_id: str,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    Admin cancels an order.
    Only allowed before payment (pending_payment / awaiting_quote / quoted).
    """
    result = await db.execute(text("""
        SELECT status FROM orders WHERE id = :id
    """), {"id": order_id})

    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")

    if row.status not in ("pending_payment", "awaiting_quote", "quoted"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot cancel order with status '{row.status}'"
        )

    await db.execute(text("""
        UPDATE orders SET status = 'cancelled' WHERE id = :id
    """), {"id": order_id})
    await db.commit()

    logger.info(f"Order cancelled by admin: {order_id}")
    return MessageResponse(message="Order cancelled")


@router.post("/orders/{order_id}/retranslate", response_model=MessageResponse)
async def retrigger_pipeline(
    order_id: str,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    重新觸發翻譯 Pipeline。
    用於訂單翻譯失敗、QA 不通過需重新翻譯等情況。
    """
    result = await db.execute(text("SELECT id, status FROM orders WHERE id = :id"), {"id": order_id})
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")

    await db.execute(text("""
        DELETE FROM qa_flags
        WHERE job_id IN (SELECT id FROM pipeline_jobs WHERE order_id = :id AND job_type = 'qa_auto')
    """), {"id": order_id})

    await db.execute(text("""
        UPDATE orders SET status = 'processing' WHERE id = :id
    """), {"id": order_id})
    await db.commit()

    await trigger_pipeline(order_id)

    logger.info(f"Pipeline re-triggered by admin: order={order_id}")
    return MessageResponse(message=f"Pipeline re-triggered for order {order_id}")


@router.put("/users/{user_id}/languages", response_model=MessageResponse)
async def update_user_languages(
    user_id: str,
    body:    UserLanguageUpdate,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """更新使用者的語言能力映射"""
    # 1. 刪除舊的
    await db.execute(text("DELETE FROM user_languages WHERE user_id = :id"), {"id": user_id})
    
    # 2. 插入新的
    for lang in body.languages:
        await db.execute(text("""
            INSERT INTO user_languages (user_id, source_lang, target_lang)
            VALUES (:user_id, :source_lang, :target_lang)
            ON CONFLICT DO NOTHING
        """), {
            "user_id":     user_id,
            "source_lang": lang.source_lang,
            "target_lang": lang.target_lang
        })
    
    await db.commit()
    return MessageResponse(message="Languages updated")


@router.get("/orders/{order_id}/eligible-users", response_model=UserListResponse)
async def list_eligible_users(
    order_id: str,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """列出符合該訂單語言要求的 Editor 和 QA"""
    # 1. 獲取訂單語言
    res = await db.execute(text("SELECT source_lang, target_lang FROM orders WHERE id = :id"), {"id": order_id})
    order = res.fetchone()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
        
    # 2. 篩選符合條件的使用者
    result = await db.execute(text("""
        SELECT 
            u.id, u.uid_firebase, u.email, u.client_type, u.disabled, u.created_at,
            array_agg(DISTINCT ur.role) FILTER (WHERE ur.role IS NOT NULL) as roles,
            json_agg(DISTINCT jsonb_build_object('source_lang', ul.source_lang, 'target_lang', ul.target_lang)) FILTER (WHERE ul.source_lang IS NOT NULL) as languages
        FROM users u
        JOIN user_languages ul ON ul.user_id = u.id
        JOIN user_roles ur ON ur.user_id = u.id
        WHERE ul.source_lang = :source AND ul.target_lang = :target
        GROUP BY u.id
    """), {"source": order.source_lang, "target": order.target_lang})
    
    rows = result.fetchall()
    users = []
    for r in rows:
        d = dict(r._mapping)
        roles = d.get("roles") or []
        users.append(UserListItem(
            **{**d, 
               "is_admin": "admin" in roles, 
               "is_editor": "editor" in roles,
               "is_qa": "qa" in roles,
               "admin_role": "admin" if "admin" in roles else None,
               "languages": d.get("languages") or []}
        ))
        
    return UserListResponse(users=users, total=len(users))
