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
from uuid import UUID
import base64
import json
import logging
from typing import Optional

from core.database import get_db
from core import storage
from core.config import settings
from core.storage import generate_download_signed_url, read_blob
from services.document_converter import convert_document
from routers.auth import get_admin_user
from models.schemas import (
    QAFlagResponse, QAFlagListResponse, QAFlagResolve,
    AssignmentUpdate, AssignmentResponse, AssignmentListResponse,
    AssignmentAction, AssignmentComplete,
    PaymentConfirm, MessageResponse, QuoteUpdate, RerunStageRequest,
    GutenbergImportResponse,
    OrderDetail, AdminOrderDetail, OrderListResponse,
    DownloadUrlResponse, OriginalContentResponse,
    UserListItem, UserListResponse, UserUpdateRequest, UserLanguageUpdate, UserLanguage,
    QASegment, QASegmentListResponse, QASegmentsBatchUpdate,
    EditorAssignRequest,
    SupportFileResponse, SupportFileListResponse,
    TokenUsageResponse, TokenUsageItem,
    TokenUsageDetailResponse, TokenUsageDetailItem,
    GutenbergBookInfo,
    GutenbergChapterItem, GutenbergChapterSegment, GutenbergChaptersResponse,
)
from services.payment import (
    get_payment_gateway, InvoiceRequest, InvoiceType, InvoiceError
)
from services.pipeline import (
    trigger_pipeline, trigger_deliver_job, trigger_rerun_stage,
    RERUN_STAGE_JOBS, RERUN_STAGE_ORDER,
)
from services.notification import publish_event_sync, EventType
from services import gutenberg as gutenberg_svc
from services.translation_versions import (
    save_translation_version as svc_save_version,
    list_versions as svc_list_versions,
    restore_version as svc_restore_version,
    diff_versions as svc_diff_versions,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


# ── Gutenberg Import ──────────────────────────────────────────────────────
@router.get("/gutenberg/{book_id}", response_model=GutenbergBookInfo)
async def preview_gutenberg_book(
    book_id: int,
    admin: dict              = Depends(get_admin_user),
):
    """
    Fetch book metadata from Gutendex and return a preview payload
    (title, authors, language, word_count, num_chapters, num_chunks).
    Does NOT create an order; use POST /admin/gutenberg/{book_id} to start.
    """
    try:
        return await gutenberg_svc.preview_book(book_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to preview Gutenberg book {book_id}: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Failed to fetch from Gutendex: {e}")


@router.post("/gutenberg/{book_id}", response_model=GutenbergImportResponse)
async def import_gutenberg_book(
    book_id: int,
    admin: dict              = Depends(get_admin_user),
    db:   AsyncSession      = Depends(get_db),
):
    """
    Admin triggers a Gutenberg book translation.
    Creates a 'gutenberg' track order and triggers the pipeline.
    """
    # Fetch book metadata first to get the real word count
    try:
        book_info = await gutenberg_svc.preview_book(book_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch Gutenberg book {book_id}: {e}")

    # Schema requires word_count > 0 and price_ntd > 0; Gutenberg orders
    # are admin-initiated (no payment) so we use the real word_count and
    # a placeholder price of 1. The user_id comes from the admin's row in
    # the users table (auto-created by get_current_user on first login).
    word_count = max(1, int(book_info.get("word_count") or 1))

    # Create order — user_id is looked up via subquery from the admin's
    # uid_firebase. All NOT NULL columns and CHECK constraints satisfied.
    result = await db.execute(text("""
        INSERT INTO orders (
            user_id, track_type, status, source_lang, target_lang,
            word_count, price_ntd, title
        )
        SELECT u.id, 'gutenberg', 'processing', 'en', 'zh-tw',
               :word_count, 1, :title
        FROM users u WHERE u.uid_firebase = :uid
        RETURNING id
    """), {
        "uid":        admin["uid"],
        "word_count": word_count,
        "title":      f"Gutenberg Book {book_id}",
    })

    order_row = result.fetchone()
    if not order_row:
        raise HTTPException(
            status_code=500,
            detail="Admin user has no matching users row; cannot create order",
        )
    order_id = str(order_row[0])

    # Store the gutenberg_book_id in notes (consumed by gt_fetcher)
    await db.execute(
        text("UPDATE orders SET notes = :notes WHERE id = :id"),
        {"notes": json.dumps({"gutenberg_book_id": book_id}), "id": order_id}
    )
    await db.commit()

    await trigger_pipeline(order_id)

    return GutenbergImportResponse(
        order_id=order_id,
        message=f"Gutenberg book {book_id} import triggered. Order ID: {order_id}",
    )


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


def _derive_delivery_path(gcs_path: str, suffix: str, new_ext: str) -> str | None:
    """Derive a sibling delivery path (e.g. HTML → _bilingual.html or _plain.txt)."""
    if not gcs_path:
        return None
    idx = gcs_path.rfind(".")
    if idx == -1:
        return None
    return gcs_path[:idx] + suffix + "." + new_ext


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
        SELECT o.gcs_output_path, o.gcs_bilingual_output_path FROM orders o WHERE o.id = :order_id
    """), {"order_id": order_id})
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")

    gcs_path = row.gcs_bilingual_output_path
    if not gcs_path:
        gcs_path = _derive_delivery_path(row.gcs_output_path, "_bilingual", "html")
    if not gcs_path:
        raise HTTPException(status_code=404, detail="Bilingual output file not found")

    signed_url = generate_download_signed_url(gcs_path)
    return DownloadUrlResponse(signed_url=signed_url, expires_in=3600)


@router.get("/orders/{order_id}/plain-text-download-url", response_model=DownloadUrlResponse)
async def admin_get_plain_text_download_url(
    order_id: str,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    result = await db.execute(text("""
        SELECT o.gcs_output_path, o.gcs_plain_text_output_path FROM orders o WHERE id = :order_id
    """), {"order_id": order_id})
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")

    gcs_path = row.gcs_plain_text_output_path
    if not gcs_path:
        gcs_path = _derive_delivery_path(row.gcs_output_path, "_plain", "txt")
    if not gcs_path:
        raise HTTPException(status_code=404, detail="Plain text output file not found")

    signed_url = generate_download_signed_url(gcs_path)
    return DownloadUrlResponse(signed_url=signed_url, expires_in=3600)


# ── Admin: Gutenberg Track Download URLs ───────────────────────────────────
# The Gutenberg track produces seven output artifacts in the orders/{id}/
# prefix; the frontend picks one via the ``version`` query param. The
# canonical mapping is:
#
#   standard          → full_translation.txt      (Traditional Chinese)
#   youth             → full_simplified.txt       (youth-friendly, whole-chapter)
#   tailo             → full_tailo.txt            (Hanzi + Tai-lo)
#   sxc               → source_vs_chinese.html    (原文 ↔ 標準翻譯)
#   simplified_tailo  → simplified_tailo.html     (簡化版 ↔ 台羅版)
#   simplified_reader → simplified_reader.html    (青少年讀本, single-column)
#   full_vs_simplified → full_vs_simplified.html  (標準翻譯 vs 青少年版)
GUTENBERG_FILE_MAP = {
    "standard":           "full_translation.txt",
    "youth":              "full_simplified.txt",
    "tailo":              "full_tailo.txt",
    "sxc":                "source_vs_chinese.html",
    "simplified_tailo":   "simplified_tailo.html",
    "simplified_reader":  "simplified_reader.html",
    "full_vs_simplified": "full_vs_simplified.html",
}


@router.get("/gutenberg/{order_id}/download-url", response_model=DownloadUrlResponse)
async def admin_get_gutenberg_download_url(
    order_id: str,
    version: str = "standard",
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """Generate a signed URL for a Gutenberg-track output artifact.

    The ``version`` query param selects which of the seven output files to
    point at; the mapping is centralised in ``GUTENBERG_FILE_MAP``.

    This endpoint replaces the previous 404 — it was added as part of the
    v2 segment-based rewrite (see change_logs/2026-06-05_gutenberg_v2_*).
    """
    if version not in GUTENBERG_FILE_MAP:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown version {version!r}. "
                   f"Valid: {sorted(GUTENBERG_FILE_MAP.keys())}",
        )

    result = await db.execute(
        text("SELECT o.track_type FROM orders o WHERE o.id = :order_id"),
        {"order_id": order_id},
    )
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    if row.track_type != "gutenberg":
        raise HTTPException(
            status_code=400,
            detail=f"Order {order_id} is not a Gutenberg order (track_type={row.track_type!r})",
        )

    filename = GUTENBERG_FILE_MAP[version]
    bucket_name = settings.gcs_outputs_bucket
    gcs_path = f"gs://{bucket_name}/orders/{order_id}/{filename}"

    client = storage.get_storage_client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(f"orders/{order_id}/{filename}")
    if not blob.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Gutenberg output {filename!r} not found for order {order_id} "
                   f"(deliver step may not have run yet)",
        )

    signed_url = generate_download_signed_url(gcs_path)
    return DownloadUrlResponse(signed_url=signed_url, expires_in=3600)


# ── Admin: Gutenberg Track Chapter Navigation ─────────────────────────────
# The v2 segment-based pipeline writes the following artifacts to the temp
# bucket (gcs_temp_bucket) under ``pipeline/{ORDER_ID}/``:
#
#   source/chapters.json   — chapter index with segment_start/segment_end
#   translated.json        — consolidated translations
#   simplified.json        — youth-friendly version
#   tailo.json             — Hanzi + Tai-lo pronunciation
#   metadata.json          — book-level info (incl. source_filename)
#
# This endpoint powers chapter-by-chapter viewing in the admin UI. By
# default it returns the chapter index only; ``?chapter=N`` returns the
# segments for that chapter (one HTTP round-trip per chapter — keeps each
# response small even for 100+ chapter books).
def _load_gutenberg_chapters_index(order_id: str) -> list[dict]:
    """Read ``pipeline/{order_id}/source/chapters.json`` from the temp bucket."""
    chapters = read_blob_as_json_or_none(
        f"pipeline/{order_id}/source/chapters.json"
    )
    if not isinstance(chapters, list):
        raise HTTPException(
            status_code=404,
            detail=f"chapters.json not found or malformed for order {order_id} "
                   f"(v2 pipeline may not have run for this order yet)",
        )
    return chapters


def _load_gutenberg_metadata(order_id: str) -> dict:
    """Read ``pipeline/{order_id}/metadata.json`` from the temp bucket."""
    return read_blob_as_json_or_none(f"pipeline/{order_id}/metadata.json") or {}


def _load_gutenberg_segments_json(order_id: str, filename: str) -> list[dict]:
    """Read one of the consolidated segment JSONs; returns [] if missing."""
    if filename not in ("translated.json", "simplified.json", "tailo.json"):
        raise ValueError(f"Unsafe filename {filename!r}")
    return read_blob_as_json_or_none(f"pipeline/{order_id}/{filename}") or []


def read_blob_as_json_or_none(gcs_path: str):
    """Helper: read a GCS blob as JSON, returning None if missing."""
    client = storage.get_storage_client()
    bucket = client.bucket(settings.gcs_temp_bucket)
    blob = bucket.blob(gcs_path)
    if not blob.exists():
        return None
    raw = blob.download_as_text(encoding="utf-8")
    return json.loads(raw)


@router.get("/gutenberg/{order_id}/chapters", response_model=GutenbergChaptersResponse)
async def admin_get_gutenberg_chapters(
    order_id: str,
    chapter: Optional[int] = Query(
        None,
        ge=0,
        description="If set, return the segments for this chapter index "
                    "in addition to the chapter index list.",
    ),
    version: str = Query(
        "all",
        description="When ?chapter=N is set, controls which translation "
                    "versions to include: 'all' returns source+translated+"
                    "simplified+tailo; any of 'standard', 'youth', 'tailo', "
                    "'sxc', 'simplified_tailo', 'full_vs_simplified' returns "
                    "the matching pair.",
    ),
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """Return the chapter index for a Gutenberg order; optionally with one
    chapter's segments included.

    The chapter index is cheap to load (it's a small JSON file in the temp
    bucket), so the default response contains only the index. When the
    caller passes ``?chapter=N`` the segments for that chapter are loaded
    from the three consolidated JSONs and returned alongside the index.

    Use this for chapter-by-chapter navigation in the admin viewer; the
    full-book HTML is still available via ``/download-url?version=sxc``
    for users who want a single scrolling view.
    """
    if chapter is not None and version not in (
        "all", "standard", "youth", "tailo", "sxc",
        "yvt", "comparison",
        "simplified_tailo", "full_vs_simplified",
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Unknown version {version!r}. Valid: all|standard|youth|tailo|sxc|yvt|comparison|simplified_tailo|full_vs_simplified",
        )

    result = await db.execute(
        text("SELECT o.track_type FROM orders o WHERE o.id = :order_id"),
        {"order_id": order_id},
    )
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    if row.track_type != "gutenberg":
        raise HTTPException(
            status_code=400,
            detail=f"Order {order_id} is not a Gutenberg order (track_type={row.track_type!r})",
        )

    raw_chapters = _load_gutenberg_chapters_index(order_id)
    metadata     = _load_gutenberg_metadata(order_id)
    chapters     = [
        GutenbergChapterItem(
            index=c.get("index", i),
            title=c.get("title", f"Chapter {i}"),
            segment_start=c.get("segment_start", 0),
            segment_end=c.get("segment_end", 0),
            segment_count=max(0, c.get("segment_end", 0) - c.get("segment_start", 0)),
            char_count=c.get("char_count", 0),
        )
        for i, c in enumerate(raw_chapters)
    ]
    total_segments = sum(ch.segment_count for ch in chapters)
    source_filename = metadata.get("source_filename") or metadata.get("title")

    response = GutenbergChaptersResponse(
        chapters=chapters,
        source_filename=source_filename,
        total_segments=total_segments,
    )

    if chapter is None:
        return response

    target = next((c for c in chapters if c.index == chapter), None)
    if target is None:
        raise HTTPException(
            status_code=404,
            detail=f"Chapter {chapter} not found (valid range: 0..{len(chapters) - 1})",
        )

    translated = _load_gutenberg_segments_json(order_id, "translated.json")
    simplified = _load_gutenberg_segments_json(order_id, "simplified.json")
    tailo      = _load_gutenberg_segments_json(order_id, "tailo.json")

    by_index_tr: dict[int, str] = {s.get("index", -1): s.get("translated", "") for s in translated}
    by_index_sm: dict[int, str] = {s.get("index", -1): s.get("translated", "") for s in simplified}
    by_index_to: dict[int, str] = {s.get("index", -1): s.get("translated", "") for s in tailo}

    segments: list[GutenbergChapterSegment] = []
    for seg in translated:
        idx = seg.get("index", -1)
        if idx < target.segment_start or idx >= target.segment_end:
            continue
        seg_obj = GutenbergChapterSegment(
            index=idx,
            chapter_index=target.index,
            chapter_title=target.title,
            source=seg.get("source", ""),
        )
        if version == "all":
            seg_obj.translated = by_index_tr.get(idx, "")
            seg_obj.simplified = by_index_sm.get(idx, "")
            seg_obj.tailo      = by_index_to.get(idx, "")
        elif version in ("standard", "sxc", "full_vs_simplified"):
            seg_obj.translated = by_index_tr.get(idx, "")
        elif version in ("youth", "yvt", "simplified_tailo"):
            seg_obj.translated = by_index_sm.get(idx, "")
        elif version in ("tailo", "comparison"):
            seg_obj.tailo = by_index_to.get(idx, "")
        segments.append(seg_obj)

    response.selected_chapter = target
    response.segments = segments
    response.version = version
    return response


# ── Admin: Pipeline Progress ─────────────────────────────────────────────────
CHECKPOINT_BATCH_PREFIX = "checkpoint_batch_"


@router.get("/orders/{order_id}/pipeline-progress")
async def admin_get_pipeline_progress(
    order_id: str,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """Return NMT batch translation progress by reading pipeline GCS artifacts.

    Returns:
      status: "no_batches" | "in_progress" | "complete"
      total_batches / completed_batches: batch-level counts
      total_segments / completed_segments: segment-level counts
    """
    # Verify order exists
    result = await db.execute(text("SELECT 1 FROM orders WHERE id = :order_id"), {"order_id": order_id})
    if not result.fetchone():
        raise HTTPException(status_code=404, detail="Order not found")

    client = storage.get_storage_client()
    bucket = client.bucket(settings.gcs_temp_bucket)

    # Read batches.json for total batch count
    try:
        batches_blob = bucket.blob(f"pipeline/{order_id}/batches.json")
        if not batches_blob.exists():
            return {
                "status": "no_batches",
                "total_batches": 0, "completed_batches": 0,
                "total_segments": 0, "completed_segments": 0,
            }
        batches_data = json.loads(batches_blob.download_as_text())
        total_batches = len(batches_data) if isinstance(batches_data, list) else 0
    except Exception:
        return {
            "status": "no_batches",
            "total_batches": 0, "completed_batches": 0,
            "total_segments": 0, "completed_segments": 0,
        }

    # Count checkpoint blobs for completed batches
    prefix = f"pipeline/{order_id}/{CHECKPOINT_BATCH_PREFIX}"
    checkpoints = list(bucket.list_blobs(prefix=prefix))
    completed_batches = len(checkpoints)

    # Read segments.json for total segments
    try:
        segs_blob = bucket.blob(f"pipeline/{order_id}/segments.json")
        if segs_blob.exists():
            segs_data = json.loads(segs_blob.download_as_text())
            total_segments = len(segs_data) if isinstance(segs_data, list) else 0
        else:
            total_segments = 0
    except Exception:
        total_segments = 0

    # Sum non-empty translations from all checkpoints
    completed_segments = 0
    for cp in checkpoints:
        try:
            data = json.loads(cp.download_as_text())
            translations = data.get("translations", [])
            completed_segments += sum(1 for t in translations if t)
        except Exception:
            pass

    # If translations.json exists, the pipeline completed its final write
    # regardless of whether individual batch checkpoints exist (e.g. after
    # retry exhaustion — those segments are flagged as must_fix).
    try:
        out_blob = bucket.blob(f"pipeline/{order_id}/translations.json")
        if out_blob.exists():
            status = "complete"
        elif total_batches == 0:
            status = "no_batches"
        elif completed_batches >= total_batches:
            status = "complete"
        else:
            status = "in_progress"
    except Exception:
        status = "in_progress"

    return {
        "status": status,
        "total_batches": total_batches,
        "completed_batches": completed_batches,
        "total_segments": total_segments,
        "completed_segments": completed_segments,
    }


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
            ORDER BY created_at DESC
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


# ── Translation Version History ──────────────────────────────────────────────
VERSION_SOURCE_MAP = {
    "list": "manual", "save": "manual", "restore": "restored",
}


@router.get("/orders/{order_id}/versions")
async def admin_list_versions(
    order_id: str,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """List all translation versions for an order."""
    return await svc_list_versions(db, order_id)


@router.post("/orders/{order_id}/versions")
async def admin_save_version(
    order_id: str,
    label: str | None = None,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """Manually save a translation version snapshot."""
    result = await svc_save_version(db, order_id, source="manual", label=label, created_by=str(admin["uid"]))
    if not result:
        raise HTTPException(status_code=404, detail="No translations.json found; pipeline may not have run yet")
    return result


@router.post("/orders/{order_id}/versions/{version_id}/restore")
async def admin_restore_version(
    order_id: str,
    version_id: str,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """Restore translations from a version snapshot."""
    result = await svc_restore_version(db, order_id, version_id, restored_by=str(admin["uid"]))
    if not result:
        raise HTTPException(status_code=404, detail="Version not found")
    return result


@router.get("/orders/{order_id}/versions/{version_id}/diff")
async def admin_diff_versions(
    order_id: str,
    version_id: str,
    against: UUID | None = None,
    admin: dict          = Depends(get_admin_user),
    db:   AsyncSession   = Depends(get_db),
):
    """Diff a version against another version (or the latest if omitted)."""
    if against is None:
        result = await db.execute(text("""
            SELECT id FROM translation_versions
            WHERE order_id = :order_id AND id != :vid
            ORDER BY version DESC LIMIT 1
        """), {"order_id": order_id, "vid": version_id})
        row = result.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="No other version to diff against")
        against = row.id
    try:
        return await svc_diff_versions(db, order_id, str(version_id), str(against))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/orders/{order_id}/versions/live/diff")
async def admin_diff_live(
    order_id: str,
    against: UUID,
    admin: dict          = Depends(get_admin_user),
    db:   AsyncSession   = Depends(get_db),
):
    """Diff current translations.json against a stored version."""
    from core.storage import read_temp_json
    live = read_temp_json(order_id, "translations.json")
    if not live:
        raise HTTPException(status_code=404, detail="No current translations.json found")

    result = await db.execute(text("""
        SELECT gcs_path FROM translation_versions
        WHERE id = :vid AND order_id = :order_id
    """), {"vid": str(against), "order_id": order_id})
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Version not found")

    from services.translation_versions import gcs_download_content
    stored = json.loads(gcs_download_content(row.gcs_path))

    def _build_diff(segs_a: list, segs_b: list) -> dict:
        map_a = {s["index"]: s for s in segs_a}
        map_b = {s["index"]: s for s in segs_b}
        changed, added, removed = [], [], []
        for idx in sorted(set(map_a) | set(map_b)):
            a, b = map_a.get(idx), map_b.get(idx)
            if a is None and b:
                added.append({"index": idx, "source": b.get("source", ""), "text": b["translated"]})
            elif a and b is None:
                removed.append({"index": idx, "source": a.get("source", ""), "text": a["translated"]})
            elif a and b and a.get("translated") != b.get("translated"):
                changed.append({
                    "index": idx, "source": a.get("source", ""),
                    "old": a.get("translated", ""), "new": b.get("translated", ""),
                })
        return {"changed": changed, "added": added, "removed": removed}

    return _build_diff(json.loads(gcs_download_content(row.gcs_path)) if isinstance(live, str) else live, live)


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
    q:        str        = Query("", description="Search keyword across source, translated, and comments"),
    search_all: bool    = Query(False, description="If true, search across all segments before paginating"),
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

    if search_all and q:
        q_lower = q.strip().lower()
        res_segments = [s for s in res_segments if (
            q_lower in (s.source or "").lower()
            or q_lower in (s.translated or "").lower()
            or q_lower in (s.comments or "").lower()
            or q_lower in (s.editor_comments or "").lower()
        )]
        total = len(res_segments)
        sliced = res_segments[offset:offset + limit]
        return QASegmentListResponse(segments=sliced, total=total)

    total_unfiltered = len(res_segments)
    sliced = res_segments[offset:offset + limit]

    if q:
        q_lower = q.strip().lower()
        sliced = [s for s in sliced if (
            q_lower in (s.source or "").lower()
            or q_lower in (s.translated or "").lower()
            or q_lower in (s.comments or "").lower()
            or q_lower in (s.editor_comments or "").lower()
        )]

    return QASegmentListResponse(segments=sliced, total=total_unfiltered)


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
    重新觸發翻譯 Pipeline（管理員專用）。
    可在任何訂單狀態下執行，包括編輯進行中。
    將清除既有翻譯、QA 標記、段落編輯與 Pipeline Job 紀錄。
    """
    result = await db.execute(text("SELECT id, status FROM orders WHERE id = :id"), {"id": order_id})
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")

    await svc_save_version(db, order_id, source="pre_retranslate", created_by=str(admin["uid"]))

    # Clean all QA flags across all job types
    await db.execute(text("""
        DELETE FROM qa_flags
        WHERE job_id IN (SELECT id FROM pipeline_jobs WHERE order_id = :id)
    """), {"id": order_id})

    # Reset literary assignments
    await db.execute(text("""
        UPDATE assignments SET status = 'pending' WHERE order_id = :id
    """), {"id": order_id})

    # Reset pipeline jobs so checkpoints aren't reused
    await db.execute(text("DELETE FROM pipeline_jobs WHERE order_id = :id"), {"id": order_id})

    await db.execute(text("""
        UPDATE orders SET status = 'processing', delivered_at = NULL WHERE id = :id
    """), {"id": order_id})
    await db.commit()

    await trigger_pipeline(order_id)

    logger.info(f"Pipeline re-triggered by admin: order={order_id} (prior status: {row.status})")
    return MessageResponse(
        message=(
            f"⚠️ Pipeline 已重新觸發。訂單 {order_id} 的所有翻譯、"
            f"QA 標記、段落編輯紀錄及 Pipeline Job 資料均已清除。"
        )
    )


@router.post("/orders/{order_id}/redeliver", response_model=MessageResponse)
async def redeliver(
    order_id: str,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    僅重新生成交付檔案（不重新翻譯）。
    觸發 deliver Cloud Run Job 並傳入 REDELIVER=true。
    """
    result = await db.execute(text("""
        SELECT id, track_type FROM orders WHERE id = :id
    """), {"id": order_id})
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")

    try:
        await trigger_deliver_job(order_id, row.track_type)
    except Exception as e:
        logger.error(f"Failed to trigger deliver job: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    logger.info(f"Deliver job re-triggered: order={order_id}, track={row.track_type}")
    return MessageResponse(message="Deliver job triggered")


@router.post("/orders/{order_id}/rerun-stage", response_model=MessageResponse)
async def rerun_stage(
    order_id: str,
    body:    RerunStageRequest,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    Rerun a specific stage (or all stages) of the Gutenberg pipeline.

    Stages: fetcher, chapter_splitter, extract_terms, translate, simplify,
    tailo, deliver, all. Each is a separate Cloud Run Job (ots-gt-*); this
    endpoint triggers the chosen one(s) directly without going through
    Cloud Workflows.

    `stage="all"` runs the seven stages in order
    (fetcher → chapter_splitter → ... → deliver).
    Stages are fire-and-forget; poll `pipeline_jobs` for completion.
    """
    allowed = set(RERUN_STAGE_JOBS) | {"all"}
    if body.stage not in allowed:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid stage: {body.stage!r}. "
                f"Must be one of: {', '.join(sorted(allowed))}."
            ),
        )

    result = await db.execute(
        text("SELECT id, track_type FROM orders WHERE id = :id"),
        {"id": order_id},
    )
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    if row.track_type != "gutenberg":
        raise HTTPException(
            status_code=400,
            detail=(
                f"rerun-stage is for Gutenberg orders only; "
                f"this order is {row.track_type!r}"
            ),
        )

    try:
        triggered = await trigger_rerun_stage(order_id, body.stage)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to trigger rerun-stage: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    stages_run = (
        "all ({} stages)".format(len(RERUN_STAGE_ORDER))
        if body.stage == "all"
        else body.stage
    )
    logger.info(
        f"Rerun-stage triggered by admin: order={order_id}, "
        f"stage={stages_run}, jobs=[{triggered}]"
    )
    return MessageResponse(
        message=f"Stage(s) triggered: {stages_run}. Jobs: {triggered}."
    )


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


# ── Video Materials (Gutenberg Track) ────────────────────────────────────────

@router.get("/orders/{order_id}/video-materials")
async def admin_get_video_materials(
    order_id: str,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """Fetch the video_materials.json storyboard + existing assets for a Gutenberg order."""
    row = await db.execute(
        text("SELECT track_type FROM orders WHERE id = :id"),
        {"id": order_id},
    )
    r = row.fetchone()
    if not r:
        raise HTTPException(404, "Order not found")
    if r.track_type != "gutenberg":
        raise HTTPException(400, "Only Gutenberg orders have video materials")

    from core.storage import get_storage_client, generate_signed_url
    from datetime import timedelta

    client = get_storage_client()
    temp_bucket = client.bucket(settings.gcs_temp_bucket)
    out_bucket = client.bucket(settings.gcs_outputs_bucket)

    # Load storyboard
    blob = temp_bucket.blob(f"pipeline/{order_id}/video_materials.json")
    if not blob.exists():
        return {"materials": None, "message": "Video materials not yet generated"}
    content = blob.download_as_text(encoding="utf-8")
    try:
        materials = json.loads(content)
    except json.JSONDecodeError:
        raise HTTPException(500, "Invalid video_materials.json")

    # Check for existing scene assets (per-language track)
    scene_assets = {}
    chapters = materials.get("chapters", [])
    for ch in chapters:
        ch_idx = ch["chapter_index"]
        for scene in ch.get("scenes", []):
            s_idx = scene["scene_index"]
            tracks = scene.get("tracks", {})
            languages = list(tracks.keys()) if tracks else ["zh"]
            for lang in languages:
                key = f"{ch_idx}_{s_idx}_{lang}"
                entry = {"audio_url": None, "audio_duration": None, "video_url": None}
                audio_path = f"pipeline/{order_id}/scenes/{ch_idx}_{s_idx}/{lang}/narration.wav"
                audio_blob = temp_bucket.blob(audio_path)
                if audio_blob.exists():
                    entry["audio_url"] = generate_signed_url(settings.gcs_temp_bucket, audio_path)
                    try:
                        import io
                        import wave as wave_mod
                        wav_bytes = audio_blob.download_as_bytes()
                        with wave_mod.open(io.BytesIO(wav_bytes), 'r') as wf:
                            entry["audio_duration"] = round(wf.getnframes() / wf.getframerate(), 2)
                    except Exception:
                        entry["audio_duration"] = None
                video_path = f"pipeline/{order_id}/scenes/{ch_idx}_{s_idx}/{lang}/scene_video.mp4"
                if temp_bucket.blob(video_path).exists():
                    entry["video_url"] = generate_signed_url(settings.gcs_temp_bucket, video_path)
                scene_assets[key] = entry

    # Check for existing chapter videos & SRTs
    chapter_videos = {}
    chapter_srts = {}
    for ch in chapters:
        ch_idx = ch["chapter_index"]
        ch_path = f"orders/{order_id}/chapter_{ch_idx:02d}.mp4"
        if out_bucket.blob(ch_path).exists():
            chapter_videos[str(ch_idx)] = generate_signed_url(settings.gcs_outputs_bucket, ch_path)
        srt_path = f"orders/{order_id}/chapter_{ch_idx:02d}.srt"
        if out_bucket.blob(srt_path).exists():
            chapter_srts[str(ch_idx)] = generate_signed_url(settings.gcs_outputs_bucket, srt_path)

    return {
        "materials": materials,
        "scene_assets": scene_assets,
        "chapter_videos": chapter_videos,
        "chapter_srts": chapter_srts,
    }


@router.post("/orders/{order_id}/video-materials/generate-storyboard")
async def admin_generate_storyboard(
    order_id: str,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """Trigger gt_video_prep job to generate storyboard for a Gutenberg order."""
    # Verify the order exists and is Gutenberg track
    result = await db.execute(
        text("SELECT id, track_type FROM orders WHERE id = :oid"),
        {"oid": order_id},
    )
    row = result.fetchone()
    if not row:
        raise HTTPException(404, "Order not found")
    if row.track_type != "gutenberg":
        raise HTTPException(400, "Storyboard generation is only for Gutenberg orders")

    from services.pipeline import trigger_video_prep_job
    await trigger_video_prep_job(order_id)
    return {"message": "Storyboard generation triggered", "order_id": order_id}


@router.put("/orders/{order_id}/video-materials")
async def admin_save_video_materials(
    order_id: str,
    body: dict,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """Save edited video_materials.json storyboard."""
    from core.storage import get_storage_client

    row = await db.execute(
        text("SELECT track_type FROM orders WHERE id = :id"),
        {"id": order_id},
    )
    r = row.fetchone()
    if not r:
        raise HTTPException(404, "Order not found")
    if r.track_type != "gutenberg":
        raise HTTPException(400, "Only Gutenberg orders have video materials")

    materials = body.get("materials")
    if not materials:
        raise HTTPException(400, "materials field is required")

    raw = json.dumps(materials, ensure_ascii=False, indent=2)
    client = get_storage_client()
    bucket = client.bucket(settings.gcs_temp_bucket)
    bucket.blob(f"pipeline/{order_id}/video_materials.json").upload_from_string(
        raw.encode("utf-8"), content_type="application/json",
    )
    return {"message": "Video materials saved"}


@router.post("/orders/{order_id}/video-materials/scene/tts")
async def admin_scene_tts(
    order_id: str,
    body: dict,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """Generate TTS audio for a single scene.

    Stores audio at `scenes/{ch_idx}_{s_idx}/{language}/narration.wav`
    so each language track has its own audio file.
    """
    ch_idx = body.get("chapter_index")
    s_idx = body.get("scene_index")
    text = body.get("text", "")
    voice_id = body.get("voice_id", "cmn-TW-vs2-F04")
    speaking_rate = body.get("speaking_rate", 1.0)
    language = body.get("language", "")
    short_pause_duration = body.get("short_pause_duration", 150)
    long_pause_duration = body.get("long_pause_duration", 450)

    if ch_idx is None or s_idx is None or not text:
        raise HTTPException(400, "chapter_index, scene_index, and text are required")

    # Infer language from voice_id if not explicitly provided
    if not language:
        language = "tai-lo" if voice_id.startswith("nan-") else "zh"

    bronci_lang_code = "nan-TW" if language == "tai-lo" else "cmn-TW"

    from services.video_gen_service import synthesize_speech
    wav_bytes = synthesize_speech(text, voice_id=voice_id, speaking_rate=speaking_rate,
                                  language_code=bronci_lang_code,
                                  short_pause_duration=short_pause_duration,
                                  long_pause_duration=long_pause_duration)

    # Compute duration from WAV header
    import io
    import wave as wave_mod
    with wave_mod.open(io.BytesIO(wav_bytes), 'r') as wf:
        duration_sec = round(wf.getnframes() / wf.getframerate(), 2)

    audio_b64 = base64.b64encode(wav_bytes).decode("utf-8")
    data_url = f"data:audio/wav;base64,{audio_b64}"

    # Persist to GCS with language in path
    from core.storage import get_storage_client
    client = get_storage_client()
    bucket = client.bucket(settings.gcs_temp_bucket)
    blob_path = f"pipeline/{order_id}/scenes/{ch_idx}_{s_idx}/{language}/narration.wav"
    bucket.blob(blob_path).upload_from_string(wav_bytes, content_type="audio/wav")

    return {
        "audio_data_url": data_url,
        "gcs_path": f"gs://{settings.gcs_temp_bucket}/{blob_path}",
        "duration_sec": duration_sec,
    }


@router.post("/orders/{order_id}/video-materials/scene/image")
async def admin_scene_image(
    order_id: str,
    body: dict,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """Generate image for a single scene."""
    ch_idx = body.get("chapter_index")
    s_idx = body.get("scene_index")
    prompt = body.get("prompt", "")

    if ch_idx is None or s_idx is None or not prompt:
        raise HTTPException(400, "chapter_index, scene_index, and prompt are required")

    from services.video_gen_service import generate_image
    jpg_bytes = generate_image(prompt)
    img_b64 = base64.b64encode(jpg_bytes).decode("utf-8")
    data_url = f"data:image/jpeg;base64,{img_b64}"

    # Persist to GCS
    from core.storage import get_storage_client
    client = get_storage_client()
    bucket = client.bucket(settings.gcs_temp_bucket)
    blob_path = f"pipeline/{order_id}/scenes/{ch_idx}_{s_idx}/visual.jpg"
    bucket.blob(blob_path).upload_from_string(jpg_bytes, content_type="image/jpeg")

    return {"image_data_url": data_url, "gcs_path": f"gs://{settings.gcs_temp_bucket}/{blob_path}"}


@router.post("/orders/{order_id}/video-materials/scene/retranslate")
async def admin_scene_retranslate(
    order_id: str,
    body: dict,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """Re-translate a single scene's Chinese narration to Tai-lo.

    Reads the scene's `tracks.zh.narration_text`, calls Gemini for Tai-lo
    translation, updates `tracks["tai-lo"].narration_text` in place, and
    clears the stale Tai-lo audio asset so it gets regenerated on next TTS.
    """
    ch_idx = body.get("chapter_index")
    s_idx = body.get("scene_index")
    if ch_idx is None or s_idx is None:
        raise HTTPException(400, "chapter_index and scene_index are required")

    row = await db.execute(
        text("SELECT track_type FROM orders WHERE id = :id"),
        {"id": order_id},
    )
    r = row.fetchone()
    if not r:
        raise HTTPException(404, "Order not found")
    if r.track_type != "gutenberg":
        raise HTTPException(400, "Video materials are only for Gutenberg orders")

    from core.storage import get_storage_client

    client = get_storage_client()
    bucket = client.bucket(settings.gcs_temp_bucket)
    blob = bucket.blob(f"pipeline/{order_id}/video_materials.json")
    if not blob.exists():
        raise HTTPException(404, "video_materials.json not found — generate storyboard first")

    materials = json.loads(blob.download_as_text(encoding="utf-8"))
    chapters = materials.get("chapters", [])
    scene = None
    for ch in chapters:
        if ch["chapter_index"] == ch_idx:
            for sc in ch.get("scenes", []):
                if sc["scene_index"] == s_idx:
                    scene = sc
                    break
            break

    if scene is None:
        raise HTTPException(404, f"Scene {ch_idx}.{s_idx} not found")

    tracks = scene.get("tracks", {})
    zh_text = tracks.get("zh", {}).get("narration_text", "")
    if not zh_text:
        raise HTTPException(400, "Chinese narration text is empty — nothing to translate")

    from services.tai_lo_translator import translate_to_tai_lo

    tai_lo_text = translate_to_tai_lo(zh_text)

    if "tai-lo" not in tracks:
        tracks["tai-lo"] = {}
    tracks["tai-lo"]["narration_text"] = tai_lo_text
    scene["tracks"] = tracks

    # Mark tracks as modified for auto-save detection
    blob.upload_from_string(
        json.dumps(materials, ensure_ascii=False),
        content_type="application/json; charset=utf-8",
    )

    # Clear stale Tai-lo audio asset so it gets regenerated
    audio_path = f"pipeline/{order_id}/scenes/{ch_idx}_{s_idx}/tai-lo/narration.wav"
    audio_blob = bucket.blob(audio_path)
    if audio_blob.exists():
        audio_blob.delete()

    logger.info(
        f"Scene {ch_idx}.{s_idx} retranslated: zh={len(zh_text)} chars → tai-lo={len(tai_lo_text)} chars"
    )
    return {"tai_lo_text": tai_lo_text}


@router.post("/orders/{order_id}/video-materials/scene/assemble")
async def admin_scene_assemble(
    order_id: str,
    body: dict,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """Assemble audio + image into a video clip for a single scene."""
    ch_idx = body.get("chapter_index")
    s_idx = body.get("scene_index")

    if ch_idx is None or s_idx is None:
        raise HTTPException(400, "chapter_index and scene_index are required")

    from services.video_gen_service import assemble_scene_video
    from core.storage import get_storage_client

    # Load persisted assets from GCS
    client = get_storage_client()
    bucket = client.bucket(settings.gcs_temp_bucket)

    wav_blob = bucket.blob(f"pipeline/{order_id}/scenes/{ch_idx}_{s_idx}/narration.wav")
    jpg_blob = bucket.blob(f"pipeline/{order_id}/scenes/{ch_idx}_{s_idx}/visual.jpg")

    if not wav_blob.exists() or not jpg_blob.exists():
        raise HTTPException(400, "Generate TTS and image first before assembling video")

    audio_bytes = wav_blob.download_as_bytes()
    image_bytes = jpg_blob.download_as_bytes()

    mp4_bytes = assemble_scene_video(audio_bytes, image_bytes)
    if mp4_bytes is None:
        raise HTTPException(500, "Video assembly failed — FFmpeg may be unavailable")

    # Persist to GCS
    blob_path = f"pipeline/{order_id}/scenes/{ch_idx}_{s_idx}/scene_video.mp4"
    bucket.blob(blob_path).upload_from_string(mp4_bytes, content_type="video/mp4")

    video_b64 = base64.b64encode(mp4_bytes).decode("utf-8")
    return {
        "video_data_url": f"data:video/mp4;base64,{video_b64}",
        "gcs_path": f"gs://{settings.gcs_temp_bucket}/{blob_path}",
    }


@router.post("/orders/{order_id}/video-materials/chapter/assemble")
async def admin_chapter_assemble(
    order_id: str,
    body: dict,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """Assemble all scenes in a chapter into one MP4 with a title card.

    The `language` param selects which narration track to use
    for SRT subtitles and audio assets. Defaults to 'zh'.
    """
    ch_idx = body.get("chapter_index")
    language = body.get("language", "zh")
    if ch_idx is None:
        raise HTTPException(400, "chapter_index is required")

    from core.storage import get_storage_client

    # Load storyboard to get scenes + title
    client = get_storage_client()
    temp_bucket = client.bucket(settings.gcs_temp_bucket)
    blob = temp_bucket.blob(f"pipeline/{order_id}/video_materials.json")
    if not blob.exists():
        raise HTTPException(400, "video_materials.json not found")
    materials = json.loads(blob.download_as_text(encoding="utf-8"))
    chapters = materials.get("chapters", [])
    chapter = next((ch for ch in chapters if ch["chapter_index"] == ch_idx), None)
    if not chapter:
        raise HTTPException(404, f"Chapter {ch_idx} not found")

    from services.video_gen_service import assemble_chapter_video
    mp4_bytes, srt_content = assemble_chapter_video(
        order_id=order_id,
        chapter_index=ch_idx,
        scenes=chapter.get("scenes", []),
        title=chapter.get("title", ""),
        language=language,
    )
    if mp4_bytes is None:
        raise HTTPException(500, "Chapter assembly failed — generate audio + image for all scenes first")

    out_bucket = client.bucket(settings.gcs_outputs_bucket)
    blob_path = f"orders/{order_id}/chapter_{ch_idx:02d}_{language}.mp4"
    out_bucket.blob(blob_path).upload_from_string(mp4_bytes, content_type="video/mp4")

    # Upload SRT alongside the video
    srt_path = f"orders/{order_id}/chapter_{ch_idx:02d}_{language}.srt"
    if srt_content:
        out_bucket.blob(srt_path).upload_from_string(srt_content, content_type="text/plain; charset=utf-8")

    from core.storage import generate_signed_url
    video_url = generate_signed_url(settings.gcs_outputs_bucket, blob_path)
    srt_url = generate_signed_url(settings.gcs_outputs_bucket, srt_path) if srt_content else None

    return {"video_url": video_url, "srt_url": srt_url, "gcs_path": f"gs://{settings.gcs_outputs_bucket}/{blob_path}"}


@router.post("/orders/{order_id}/video-materials/scene/video")
async def admin_scene_video(
    order_id: str,
    body: dict,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """Generate scene video: LTX 2.3 Fast → overlay TTS audio.

    Reads `visual_prompt` from video_materials.json, generates raw video
    via LTX 2.3 Fast, then overlays the TTS audio track.
    Stores at `scenes/{ch_idx}_{s_idx}/{language}/scene_video.mp4`.
    """
    ch_idx = body.get("chapter_index")
    s_idx = body.get("scene_index")
    language = body.get("language", "zh")

    if ch_idx is None or s_idx is None:
        raise HTTPException(400, "chapter_index and scene_index are required")

    if not settings.fal_api_key:
        raise HTTPException(500, "FAL_API_KEY not configured")

    from core.storage import get_storage_client
    client = get_storage_client()
    bucket = client.bucket(settings.gcs_temp_bucket)

    # Load storyboard to get visual_prompt
    blob = bucket.blob(f"pipeline/{order_id}/video_materials.json")
    if not blob.exists():
        raise HTTPException(404, "video_materials.json not found — generate storyboard first")
    materials = json.loads(blob.download_as_text(encoding="utf-8"))
    chapters = materials.get("chapters", [])
    scene = None
    for ch in chapters:
        if ch["chapter_index"] == ch_idx:
            for sc in ch.get("scenes", []):
                if sc["scene_index"] == s_idx:
                    scene = sc
                    break
            break
    if scene is None:
        raise HTTPException(404, f"Scene {ch_idx}.{s_idx} not found")

    prompt = scene.get("visual_prompt", "")
    if not prompt:
        raise HTTPException(400, "visual_prompt is empty — edit the storyboard first")

    # Load TTS audio from GCS
    audio_path = f"pipeline/{order_id}/scenes/{ch_idx}_{s_idx}/{language}/narration.wav"
    audio_blob = bucket.blob(audio_path)
    if not audio_blob.exists():
        raise HTTPException(400, "TTS audio not found — generate TTS first")

    audio_bytes = audio_blob.download_as_bytes()

    import wave as wave_mod
    import io
    with wave_mod.open(io.BytesIO(audio_bytes), 'r') as wf:
        audio_dur = wf.getnframes() / wf.getframerate()

    # Choose video model based on audio duration
    from services.video_gen_service import FalLtxClient, FalPixVerseClient, assemble_scene_video_from_clip
    if audio_dur < 15:
        model_name = "PixVerse V6"
        client = FalPixVerseClient(settings.fal_api_key)
        raw_video = client.generate(prompt, audio_dur)
    else:
        model_name = "LTX 2.3 Fast"
        client = FalLtxClient(settings.fal_api_key)
        raw_video = client.generate(prompt, audio_dur)
    logger.info(f"{model_name} generating for scene {ch_idx}.{s_idx} — audio_dur={audio_dur:.2f}s prompt={prompt[:60]}")
    if raw_video is None:
        raise HTTPException(500, f"{model_name} video generation failed")

    # Save raw video to GCS (for debugging)
    raw_path = f"pipeline/{order_id}/scenes/{ch_idx}_{s_idx}/raw_video.mp4"
    bucket.blob(raw_path).upload_from_string(raw_video, content_type="video/mp4")

    # Overlay TTS audio onto the video
    mp4_bytes = assemble_scene_video_from_clip(raw_video, audio_bytes)
    if mp4_bytes is None:
        raise HTTPException(500, "Scene video assembly failed — FFmpeg may be unavailable")

    # Persist to GCS
    blob_path = f"pipeline/{order_id}/scenes/{ch_idx}_{s_idx}/{language}/scene_video.mp4"
    bucket.blob(blob_path).upload_from_string(mp4_bytes, content_type="video/mp4")

    video_b64 = base64.b64encode(mp4_bytes).decode("utf-8")
    return {
        "video_data_url": f"data:video/mp4;base64,{video_b64}",
        "gcs_path": f"gs://{settings.gcs_temp_bucket}/{blob_path}",
        "duration_sec": round(audio_dur, 2),
    }


@router.post("/orders/{order_id}/video-materials/scene/regenerate-prompt")
async def admin_scene_regenerate_prompt(
    order_id: str,
    body: dict,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """Regenerate the visual_prompt for a single scene via Gemini.

    Reads narration + character sheet from video_materials.json,
    calls Gemini using the shared LTX prompt rules from ots-common,
    and writes the new prompt back.
    """
    ch_idx = body.get("chapter_index")
    s_idx = body.get("scene_index")
    instruction = body.get("instruction", "")

    if ch_idx is None or s_idx is None:
        raise HTTPException(400, "chapter_index and scene_index are required")

    from core.storage import get_storage_client
    client = get_storage_client()
    bucket = client.bucket(settings.gcs_temp_bucket)

    blob = bucket.blob(f"pipeline/{order_id}/video_materials.json")
    if not blob.exists():
        raise HTTPException(404, "video_materials.json not found")

    materials = json.loads(blob.download_as_text(encoding="utf-8"))
    chapters = materials.get("chapters", [])
    chapter = next((ch for ch in chapters if ch["chapter_index"] == ch_idx), None)
    if not chapter:
        raise HTTPException(404, f"Chapter {ch_idx} not found")
    scene = next((s for s in chapter.get("scenes", []) if s["scene_index"] == s_idx), None)
    if not scene:
        raise HTTPException(404, f"Scene {ch_idx}.{s_idx} not found")

    zh_text = scene.get("tracks", {}).get("zh", {}).get("narration_text", "") or scene.get("narration_text", "")
    current_prompt = scene.get("visual_prompt", "")
    if not zh_text:
        raise HTTPException(400, "Scene has no narration text to base a prompt on")

    character_sheet = materials.get("global_style", {"characters": {}, "environment": ""})
    sheet_text = json.dumps(character_sheet, ensure_ascii=False, indent=2)

    # Import shared LTX rules
    from ots_common.video.ltx_prompt_rules import LTX_VISUAL_PROMPT_RULES

    if instruction:
        instruction_block = f"\nUser instruction (apply this over everything else): {instruction}"
    else:
        instruction_block = ""

    regen_prompt = f"""You are a professional video director. Given a scene's narration text, character sheet, and the previous prompt, generate a better LTX-optimized visual prompt.

{LTX_VISUAL_PROMPT_RULES}

Narration: {zh_text}

Character sheet: {sheet_text}

Previous prompt (make it better): {current_prompt}{instruction_block}

Output ONLY the new visual_prompt text. No JSON, no commentary."""
    import google.genai as genai
    client_genai = genai.Client(api_key=settings.gemini_api_key)
    response = client_genai.models.generate_content(
        model="gemini-2.5-flash",
        contents=regen_prompt,
        config={
            "max_output_tokens": 16384,
            "temperature": 0.3,
        }
    )
    new_prompt = response.text.strip() if response.text else ""
    if not new_prompt:
        raise HTTPException(500, "Gemini returned empty prompt")

    # Update video_materials.json
    scene["visual_prompt"] = new_prompt
    blob.upload_from_string(
        json.dumps(materials, ensure_ascii=False, indent=2),
        content_type="application/json",
    )

    return {"visual_prompt": new_prompt}


@router.post("/orders/{order_id}/video-materials/chapter/merge")
async def admin_chapter_merge(
    order_id: str,
    body: dict,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """Merge all scene videos in a chapter into one MP4 with a title card.

    Downloads each scene's `scene_video.mp4` from GCS and concatenates them.
    Falls back to image+audio loop for scenes without video.
    The `language` param selects which narration track to use. Defaults to 'zh'.
    """
    ch_idx = body.get("chapter_index")
    language = body.get("language", "zh")
    if ch_idx is None:
        raise HTTPException(400, "chapter_index is required")

    from core.storage import get_storage_client

    client = get_storage_client()
    temp_bucket = client.bucket(settings.gcs_temp_bucket)
    blob = temp_bucket.blob(f"pipeline/{order_id}/video_materials.json")
    if not blob.exists():
        raise HTTPException(400, "video_materials.json not found")
    materials = json.loads(blob.download_as_text(encoding="utf-8"))
    chapters = materials.get("chapters", [])
    chapter = next((ch for ch in chapters if ch["chapter_index"] == ch_idx), None)
    if not chapter:
        raise HTTPException(404, f"Chapter {ch_idx} not found")

    from services.video_gen_service import merge_chapter_videos
    mp4_bytes, srt_content = merge_chapter_videos(
        order_id=order_id,
        chapter_index=ch_idx,
        scenes=chapter.get("scenes", []),
        language=language,
        title=chapter.get("title", ""),
    )
    if mp4_bytes is None:
        raise HTTPException(500, "Chapter merge failed — generate scene videos or TTS+images first")

    out_bucket = client.bucket(settings.gcs_outputs_bucket)
    blob_path = f"orders/{order_id}/chapter_{ch_idx:02d}_{language}.mp4"
    out_bucket.blob(blob_path).upload_from_string(mp4_bytes, content_type="video/mp4")

    srt_path = f"orders/{order_id}/chapter_{ch_idx:02d}_{language}.srt"
    if srt_content:
        out_bucket.blob(srt_path).upload_from_string(srt_content, content_type="text/plain; charset=utf-8")

    from core.storage import generate_signed_url
    video_url = generate_signed_url(settings.gcs_outputs_bucket, blob_path)
    srt_url = generate_signed_url(settings.gcs_outputs_bucket, srt_path) if srt_content else None

    return {"video_url": video_url, "srt_url": srt_url, "gcs_path": f"gs://{settings.gcs_outputs_bucket}/{blob_path}"}


@router.post("/orders/{order_id}/video-materials/clean")
async def admin_clean_video_assets(
    order_id: str,
    body: dict,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """Clean all generated video assets for an order, with optional backup.

    Request body:
      backup: bool          — copy assets to backup_{timestamp}/ before deleting (default true)
      remove_materials: bool — also delete video_materials.json (default false)
      language: str         — if provided, only clean assets for this track (e.g. 'zh' or 'tai-lo')

    Deletes:
      - pipeline/{order_id}/video_materials.json (if remove_materials=true)
      - pipeline/{order_id}/scenes/** (narration.wav per scene, within language if specified)
      - pipeline/{order_id}/scenes/**/visual.jpg (shared — only when no language filter)
      - orders/{order_id}/chapter_*.mp4 (within language if specified)
      - orders/{order_id}/chapter_*.srt (within language if specified)
    """
    do_backup = body.get("backup", True)
    remove_materials = body.get("remove_materials", False)
    language = body.get("language", "")

    from core.storage import get_storage_client
    from datetime import datetime

    client = get_storage_client()
    temp_bucket = client.bucket(settings.gcs_temp_bucket)
    out_bucket = client.bucket(settings.gcs_outputs_bucket)

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backup_prefix = f"pipeline/{order_id}/backup_{timestamp}/"

    deleted = {"audio": 0, "image": 0, "video": 0, "srt": 0, "materials": 0, "backup": 0}

    def _blobs_to_delete(bucket, prefix: str) -> list:
        return list(bucket.list_blobs(prefix=prefix))

    # ── video_materials.json ──
    materials_path = f"pipeline/{order_id}/video_materials.json"
    materials_blob = temp_bucket.blob(materials_path)
    if remove_materials and materials_blob.exists():
        if do_backup:
            new_name = backup_prefix + f"{order_id}/video_materials.json"
            temp_bucket.copy_blob(materials_blob, temp_bucket, new_name)
            deleted["backup"] += 1
        materials_blob.delete()
        deleted["materials"] = 1

    # ── Scene assets (temp bucket) ──
    scenes_prefix = f"pipeline/{order_id}/scenes/"
    scene_blobs = _blobs_to_delete(temp_bucket, scenes_prefix)

    if language:
        # Filter to specific language track
        scene_blobs = [b for b in scene_blobs if f"/{language}/narration.wav" in b.name]

    for blob in scene_blobs:
        if do_backup:
            new_name = backup_prefix + blob.name[len(f"pipeline/{order_id}/"):]
            temp_bucket.copy_blob(blob, temp_bucket, new_name)
            deleted["backup"] += 1
        blob.delete()
        if blob.name.endswith("narration.wav"):
            deleted["audio"] += 1
        elif blob.name.endswith("visual.jpg"):
            deleted["image"] += 1

    # ── Chapter videos and SRTs (outputs bucket) ──
    out_prefix = f"orders/{order_id}/chapter_"
    out_blobs = _blobs_to_delete(out_bucket, out_prefix)

    if language:
        out_blobs = [b for b in out_blobs if f"_{language}." in b.name]

    for blob in out_blobs:
        if do_backup:
            new_name = backup_prefix + blob.name[len(f"orders/{order_id}/"):]
            out_bucket.copy_blob(blob, out_bucket, new_name)
            deleted["backup"] += 1
        blob.delete()
        if blob.name.endswith(".mp4"):
            deleted["video"] += 1
        elif blob.name.endswith(".srt"):
            deleted["srt"] += 1

    logger.info(
        f"Clean video assets: order={order_id} lang={language or 'all'} "
        f"audio={deleted['audio']} image={deleted['image']} "
        f"video={deleted['video']} srt={deleted['srt']} backup={deleted['backup']}"
    )
    return {
        "message": "Video assets cleaned",
        "backup_taken": do_backup,
        "remove_materials": remove_materials,
        "backup_prefix": backup_prefix if do_backup else None,
        "deleted": deleted,
    }


@router.put("/orders/{order_id}/video-materials/chapter/srt")
async def admin_save_chapter_srt(
    order_id: str,
    body: dict,
    admin: dict        = Depends(get_admin_user),
    db:   AsyncSession = Depends(get_db),
):
    """Save edited SRT content for a chapter (language-aware path)."""
    ch_idx = body.get("chapter_index")
    language = body.get("language", "zh")
    srt_content = body.get("srt_content")
    if ch_idx is None or srt_content is None:
        raise HTTPException(400, "chapter_index and srt_content are required")

    from core.storage import get_storage_client, generate_signed_url
    client = get_storage_client()
    out_bucket = client.bucket(settings.gcs_outputs_bucket)
    srt_path = f"orders/{order_id}/chapter_{ch_idx:02d}_{language}.srt"
    out_bucket.blob(srt_path).upload_from_string(srt_content, content_type="text/plain; charset=utf-8")
    srt_url = generate_signed_url(settings.gcs_outputs_bucket, srt_path)
    return {"srt_url": srt_url}
