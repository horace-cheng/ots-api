"""
routers/editor.py

Editor Dashboard 端點。
獲取指派訂單、編輯段落、儲存草稿、提交或退回 QA。
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
import logging

from core.database import get_db
from core import storage
from core.storage import read_blob
from routers.auth import get_editor_user, get_reviewer_user, get_lt_user
from models.schemas import (
    OrderDetail, OrderListResponse,
    QASegment, QASegmentListResponse, QASegmentsBatchUpdate,
    MessageResponse, QAFlagResponse, EditorAssignRequest,
    UserListResponse, UserListItem,
    AssignmentResponse, AssignmentListResponse,
    OriginalContentResponse,
    SupportFileResponse, SupportFileListResponse,
)
from services.document_converter import convert_document

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/editor", tags=["editor"])


@router.get("/orders", response_model=OrderListResponse)
async def list_assigned_orders(
    user:   dict       = Depends(get_reviewer_user),
    db:     AsyncSession = Depends(get_db),
    limit:  int = 10,
    offset: int = 0,
):
    """列出指派給當前使用者 (Editor 或 QA) 的待審閱訂單"""
    # 如果是 admin，看到所有待審閱
    # 如果是 editor，看到 editor_id = me
    # 如果是 qa，看到 qa_id = me
    conditions = []
    params = {"user_id": user["user_id"], "limit": limit, "offset": offset}

    if user.get("is_admin"):
        conditions.append("o.status IN ('qa_review', 'editor_verify')")
    else:
        role_conds = []
        if user.get("is_editor"):
            role_conds.append("a.editor_id = :user_id")
        if user.get("is_qa"):
            role_conds.append("a.qa_id = :user_id")
            # QA 只能看到 qa_review 狀態的訂單，不能看到 editor_verify
            conditions.append("o.status = 'qa_review'")
        
        if not role_conds:
             raise HTTPException(status_code=403, detail="No assigned roles found")
        
        conditions.append(f"({ ' OR '.join(role_conds) })")
        # Editor 可以看到 qa_review 和 editor_verify
        if user.get("is_editor"):
            conditions.append("o.status IN ('qa_review', 'editor_verify')")

    where = " AND ".join(conditions)

    # Get total count
    count_result = await db.execute(text(f"""
        SELECT COUNT(*) FROM orders o
        LEFT JOIN assignments a ON a.order_id = o.id
        WHERE {where}
    """), params)
    total = count_result.scalar() or 0

    result = await db.execute(text(f"""
        SELECT
            o.id, o.track_type, o.status, o.source_lang, o.target_lang,
            o.word_count, o.price_ntd, o.title, o.notes,
            o.created_at, o.deadline_at, o.delivered_at,
            o.gcs_output_path, o.gcs_upload_path, a.editor_id, a.qa_id,
            p.payment_status, p.invoice_no
        FROM orders o
        LEFT JOIN payments p ON p.order_id = o.id
        LEFT JOIN assignments a ON a.order_id = o.id
        WHERE {where}
        ORDER BY o.created_at DESC
        LIMIT :limit OFFSET :offset
    """), params)
    rows = result.fetchall()

    return OrderListResponse(
        orders=[OrderDetail(**dict(r._mapping)) for r in rows],
        total=total
    )


@router.get("/orders/{order_id}", response_model=OrderDetail)
async def get_editor_order(
    order_id: str,
    user:   dict       = Depends(get_reviewer_user),
    db:       AsyncSession = Depends(get_db),
):
    """取得指派給該 Editor/QA 的訂單詳情"""
    result = await db.execute(text("""
        SELECT
            o.id, o.track_type, o.status, o.source_lang, o.target_lang,
            o.word_count, o.price_ntd, o.title, o.notes,
            o.created_at, o.deadline_at, o.delivered_at,
            o.gcs_output_path, o.gcs_upload_path, a.editor_id, a.qa_id,
            p.payment_status, p.invoice_no
        FROM orders o
        LEFT JOIN payments p ON p.order_id = o.id
        LEFT JOIN assignments a ON a.order_id = o.id
        WHERE o.id = :id AND (a.editor_id = :user_id OR a.qa_id = :user_id OR :is_admin = true)
    """), {
        "id":        order_id,
        "user_id":   user["user_id"],
        "is_admin":  user.get("is_admin", False)
    })

    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found or access denied")

    return OrderDetail(**dict(row._mapping))


@router.patch("/orders/{order_id}/segments", response_model=MessageResponse)
async def update_editor_segments(
    order_id: str,
    body:     QASegmentsBatchUpdate = ...,
    user:     dict       = Depends(get_reviewer_user),
    db:       AsyncSession = Depends(get_db),
):
    """儲存 Editor/QA 的段落編輯 (FT)"""
    res = await db.execute(text("""
        SELECT 1 FROM orders o
        LEFT JOIN assignments a ON a.order_id = o.id
        WHERE o.id = :id AND (a.editor_id = :user_id OR a.qa_id = :user_id OR :is_admin = true)
    """), {"id": order_id, "user_id": user["user_id"], "is_admin": user.get("is_admin", False)})
    if not res.fetchone():
        raise HTTPException(status_code=403, detail="Access denied")

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


@router.post("/orders/{order_id}/submit", response_model=MessageResponse)
async def submit_editor_order(
    order_id: str,
    user:   dict       = Depends(get_reviewer_user),
    db:       AsyncSession = Depends(get_db),
):
    """提交訂單審閱結果 — Editor 送交 deliver，QA 送回 editor_verify"""
    res = await db.execute(text("""
        SELECT o.status FROM orders o
        LEFT JOIN assignments a ON a.order_id = o.id
        WHERE o.id = :id AND (a.editor_id = :user_id OR a.qa_id = :user_id OR :is_admin = true)
    """), {"id": order_id, "user_id": user["user_id"], "is_admin": user.get("is_admin", False)})
    order = res.fetchone()
    if not order:
        raise HTTPException(status_code=403, detail="Access denied")

    if user.get("is_editor"):
        new_status = "delivered"
    else:
        new_status = "editor_verify"

    await db.execute(text("""
        UPDATE orders SET status = :status, delivered_at = CASE WHEN :status = 'delivered' THEN NOW() ELSE delivered_at END
        WHERE id = :id
    """), {"id": order_id, "status": new_status})
    await db.commit()
    return MessageResponse(message=f"Order submitted, new status: {new_status}")


@router.post("/orders/{order_id}/return", response_model=MessageResponse)
async def return_order_to_qa(
    order_id: str,
    user:   dict       = Depends(get_editor_user),
    db:       AsyncSession = Depends(get_db),
):
    """退回訂單給 QA re-review（僅限 Editor）"""
    res = await db.execute(text("""
        SELECT 1 FROM orders o
        JOIN assignments a ON a.order_id = o.id
        WHERE o.id = :id AND a.editor_id = :user_id AND o.status = 'editor_verify'
    """), {"id": order_id, "user_id": user["user_id"]})
    if not res.fetchone():
        raise HTTPException(status_code=403, detail="Access denied")

    await db.execute(text("UPDATE orders SET status = 'qa_review' WHERE id = :id"), {"id": order_id})
    await db.commit()
    return MessageResponse(message="Order returned to qa_review")


@router.get("/team", response_model=UserListResponse)
async def list_team(
    user:   dict       = Depends(get_editor_user),
    db:       AsyncSession = Depends(get_db),
):
    """列出所有可指派的團隊成員（包含 Editor 與 QA）"""
    result = await db.execute(text("""
        SELECT id, uid_firebase, email, client_type, disabled, created_at,
               roles, languages
        FROM users WHERE disabled = false ORDER BY email ASC
    """))
    rows = result.fetchall()
    users = []
    for r in rows:
        d = dict(r._mapping)
        roles_set = set(d.pop("roles", []) or [])
        d["is_admin"] = "admin" in roles_set
        d["is_editor"] = "editor" in roles_set
        d["is_qa"] = "qa" in roles_set
        d["admin_role"] = "admin" if "admin" in roles_set else None
        users.append(UserListItem(**d))
    return UserListResponse(users=users, total=len(users))


@router.patch("/orders/{order_id}/assign-qa", response_model=MessageResponse)
async def assign_qa_to_order(
    order_id: str,
    body:     EditorAssignRequest = ...,
    user:   dict         = Depends(get_editor_user),
    db:       AsyncSession = Depends(get_db),
):
    """Editor 指派 QA 到訂單"""
    res = await db.execute(text("""
        SELECT 1 FROM orders o
        JOIN assignments a ON a.order_id = o.id
        WHERE o.id = :id AND a.editor_id = :user_id
    """), {"id": order_id, "user_id": user["user_id"]})
    if not res.fetchone():
        raise HTTPException(status_code=403, detail="Access denied")

    qa_res = await db.execute(text("""
        SELECT 1 FROM users WHERE id = :qa_id AND :qa_id = ANY(roles)
    """), {"qa_id": body.qa_id})
    if not qa_res.fetchone():
        raise HTTPException(status_code=400, detail="Specified user is not a QA")

    await db.execute(text("""
        UPDATE assignments SET qa_id = :qa_id WHERE order_id = :order_id
    """), {"order_id": order_id, "qa_id": body.qa_id})
    await db.commit()
    return MessageResponse(message="QA assigned to order")


@router.get("/orders/{order_id}/segments", response_model=QASegmentListResponse)
async def get_assigned_order_segments(
    order_id: str,
    user:     dict       = Depends(get_reviewer_user),
    db:       AsyncSession = Depends(get_db),
):
    """獲取指派訂單的段落資料 (Editor 或 QA 呼叫)"""
    # 1. 驗證權限：訂單必須指派給該使用者
    res = await db.execute(text("""
        SELECT o.id, a.editor_id, a.qa_id, o.status FROM orders o
        LEFT JOIN assignments a ON a.order_id = o.id
        WHERE o.id = :id AND (a.editor_id = :user_id OR a.qa_id = :user_id OR :is_admin = true)
    """), {"id": order_id, "user_id": user["user_id"], "is_admin": user.get("is_admin", False)})
    order = res.fetchone()
    if not order:
        raise HTTPException(status_code=403, detail="Access denied")

    # 2. 從 GCS 讀取資料 (與 Admin 邏輯相同)
    segments_raw = storage.read_temp_json(order_id, "segments.json")
    translations = storage.read_temp_json(order_id, "translations.json")
    trans_raw    = storage.read_temp_json(order_id, "translations_raw.json")

    if not segments_raw or not translations:
        raise HTTPException(status_code=404, detail="Segments or translations not found")

    # 3. Load QA flags from DB
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

    flags_map: dict[int, list] = {}
    for r in flags_rows:
        idx = r.paragraph_index
        if idx not in flags_map:
            flags_map[idx] = []
        flags_map[idx].append(QAFlagResponse(**dict(r._mapping)))

    # 4. Build response
    raw_map = {t["index"]: t["translated"] for t in trans_raw} if isinstance(trans_raw, list) else {}
    trans_map = {t["index"]: t for t in translations} if isinstance(translations, list) else {}

    res_segments = []
    for s in segments_raw:
        idx = s["index"]
        t = trans_map.get(idx, {})
        res_segments.append(QASegment(
            index      = idx,
            source     = s["text"],
            translated = t.get("translated", ""),
            raw        = raw_map.get(idx),
            comments   = t.get("comments"),
            editor_comments = t.get("editor_comments"),
            proofreader_comments = t.get("proofreader_comments"),
            flags      = flags_map.get(idx, []),
        ))

    return QASegmentListResponse(segments=res_segments)


# ── Literary Track: Assignments ───────────────────────────────────────────────
@router.get("/lt/assignments", response_model=AssignmentListResponse)
async def list_lt_assignments(
    limit:  int        = Query(50, ge=1, le=200),
    offset: int        = Query(0, ge=0),
    user:   dict       = Depends(get_lt_user),
    db:     AsyncSession = Depends(get_db),
):
    """列出當前使用者的 Literary Track 指派（editor / proofreader / qa）
    只顯示各角色進行中的任務，隱藏已完成狀態：
      - editor: editing, revision_needed（隱藏 editor_done, proofread_done）
      - proofreader: proofreading（隱藏 proofread_done）
    """
    params: dict = {"user_id": user["user_id"], "limit": limit, "offset": offset}

    result = await db.execute(text("""
        SELECT
            la.id, la.order_id, la.editor_id, la.qa_id, la.proofreader_id,
            la.status, la.assigned_at,
            la.editor_submitted_at, la.proofread_submitted_at, la.qa_submitted_at,
            la.editor_notes, la.proofreader_notes
        FROM assignments la
        WHERE (la.editor_id = :user_id AND la.status IN ('editing', 'revision_needed'))
           OR (la.proofreader_id = :user_id AND la.status = 'proofreading')
           OR (la.qa_id = :user_id)
        ORDER BY la.assigned_at DESC
        LIMIT :limit OFFSET :offset
    """), params)

    rows = result.fetchall()
    assignments = [AssignmentResponse(**dict(r._mapping)) for r in rows]

    count_result = await db.execute(text("""
        SELECT COUNT(*) FROM assignments la
        WHERE (la.editor_id = :user_id AND la.status IN ('editing', 'revision_needed'))
           OR (la.proofreader_id = :user_id AND la.status = 'proofreading')
           OR (la.qa_id = :user_id)
    """), {"user_id": user["user_id"]})
    total = count_result.scalar()

    return AssignmentListResponse(assignments=assignments, total=total)


@router.get("/lt/orders/{order_id}", response_model=OrderDetail)
async def get_lt_order(
    order_id: str,
    role:     str        = Query("editor"),
    user:     dict       = Depends(get_lt_user),
    db:       AsyncSession = Depends(get_db),
):
    """取得 Literary Track 訂單詳情（限指派給該使用者的訂單）"""
    if role == "proofreader":
        where_clause = "a.proofreader_id = :user_id OR :is_admin = true"
    else:
        where_clause = "a.editor_id = :user_id OR :is_admin = true"

    result = await db.execute(text(f"""
        SELECT
            o.id, o.track_type, o.status, o.source_lang, o.target_lang,
            o.word_count, o.price_ntd, o.quoted_price, o.reference_price,
            o.title, o.notes,
            o.created_at, o.deadline_at, o.delivered_at,
            o.gcs_output_path, o.gcs_upload_path,
            p.payment_status, p.invoice_no
        FROM orders o
        LEFT JOIN payments p ON p.order_id = o.id
        JOIN assignments a ON a.order_id = o.id
        WHERE o.id = :id AND ({where_clause})
    """), {
        "id":       order_id,
        "user_id":  user["user_id"],
        "is_admin": user.get("is_admin", False),
    })

    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found or access denied")

    return OrderDetail(**dict(row._mapping))


@router.get("/lt/orders/{order_id}/segments", response_model=QASegmentListResponse)
async def get_lt_order_segments(
    order_id: str,
    role:     str        = Query("editor"),
    user:     dict       = Depends(get_lt_user),
    db:       AsyncSession = Depends(get_db),
):
    """獲取 Literary Track 訂單的段落資料 (editor 或 proofreader 呼叫)"""
    if role == "proofreader":
        where_clause = "a.proofreader_id = :user_id OR :is_admin = true"
    else:
        where_clause = "a.editor_id = :user_id OR :is_admin = true"

    res = await db.execute(text(f"""
        SELECT o.id FROM orders o
        JOIN assignments a ON a.order_id = o.id
        WHERE o.id = :id AND ({where_clause})
    """), {"id": order_id, "user_id": user["user_id"], "is_admin": user.get("is_admin", False)})
    if not res.fetchone():
        raise HTTPException(status_code=403, detail="Access denied")

    segments_raw = storage.read_temp_json(order_id, "segments.json")
    translations = storage.read_temp_json(order_id, "translations.json")
    trans_raw    = storage.read_temp_json(order_id, "translations_raw.json")

    if not segments_raw or not translations:
        raise HTTPException(status_code=404, detail="Segments or translations not found")

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

    flags_map: dict[int, list] = {}
    for r in flags_rows:
        idx = r.paragraph_index
        if idx not in flags_map:
            flags_map[idx] = []
        flags_map[idx].append(QAFlagResponse(**dict(r._mapping)))

    raw_map = {t["index"]: t["translated"] for t in trans_raw} if isinstance(trans_raw, list) else {}
    trans_map = {t["index"]: t for t in translations} if isinstance(translations, list) else {}

    res_segments = []
    for s in segments_raw:
        idx = s["index"]
        t = trans_map.get(idx, {})
        res_segments.append(QASegment(
            index      = idx,
            source     = s["text"],
            translated = t.get("translated", ""),
            raw        = raw_map.get(idx),
            comments   = t.get("comments"),
            editor_comments = t.get("editor_comments"),
            proofreader_comments = t.get("proofreader_comments"),
            flags      = flags_map.get(idx, []),
        ))

    return QASegmentListResponse(segments=res_segments)


@router.patch("/lt/orders/{order_id}/segments", response_model=MessageResponse)
async def update_lt_order_segments(
    order_id: str,
    role:     str                 = Query("editor"),
    body:     QASegmentsBatchUpdate = ...,
    user:     dict       = Depends(get_lt_user),
    db:       AsyncSession = Depends(get_db),
):
    """Save draft edits for a Literary Track order."""
    if role == "proofreader":
        where_clause = "a.proofreader_id = :user_id OR :is_admin = true"
    else:
        where_clause = "a.editor_id = :user_id OR :is_admin = true"

    # 1. Verify assignment
    res = await db.execute(text(f"""
        SELECT a.status FROM assignments a
        JOIN orders o ON o.id = a.order_id
        WHERE o.id = :id AND o.track_type = 'literary'
          AND ({where_clause})
    """), {"id": order_id, "user_id": user["user_id"], "is_admin": user.get("is_admin", False)})
    if not res.fetchone():
        raise HTTPException(status_code=403, detail="Access denied")

    translations = storage.read_temp_json(order_id, "translations.json")
    if not translations:
        raise HTTPException(status_code=404, detail="Translations not found")

    must_fix_indices = set()
    flags_res = await db.execute(text("""
        SELECT DISTINCT qf.paragraph_index FROM qa_flags qf
        JOIN pipeline_jobs pj ON pj.id = qf.job_id
        WHERE pj.order_id = :order_id
          AND qf.flag_level = 'must_fix'
          AND qf.resolved = false
    """), {"order_id": order_id})
    for row in flags_res.fetchall():
        must_fix_indices.add(row[0])

    trans_map = {t["index"]: t for t in translations}
    for up in body.segments:
        if up.index in trans_map:
            if role == "editor" and up.index in must_fix_indices and not (up.editor_comments or "").strip():
                raise HTTPException(
                    status_code=400,
                    detail=f"Segment {up.index + 1}: comments are required for flagged segments"
                )
            trans_map[up.index]["translated"] = up.translated
            if up.comments is not None:
                trans_map[up.index]["comments"] = up.comments
            if up.editor_comments is not None:
                trans_map[up.index]["editor_comments"] = up.editor_comments
            if up.proofreader_comments is not None:
                trans_map[up.index]["proofreader_comments"] = up.proofreader_comments

    storage.write_temp_json(order_id, "translations.json", list(trans_map.values()))
    return MessageResponse(message="Segments updated")


@router.post("/lt/orders/{order_id}/complete", response_model=MessageResponse)
async def complete_lt_assignment(
    order_id: str,
    role:     str        = Query("editor"),
    user:     dict       = Depends(get_lt_user),
    db:       AsyncSession = Depends(get_db),
):
    """
    Mark Literary Track work as complete.
    - Editor from editing → editor_done (first completion)
    - Editor from revision_needed → proofreading (back to proofreader for re-review)
    - Proofreader from proofreading → proofread_done (final delivery)
    """
    if role == "proofreader":
        where_clause = "a.proofreader_id = :user_id OR :is_admin = true"
    else:
        where_clause = "a.editor_id = :user_id OR :is_admin = true"

    res = await db.execute(text(f"""
        SELECT a.status FROM assignments a
        JOIN orders o ON o.id = a.order_id
        WHERE o.id = :id AND o.track_type = 'literary'
          AND ({where_clause})
    """), {"id": order_id, "user_id": user["user_id"], "is_admin": user.get("is_admin", False)})
    assignment = res.fetchone()
    if not assignment:
        raise HTTPException(status_code=403, detail="Access denied")

    if role == "editor":
        if assignment.status == "revision_needed":
            await db.execute(text("""
                UPDATE assignments
                SET status = 'proofreading'
                WHERE order_id = :id
            """), {"id": order_id})
            await db.commit()
            return MessageResponse(message="Revision submitted, sent back to proofreader for re-review")
        if assignment.status not in ("editing", "editor_done"):
            raise HTTPException(
                status_code=400,
                detail=f"Editor can only complete when status is 'editing' or 'revision_needed', got '{assignment.status}'"
            )
        if assignment.status == "editor_done":
            return MessageResponse(message="Assignment already completed")

        translations = storage.read_temp_json(order_id, "translations.json")
        if not translations:
            raise HTTPException(status_code=404, detail="Translations not found")

        trans_map = {t["index"]: t.get("editor_comments", "").strip() for t in translations}

        must_fix_flags = await db.execute(text("""
            SELECT qf.id, qf.paragraph_index FROM qa_flags qf
            JOIN pipeline_jobs pj ON pj.id = qf.job_id
            WHERE pj.order_id = :order_id
              AND qf.flag_level = 'must_fix'
              AND qf.resolved = false
        """), {"order_id": order_id})
        must_fix_rows = must_fix_flags.fetchall()

        unresolved_without_comment = []
        for row in must_fix_rows:
            comment = trans_map.get(row.paragraph_index, "")
            if comment:
                await db.execute(text("""
                    UPDATE qa_flags
                    SET resolved = true,
                        reviewer_note = COALESCE(reviewer_note, :comment),
                        resolved_at = NOW()
                    WHERE id = :flag_id
                """), {"flag_id": row.id, "comment": comment})
            else:
                unresolved_without_comment.append(row.paragraph_index)

        if unresolved_without_comment:
            seg_nums = ", ".join(str(i + 1) for i in sorted(unresolved_without_comment))
            raise HTTPException(
                status_code=400,
                detail=f"Segments {seg_nums} have QA flags but no comments. Please add comments before completing."
            )

        await db.execute(text("""
            UPDATE assignments
            SET status = 'editor_done',
                editor_submitted_at = NOW(),
                editor_completed_at = NOW()
            WHERE order_id = :id
        """), {"id": order_id})
    else:
        if assignment.status != "proofreading":
            raise HTTPException(
                status_code=400,
                detail=f"Proofreader can only complete when status is 'proofreading', got '{assignment.status}'"
            )
        await db.execute(text("""
            UPDATE assignments
            SET status = 'proofread_done',
                proofread_submitted_at = NOW(),
                proofreader_completed_at = NOW()
            WHERE order_id = :id
        """), {"id": order_id})

    await db.commit()
    return MessageResponse(message="Assignment completed")


class RejectRequest(BaseModel):
    notes: str = Field(..., min_length=1, max_length=2000, description="Rejection notes")


@router.post("/lt/orders/{order_id}/reject", response_model=MessageResponse)
async def reject_lt_assignment(
    order_id: str,
    role:     str        = Query("proofreader"),
    body:     RejectRequest = ...,
    user:     dict       = Depends(get_lt_user),
    db:       AsyncSession = Depends(get_db),
):
    """
    Proofreader rejects work and sends back to editor for revision.
    - Only allowed when assignment.status == 'proofreading'
    - Updates status to 'revision_needed' with proofreader_notes
    """
    if role == "proofreader":
        where_clause = "a.proofreader_id = :user_id OR :is_admin = true"
    else:
        where_clause = "a.editor_id = :user_id OR :is_admin = true"

    res = await db.execute(text(f"""
        SELECT a.status FROM assignments a
        JOIN orders o ON o.id = a.order_id
        WHERE o.id = :id AND o.track_type = 'literary'
          AND ({where_clause})
    """), {"id": order_id, "user_id": user["user_id"], "is_admin": user.get("is_admin", False)})
    assignment = res.fetchone()
    if not assignment:
        raise HTTPException(status_code=403, detail="Access denied")

    if assignment.status != "proofreading":
        raise HTTPException(
            status_code=400,
            detail=f"Can only reject when status is 'proofreading', got '{assignment.status}'"
        )

    await db.execute(text("""
        UPDATE assignments
        SET status = 'revision_needed',
            proofreader_notes = :notes
        WHERE order_id = :id
    """), {"id": order_id, "notes": body.notes})

    await db.commit()
    return MessageResponse(message="Assignment rejected, sent back for revision")


# ── Editor: 取得原始檔案內容 (FT) ────────────────────────────────────────────
@router.get("/orders/{order_id}/original-content", response_model=OriginalContentResponse)
async def editor_get_original_content(
    order_id: str,
    user: dict         = Depends(get_reviewer_user),
    db:   AsyncSession = Depends(get_db),
):
    """FT: 讀取原始檔案，轉換為 HTML。僅限被指派的 Editor 或 QA。"""
    result = await db.execute(text("""
        SELECT o.gcs_upload_path FROM orders o
        JOIN assignments a ON a.order_id = o.id
        WHERE o.id = :order_id
          AND (a.editor_id = :user_id OR a.qa_id = :user_id OR :is_admin = true)
    """), {"order_id": order_id, "user_id": user["user_id"], "is_admin": user.get("is_admin", False)})
    row = result.fetchone()
    if not row or not row.gcs_upload_path:
        raise HTTPException(status_code=404, detail="Original file not found or access denied")
    try:
        raw_bytes, filename = read_blob(row.gcs_upload_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Original file not found in storage")
    doc = convert_document(raw_bytes, filename)
    return OriginalContentResponse(filename=doc.filename, content_type=doc.content_type, html=doc.html)


# ── Editor: 取得原始檔案內容 (LT) ────────────────────────────────────────────
@router.get("/lt/orders/{order_id}/original-content", response_model=OriginalContentResponse)
async def lt_get_original_content(
    order_id: str,
    user: dict         = Depends(get_lt_user),
    db:   AsyncSession = Depends(get_db),
):
    """LT: 讀取原始檔案，轉換為 HTML。僅限被指派的 Editor 或 Proofreader。"""
    result = await db.execute(text("""
        SELECT o.gcs_upload_path FROM orders o
        JOIN assignments a ON a.order_id = o.id
        WHERE o.id = :order_id AND o.track_type = 'literary'
          AND (a.editor_id = :user_id OR a.proofreader_id = :user_id OR :is_admin = true)
    """), {"order_id": order_id, "user_id": user["user_id"], "is_admin": user.get("is_admin", False)})
    row = result.fetchone()
    if not row or not row.gcs_upload_path:
        raise HTTPException(status_code=404, detail="Original file not found or access denied")
    try:
        raw_bytes, filename = read_blob(row.gcs_upload_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Original file not found in storage")
    doc = convert_document(raw_bytes, filename)
    return OriginalContentResponse(filename=doc.filename, content_type=doc.content_type, html=doc.html)


# ── Editor: 列出 LT 支援文件 ─────────────────────────────────────────────────
@router.get("/lt/orders/{order_id}/support-files", response_model=SupportFileListResponse)
async def lt_list_support_files(
    order_id: str,
    user: dict         = Depends(get_lt_user),
    db:   AsyncSession = Depends(get_db),
):
    """LT: 列出支援文件。僅限被指派的 Editor 或 Proofreader。"""
    result = await db.execute(text("""
        SELECT 1 FROM orders o
        JOIN assignments a ON a.order_id = o.id
        WHERE o.id = :order_id AND o.track_type = 'literary'
          AND (a.editor_id = :user_id OR a.proofreader_id = :user_id OR :is_admin = true)
    """), {"order_id": order_id, "user_id": user["user_id"], "is_admin": user.get("is_admin", False)})
    if not result.fetchone():
        raise HTTPException(status_code=403, detail="Access denied")

    rows = await db.execute(text("""
        SELECT sf.id, sf.order_id, sf.filename, sf.content_type,
               sf.file_size, sf.gcs_path, sf.file_role, sf.created_at
        FROM order_support_files sf
        WHERE sf.order_id = :order_id
        ORDER BY sf.created_at ASC
    """), {"order_id": order_id})
    files = [SupportFileResponse(**dict(r._mapping)) for r in rows.fetchall()]
    return SupportFileListResponse(files=files, total=len(files))


# ── Editor: 讀取 LT 支援檔案內容 ────────────────────────────────────────────
@router.get("/lt/orders/{order_id}/support-files/{file_id}/content", response_model=OriginalContentResponse)
async def lt_get_support_file_content(
    order_id: str,
    file_id: str,
    user: dict         = Depends(get_lt_user),
    db:   AsyncSession = Depends(get_db),
):
    """LT: 讀取特定支援檔案內容，轉換為 HTML。"""
    result = await db.execute(text("""
        SELECT 1 FROM orders o
        JOIN assignments a ON a.order_id = o.id
        WHERE o.id = :order_id AND o.track_type = 'literary'
          AND (a.editor_id = :user_id OR a.proofreader_id = :user_id OR :is_admin = true)
    """), {"order_id": order_id, "user_id": user["user_id"], "is_admin": user.get("is_admin", False)})
    if not result.fetchone():
        raise HTTPException(status_code=403, detail="Access denied")

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
