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
from routers.auth import get_editor_user
from models.schemas import (
    OrderDetail, OrderListResponse,
    QASegment, QASegmentListResponse, QASegmentsBatchUpdate,
    MessageResponse, QAFlagResponse
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/editor", tags=["editor"])


@router.get("/orders", response_model=OrderListResponse)
async def list_assigned_orders(
    editor: dict       = Depends(get_editor_user),
    db:     AsyncSession = Depends(get_db),
):
    """列出指派給當前 Editor 的待審閱訂單"""
    result = await db.execute(text("""
        SELECT
            o.id, o.track_type, o.status, o.source_lang, o.target_lang,
            o.word_count, o.price_ntd, o.title, o.notes,
            o.created_at, o.deadline_at, o.delivered_at,
            o.gcs_output_path, o.editor_id,
            p.payment_status, p.invoice_no
        FROM orders o
        LEFT JOIN payments p ON p.order_id = o.id
        WHERE o.editor_id = :editor_id AND o.status = 'editor_verify'
        ORDER BY o.created_at DESC
    """), {"editor_id": editor["user_id"]})

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
            o.gcs_output_path, o.editor_id,
            p.payment_status, p.invoice_no
        FROM orders o
        LEFT JOIN payments p ON p.order_id = o.id
        WHERE o.id = :id AND (o.editor_id = :editor_id OR :is_admin = true)
    """), {
        "id":        order_id,
        "editor_id": editor["user_id"],
        "is_admin":  editor.get("is_admin", False)
    })

    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found or access denied")

    return OrderDetail(**dict(row._mapping))


@router.get("/orders/{order_id}/segments", response_model=QASegmentListResponse)
async def get_assigned_order_segments(
    order_id: str,
    editor:   dict       = Depends(get_editor_user),
    db:       AsyncSession = Depends(get_db),
):
    """獲取指派訂單的段落資料"""
    # 1. 驗證權限：訂單必須指派給該 Editor 且狀態為 editor_verify
    res = await db.execute(text("""
        SELECT id FROM orders 
        WHERE id = :id AND editor_id = :editor_id AND status = 'editor_verify'
    """), {"id": order_id, "editor_id": editor["user_id"]})
    if not res.fetchone():
        raise HTTPException(status_code=403, detail="Access denied or order not in editor_verify status")

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
    editor:   dict       = Depends(get_editor_user),
    db:       AsyncSession = Depends(get_db),
):
    """Editor 儲存草稿或更新譯文"""
    # 驗證權限
    res = await db.execute(text("""
        SELECT id FROM orders 
        WHERE id = :id AND editor_id = :editor_id AND status = 'editor_verify'
    """), {"id": order_id, "editor_id": editor["user_id"]})
    if not res.fetchone():
        raise HTTPException(status_code=403, detail="Access denied or order not in editor_verify status")

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
async def submit_editor_verify(
    order_id: str,
    editor:   dict       = Depends(get_editor_user),
    db:       AsyncSession = Depends(get_db),
):
    """Editor 完成審閱，將訂單狀態改為 delivered"""
    res = await db.execute(text("""
        SELECT id FROM orders 
        WHERE id = :id AND editor_id = :editor_id AND status = 'editor_verify'
    """), {"id": order_id, "editor_id": editor["user_id"]})
    if not res.fetchone():
        raise HTTPException(status_code=403, detail="Access denied or order not in editor_verify status")

    await db.execute(text("""
        UPDATE orders SET status = 'delivered', delivered_at = NOW() WHERE id = :id
    """), {"id": order_id})
    await db.commit()
    return MessageResponse(message="Verification completed, order delivered")


@router.post("/orders/{order_id}/return", response_model=MessageResponse)
async def return_to_qa(
    order_id: str,
    editor:   dict       = Depends(get_editor_user),
    db:       AsyncSession = Depends(get_db),
):
    """Editor 將訂單退回 qa_review 狀態"""
    res = await db.execute(text("""
        SELECT id FROM orders 
        WHERE id = :id AND editor_id = :editor_id AND status = 'editor_verify'
    """), {"id": order_id, "editor_id": editor["user_id"]})
    if not res.fetchone():
        raise HTTPException(status_code=403, detail="Access denied or order not in editor_verify status")

    await db.execute(text("""
        UPDATE orders SET status = 'qa_review' WHERE id = :id
    """), {"id": order_id})
    await db.commit()
    return MessageResponse(message="Order returned to qa_review")
