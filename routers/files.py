"""
routers/files.py

檔案上傳 / 下載 Signed URL 端點。
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
import logging

from core.database import get_db
from core.storage import generate_upload_signed_url, generate_download_signed_url
from routers.auth import get_current_user
from models.schemas import (
    UploadUrlRequest, UploadUrlResponse, DownloadUrlResponse
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/files", tags=["files"])

ALLOWED_CONTENT_TYPES = {
    "text/plain",
    "text/html",
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


# ── POST /files/upload-url ────────────────────────────────────────────────────
@router.post("/upload-url", response_model=UploadUrlResponse)
async def get_upload_url(
    body: UploadUrlRequest,
    user: dict         = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    產生 GCS 上傳用 Signed URL（PUT method，有效 30 分鐘）。
    前端取得 URL 後，直接 PUT 到 GCS，不需要經過後端。
    上傳完成後呼叫 POST /files/{order_id}/confirm 通知後端。
    """
    if body.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported content type: {body.content_type}"
        )

    # 確認訂單屬於當前用戶
    result = await db.execute(text("""
        SELECT o.id, o.status FROM orders o
        JOIN users u ON u.id = o.user_id
        WHERE o.id = :order_id AND u.uid_firebase = :uid
    """), {"order_id": body.order_id, "uid": user["uid"]})

    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    if row.status not in ("pending_payment", "paid"):
        raise HTTPException(
            status_code=400,
            detail="Can only upload files for pending_payment or paid orders"
        )

    signed_url, gcs_path = generate_upload_signed_url(
        order_id     = body.order_id,
        filename     = body.filename,
        content_type = body.content_type,
    )

    logger.info(f"Upload URL generated: order={body.order_id}, path={gcs_path}")

    return UploadUrlResponse(
        signed_url = signed_url,
        gcs_path   = gcs_path,
        expires_in = 1800,
    )


# ── POST /files/{order_id}/confirm ────────────────────────────────────────────
@router.post("/{order_id}/confirm")
async def confirm_upload(
    order_id: str,
    gcs_path: str,
    user: dict         = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    前端完成 GCS 上傳後，通知後端記錄 gcs_upload_path。
    """
    result = await db.execute(text("""
        SELECT o.id FROM orders o
        JOIN users u ON u.id = o.user_id
        WHERE o.id = :order_id AND u.uid_firebase = :uid
    """), {"order_id": order_id, "uid": user["uid"]})

    if not result.fetchone():
        raise HTTPException(status_code=404, detail="Order not found")

    await db.execute(text("""
        UPDATE orders SET gcs_upload_path = :gcs_path WHERE id = :order_id
    """), {"gcs_path": gcs_path, "order_id": order_id})
    await db.commit()

    logger.info(f"Upload confirmed: order={order_id}, path={gcs_path}")
    return {"message": "Upload confirmed", "gcs_path": gcs_path}


# ── GET /files/{order_id}/download-url ───────────────────────────────────────
@router.get("/{order_id}/download-url", response_model=DownloadUrlResponse)
async def get_download_url(
    order_id: str,
    user: dict         = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    產生交付譯文的 GCS 下載 Signed URL（GET method，有效 1 小時）。
    只有 delivered 狀態的訂單才能下載。
    """
    result = await db.execute(text("""
        SELECT o.status, o.gcs_output_path FROM orders o
        JOIN users u ON u.id = o.user_id
        WHERE o.id = :order_id AND u.uid_firebase = :uid
    """), {"order_id": order_id, "uid": user["uid"]})

    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    if row.status != "delivered":
        raise HTTPException(
            status_code=400,
            detail="Translation not yet delivered"
        )
    if not row.gcs_output_path:
        raise HTTPException(status_code=404, detail="Output file not found")

    signed_url = generate_download_signed_url(row.gcs_output_path)

    return DownloadUrlResponse(signed_url=signed_url, expires_in=3600)
