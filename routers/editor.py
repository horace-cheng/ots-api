"""
routers/editor.py

Editor Dashboard 端點。
獲取指派訂單、編輯段落、儲存草稿、提交或退回 QA。
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
import logging

from core.database import get_db
from core import storage
from routers.auth import get_editor_user, get_qa_user
from models.schemas import (
    OrderDetail, OrderListResponse,
    QASegment, QASegmentListResponse, QASegmentsBatchUpdate,
    MessageResponse, QAFlagResponse, EditorAssignRequest
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/editor", tags=["editor"])


@router.get("/orders", response_model=OrderListResponse)
async def list_assigned_orders(
    user:   dict       = Depends(get_qa_user),
    db:     AsyncSession = Depends(get_db),
):
    """列出指派給當前使用者 (Editor 或 QA) 的待審閱訂單"""
    # 如果是 admin，看到所有待審閱
    # 如果是 editor，看到 editor_id = me
    # 如果是 qa，看到 qa_id = me
    conditions = []
    params = {"user_id": user["user_id"]}

    if user.get("is_admin"):
        conditions.append("o.status IN ('qa_review', 'editor_verify')")
    else:
        role_conds = []
        if user.get("is_editor"):
            role_conds.append("o.editor_id = :user_id")
        if user.get("is_qa"):
            role_conds.append("o.qa_id = :user_id")
        
        if not role_conds:
             raise HTTPException(status_code=403, detail="No assigned roles found")
        
        conditions.append(f"({ ' OR '.join(role_conds) })")
        conditions.append("o.status IN ('qa_review', 'editor_verify')")

    where = " AND ".join(conditions)

    result = await db.execute(text(f"""
        SELECT
            o.id, o.track_type, o.status, o.source_lang, o.target_lang,
            o.word_count, o.price_ntd, o.title, o.notes,
            o.created_at, o.deadline_at, o.delivered_at,
            o.gcs_output_path, o.editor_id, o.qa_id,
            p.payment_status, p.invoice_no
        FROM orders o
        LEFT JOIN payments p ON p.order_id = o.id
        WHERE {where}
        ORDER BY o.created_at DESC
    """), params)

    rows = result.fetchall()
    orders = [OrderDetail(**dict(r._mapping)) for r in rows]
    return OrderListResponse(orders=orders, total=len(orders))


@router.get("/orders/{order_id}", response_model=OrderDetail)
async def get_editor_order(
    order_id: str,
    editor:   dict       = Depends(get_editor_user),
    db:       AsyncSession = Depends(get_db),
):
    """取得指派給該 Editor 的訂單詳情"""
    result = await db.execute(text("""
        SELECT
            o.id, o.track_type, o.status, o.source_lang, o.target_lang,
            o.word_count, o.price_ntd, o.title, o.notes,
            o.created_at, o.deadline_at, o.delivered_at,
            o.gcs_output_path, o.editor_id, o.qa_id,
            p.payment_status, p.invoice_no
        FROM orders o
        LEFT JOIN payments p ON p.order_id = o.id
        WHERE o.id = :id AND (o.editor_id = :user_id OR o.qa_id = :user_id OR :is_admin = true)
    """), {
        "id":        order_id,
        "user_id":   editor["user_id"],
        "is_admin":  editor.get("is_admin", False)
    })

    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found or access denied")

    return OrderDetail(**dict(row._mapping))


@router.get("/orders/{order_id}/segments", response_model=QASegmentListResponse)
async def get_assigned_order_segments(
    order_id: str,
    user:     dict       = Depends(get_qa_user),
    db:       AsyncSession = Depends(get_db),
):
    """獲取指派訂單的段落資料 (Editor 或 QA 呼叫)"""
    # 1. 驗證權限：訂單必須指派給該使用者
    res = await db.execute(text("""
        SELECT id, editor_id, qa_id, status FROM orders 
        WHERE id = :id AND (editor_id = :user_id OR qa_id = :user_id OR :is_admin = true)
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

    # 3. 從 DB 讀取 QA Flags (唯讀參考)
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
            translated      = t.get("translated", ""),
            raw             = raw_map.get(idx),
            comments        = t.get("comments"),
            editor_comments = t.get("editor_comments"),
            flags           = flags_map.get(idx, []),
        ))

    return QASegmentListResponse(segments=res_segments)


