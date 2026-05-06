"""
routers/files.py

檔案上傳 / 下載 Signed URL 端點。
"""

from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
import logging
import re

from core.database import get_db
from core.storage import generate_upload_signed_url, generate_download_signed_url, get_storage_client, _get_signing_credentials
from core.config import settings
from routers.auth import get_current_user
from models.schemas import (
    UploadUrlRequest, UploadUrlResponse, DownloadUrlResponse,
    SupportFileResponse, SupportFileListResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/files", tags=["files"])

_LANG_ZH = {
    "tai-lo":     "台語",
    "hakka":      "客語",
    "indigenous": "原住民族語",
    "zh-tw":      "繁體中文",
    "en":         "English",
    "ja":         "日本語",
    "ko":         "한국어",
}


LANG_ZH = {
    "tai-lo":     "台語",
    "hakka":      "客語",
    "indigenous": "原住民族語",
    "zh-tw":      "繁體中文",
    "en":         "English",
    "ja":         "日本語",
    "ko":         "한국어",
}

ALLOWED_CONTENT_TYPES = {
    "text/plain",
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def _extract_title(gcs_path: str) -> str | None:
    """Read the first 600 bytes of an uploaded file and return the opening words as a title."""
    try:
        filename = gcs_path.rsplit("/", 1)[-1].lower()
        ext = filename.rsplit(".", 1)[-1] if "." in filename else ""
        if ext != "txt":
            return None

        client = get_storage_client()
        blob   = client.bucket(settings.gcs_uploads_bucket).blob(gcs_path)
        data   = blob.download_as_bytes(start=0, end=600)
        text_  = data.decode("utf-8", errors="ignore")

        words = text_.split()
        if not words:
            return None
        return " ".join(words[:10])[:50]
    except Exception as e:
        logger.warning(f"Title extraction failed for {gcs_path}: {e}")
        return None


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
    if row.status not in ("pending_payment", "paid", "awaiting_quote"):
        raise HTTPException(
            status_code=400,
            detail="Can only upload files for pending_payment, awaiting_quote, or paid orders"
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
    若訂單標題尚未設定，從檔案內容擷取前幾個字作為標題。
    """
    result = await db.execute(text("""
        SELECT o.id, o.title, o.track_type, o.source_lang, o.target_lang
        FROM orders o
        JOIN users u ON u.id = o.user_id
        WHERE o.id = :order_id AND u.uid_firebase = :uid
    """), {"order_id": order_id, "uid": user["uid"]})

    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")

    await db.execute(text("""
        UPDATE orders SET gcs_upload_path = :gcs_path WHERE id = :order_id
    """), {"gcs_path": gcs_path, "order_id": order_id})

    # Generate title from file content if user didn't provide one
    title = row.title
    if not title:
        title = _extract_title(gcs_path)
        if not title:
            # Fallback for binary files (docx, pdf)
            src   = LANG_ZH.get(row.source_lang, row.source_lang)
            tgt   = LANG_ZH.get(row.target_lang, row.target_lang)
            track = "快速翻譯" if row.track_type == "fast" else "文學翻譯"
            title = f"{src} → {tgt} {track}"
        await db.execute(
            text("UPDATE orders SET title = :title WHERE id = :order_id"),
            {"title": title, "order_id": order_id}
        )

    await db.commit()

    logger.info(f"Upload confirmed: order={order_id}, path={gcs_path}, title={title!r}")
    return {"message": "Upload confirmed", "gcs_path": gcs_path, "title": title}


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


# ── Support Files (Literary Track) ───────────────────────────────────────────

SUPPORT_CONTENT_TYPES = {
    "text/plain",
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/csv",
}


@router.post("/{order_id}/support-upload-url", response_model=UploadUrlResponse)
async def get_support_upload_url(
    order_id: str,
    filename: str,
    content_type: str = "text/plain",
    user: dict         = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    Generate GCS upload Signed URL for Literary Track support files.
    Files go to orders/{order_id}/support/ prefix.
    """
    if content_type not in SUPPORT_CONTENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported content type: {content_type}"
        )

    # Verify order belongs to current user and is LT
    result = await db.execute(text("""
        SELECT o.id, o.status FROM orders o
        JOIN users u ON u.id = o.user_id
        WHERE o.id = :order_id AND u.uid_firebase = :uid
    """), {"order_id": order_id, "uid": user["uid"]})

    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")

    client   = get_storage_client()
    bucket   = client.bucket(settings.gcs_uploads_bucket)
    gcs_path = f"orders/{order_id}/support/{filename}"
    blob     = bucket.blob(gcs_path)

    signed_url = blob.generate_signed_url(
        version             = "v4",
        expiration          = timedelta(minutes=30),
        method              = "PUT",
        content_type        = content_type,
        credentials         = _get_signing_credentials(),
    )

    logger.info(f"Support upload URL generated: order={order_id}, path={gcs_path}")

    return UploadUrlResponse(
        signed_url = signed_url,
        gcs_path   = gcs_path,
        expires_in = 1800,
    )


@router.post("/{order_id}/support-confirm", response_model=SupportFileResponse)
async def confirm_support_upload(
    order_id: str,
    filename: str,
    content_type: str,
    file_size: int,
    gcs_path: str,
    file_role: str = "reference",
    user: dict         = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    Record a support file upload in the database.
    Called after frontend completes GCS upload.
    """
    # Verify order belongs to current user
    result = await db.execute(text("""
        SELECT o.id FROM orders o
        JOIN users u ON u.id = o.user_id
        WHERE o.id = :order_id AND u.uid_firebase = :uid
    """), {"order_id": order_id, "uid": user["uid"]})

    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")

    result = await db.execute(text("""
        INSERT INTO order_support_files
            (order_id, filename, content_type, file_size, gcs_path, file_role, uploaded_by)
        VALUES
            (:order_id, :filename, :content_type, :file_size, :gcs_path, :file_role, :user_id)
        RETURNING id, order_id, filename, content_type, file_size, gcs_path, file_role, created_at
    """), {
        "order_id": order_id,
        "filename": filename,
        "content_type": content_type,
        "file_size": file_size,
        "gcs_path": gcs_path,
        "file_role": file_role,
        "user_id": user["user_id"],
    })

    file_row = result.fetchone()
    if not file_row:
        raise HTTPException(status_code=500, detail="Failed to record support file")

    logger.info(f"Support file confirmed: order={order_id}, file={filename}, role={file_role}")

    return SupportFileResponse(**dict(file_row._mapping))


@router.get("/{order_id}/support-files", response_model=SupportFileListResponse)
async def list_support_files(
    order_id: str,
    user: dict         = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    """List support files for an order."""
    result = await db.execute(text("""
        SELECT sf.id, sf.order_id, sf.filename, sf.content_type,
               sf.file_size, sf.gcs_path, sf.file_role, sf.created_at
        FROM order_support_files sf
        JOIN orders o ON o.id = sf.order_id
        WHERE o.id = :order_id AND o.user_id = (
            SELECT id FROM users WHERE uid_firebase = :uid
        )
        ORDER BY sf.created_at ASC
    """), {"order_id": order_id, "uid": user["uid"]})

    rows = result.fetchall()
    files = [SupportFileResponse(**dict(r._mapping)) for r in rows]

    return SupportFileListResponse(files=files, total=len(files))
