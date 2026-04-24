"""
routers/auth.py

Firebase ID Token 驗證中介層。
所有需要登入的端點都 Depends(get_current_user)。
"""

from fastapi import Depends, HTTPException, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from core.database import get_db
from core.firebase import verify_firebase_token
import logging

logger = logging.getLogger(__name__)


async def get_current_user(
    authorization: str = Header(..., description="Bearer {Firebase ID Token}"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    驗證 Firebase ID Token，回傳 user dict。
    首次登入時自動在 users 表建立記錄。
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header format")

    token = authorization.removeprefix("Bearer ").strip()

    try:
        decoded = verify_firebase_token(token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    uid   = decoded["uid"]
    email = decoded.get("email", "")

    # 首次登入：自動建立 users 記錄
    result = await db.execute(
        text("SELECT id, client_type FROM users WHERE uid_firebase = :uid"),
        {"uid": uid}
    )
    user_row = result.fetchone()

    if not user_row:
        await db.execute(text("""
            INSERT INTO users (uid_firebase, client_type)
            VALUES (:uid, 'b2c')
            ON CONFLICT (uid_firebase) DO NOTHING
        """), {"uid": uid})
        await db.commit()

        result = await db.execute(
            text("SELECT id, client_type FROM users WHERE uid_firebase = :uid"),
            {"uid": uid}
        )
        user_row = result.fetchone()

    return {
        "uid":         uid,
        "email":       email,
        "user_id":     str(user_row.id),
        "client_type": user_row.client_type,
    }


async def get_admin_user(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """
    Admin 端點用。
    目前以 client_type 判斷，之後可改為 roles 表。
    TODO: 建立 admin_users 白名單表
    """
    # 暫時以環境變數 ADMIN_UIDS 做白名單
    import os
    admin_uids = os.environ.get("ADMIN_UIDS", "").split(",")
    if current_user["uid"] not in admin_uids:
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user