@router.patch("/orders/{order_id}/segments", response_model=MessageResponse)
async def update_assigned_order_segments(
    order_id: str,
    body:     QASegmentsBatchUpdate,
    user:     dict       = Depends(get_qa_user),
    db:       AsyncSession = Depends(get_db),
):
    """Editor 或 QA 儲存草稿"""
    # 驗證權限
    res = await db.execute(text("""
        SELECT id FROM orders 
        WHERE id = :id AND (editor_id = :user_id OR qa_id = :user_id OR :is_admin = true)
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
    return MessageResponse(message="Segments updated by editor")


@router.post("/orders/{order_id}/submit", response_model=MessageResponse)
async def submit_review(
    order_id: str,
    user:     dict       = Depends(get_qa_user),
    db:       AsyncSession = Depends(get_db),
):
    """完成審閱。QA 提交後變為 editor_verify，Editor 提交後變為 delivered"""
    res = await db.execute(text("""
        SELECT id, status, editor_id, qa_id FROM orders 
        WHERE id = :id AND (editor_id = :user_id OR qa_id = :user_id OR :is_admin = true)
    """), {"id": order_id, "user_id": user["user_id"], "is_admin": user.get("is_admin", False)})
    order = res.fetchone()
    if not order:
        raise HTTPException(status_code=403, detail="Access denied")

    # 判斷角色與狀態
    is_qa_only = user.get("is_qa") and not user.get("is_editor") and not user.get("is_admin")
    
    if is_qa_only and order.status == 'qa_review':
        new_status = 'editor_verify'
        await db.execute(text("""
            UPDATE orders SET status = :status, qa_submitted_at = NOW() WHERE id = :id
        """), {"status": new_status, "id": order_id})
    else:
        new_status = 'delivered'
        await db.execute(text("""
            UPDATE orders SET status = :status, delivered_at = NOW() WHERE id = :id
        """), {"status": new_status, "id": order_id})
    
    await db.commit()
    return MessageResponse(message=f"Review submitted. New status: {new_status}")


@router.post("/orders/{order_id}/return", response_model=MessageResponse)
async def return_to_qa(
    order_id: str,
    editor:   dict       = Depends(get_editor_user),
    db:       AsyncSession = Depends(get_db),
):
    """Editor 將訂單退回 qa_review 狀態"""
    res = await db.execute(text("""
        SELECT id FROM orders 
        WHERE id = :id AND (editor_id = :editor_id OR :is_admin = true) AND status = 'editor_verify'
    """), {"id": order_id, "editor_id": editor["user_id"], "is_admin": editor.get("is_admin", False)})
    if not res.fetchone():
        raise HTTPException(status_code=403, detail="Access denied or order not in editor_verify status")

    await db.execute(text("""
        UPDATE orders SET status = 'qa_review' WHERE id = :id
    """), {"id": order_id})
    await db.commit()
    return MessageResponse(message="Order returned to qa_review")


@router.patch("/orders/{order_id}/assign-qa", response_model=MessageResponse)
async def assign_qa_to_order(
    order_id: str,
    body:     EditorAssignRequest,
    editor:   dict       = Depends(get_editor_user),
    db:       AsyncSession = Depends(get_db),
):
    """Editor 將其指派訂單再指派給 QA"""
    # 驗證該訂單是否指派給該 Editor
    res = await db.execute(text("""
        SELECT id FROM orders WHERE id = :id AND (editor_id = :user_id OR :is_admin = true)
    """), {"id": order_id, "user_id": editor["user_id"], "is_admin": editor.get("is_admin", False)})
    if not res.fetchone():
        raise HTTPException(status_code=403, detail="Access denied")

    qa_id = body.qa_id
    if qa_id:
        # 驗證 qa_id 是否有 QA 角色
        res = await db.execute(text("""
            SELECT user_id FROM user_roles WHERE user_id = :id AND role = 'qa'
        """), {"id": qa_id})
        if not res.fetchone():
            raise HTTPException(status_code=400, detail="User is not a QA or not found")

    await db.execute(text("""
        UPDATE orders SET qa_id = :qa_id WHERE id = :id
    """), {"qa_id": qa_id, "id": order_id})
    await db.commit()
    return MessageResponse(message="QA assigned by editor")
