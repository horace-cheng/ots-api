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
from core import storage
from core.storage import generate_download_signed_url
from routers.auth import get_admin_user
from models.schemas import (
    QAFlagResponse, QAFlagListResponse, QAFlagResolve,
    AssignmentUpdate, AssignmentResponse, AssignmentListResponse,
    PaymentConfirm, MessageResponse,
    OrderDetail, AdminOrderDetail, OrderListResponse,
    DownloadUrlResponse,
    UserListItem, UserListResponse, UserUpdateRequest, UserLanguageUpdate, UserLanguage,
    QASegment, QASegmentListResponse, QASegmentsBatchUpdate,
    EditorAssignRequest,
)
from services.payment import (
    get_payment_gateway, InvoiceRequest, InvoiceType, InvoiceError
)
from services.pipeline import trigger_pipeline

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
            la.id, la.order_id, la.editor_id, la.proofreader_id,
            la.status, la.assigned_at,
            la.editor_submitted_at, la.proofread_submitted_at
        FROM literary_assignments la
        {where}
        ORDER BY la.assigned_at DESC
        LIMIT :limit OFFSET :offset
    """), params)

    rows = result.fetchall()
    assignments = [AssignmentResponse(**dict(r._mapping)) for r in rows]

    count_result = await db.execute(text(f"""
        SELECT COUNT(*) FROM literary_assignments la {where}
    """), {k: v for k, v in params.items() if k not in ("limit", "offset")})
    total = count_result.scalar()

    return AssignmentListResponse(assignments=assignments, total=total)


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
            o.word_count, o.price_ntd, o.title, o.notes,
            o.created_at, o.deadline_at, o.delivered_at,
            o.gcs_output_path, o.editor_id,
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
            o.created_at, o.deadline_at, o.delivered_at,
            o.gcs_output_path, o.editor_id, o.qa_id,
            p.payment_status, p.invoice_no,
            pj.qa_result
        FROM orders o
        LEFT JOIN payments p ON p.order_id = o.id
        LEFT JOIN pipeline_jobs pj ON pj.order_id = o.id AND pj.job_type = 'qa_auto'
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
        raise HTTPException(status_code=404, detail="Segments or translations not found")

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

    return QASegmentListResponse(segments=res_segments)


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
    """指派或更換 Editor"""
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

    await db.execute(text("""
        UPDATE orders SET editor_id = :editor_id, qa_id = :qa_id WHERE id = :id
    """), {"editor_id": editor_id, "qa_id": qa_id, "id": order_id})
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
        WHERE order_id = (SELECT id FROM pipeline_jobs WHERE order_id = :id AND job_type = 'qa_auto')
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
