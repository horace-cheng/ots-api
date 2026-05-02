"""
routers/auth.py

Firebase ID Token 驗證中介層。
所有需要登入的端點都 Depends(get_current_user)。
Admin 端點 Depends(get_admin_user)，查 admin_users 表判斷。
"""

from fastapi import Depends, HTTPException, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from core.database import get_db
from core.firebase import verify_firebase_token
import logging

logger = logging.getLogger(__name__)


async def get_current_user(
    authorization: str | None = Header(None, description="Bearer {Firebase ID Token}"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    驗證 Firebase ID Token，回傳 user dict。
    首次登入時自動在 users 表建立記錄。
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header format")

    token = authorization.removeprefix("Bearer ").strip()

    try:
        decoded = verify_firebase_token(token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    uid   = decoded["uid"]
    email = decoded.get("email", "")

    # 首次登入：自動建立 users 記錄；後續登入：同步 email 快取
    result = await db.execute(text("""
        SELECT 
            u.id, u.client_type, u.disabled, 
            array_agg(ur.role) FILTER (WHERE ur.role IS NOT NULL) as roles
        FROM users u 
        LEFT JOIN user_roles ur ON ur.user_id = u.id
        WHERE u.uid_firebase = :uid
        GROUP BY u.id
    """), {"uid": uid})
    user_row = result.fetchone()

    if not user_row:
        await db.execute(text("""
            INSERT INTO users (uid_firebase, email, client_type)
            VALUES (:uid, :email, 'b2c')
            ON CONFLICT (uid_firebase) DO NOTHING
        """), {"uid": uid, "email": email})
        await db.commit()

        result = await db.execute(text("""
            SELECT 
                u.id, u.client_type, u.disabled, 
                array_agg(ur.role) FILTER (WHERE ur.role IS NOT NULL) as roles
            FROM users u 
            LEFT JOIN user_roles ur ON ur.user_id = u.id
            WHERE u.uid_firebase = :uid
            GROUP BY u.id
        """), {"uid": uid})
        user_row = result.fetchone()
    elif email and not user_row.disabled:
        # 同步 email（Firebase 為源頭，DB 為快取）
        await db.execute(
            text("UPDATE users SET email = :email WHERE uid_firebase = :uid"),
            {"email": email, "uid": uid}
        )
        await db.commit()

    if user_row.disabled:
        raise HTTPException(status_code=403, detail="Account is disabled")

    roles = user_row.roles or []
    return {
        "uid":         uid,
        "email":       email,
        "user_id":     str(user_row.id),
        "client_type": user_row.client_type,
        "roles":       roles,
        "is_editor":   "editor" in roles,
        "is_admin":    "admin" in roles,
        "is_qa":       "qa" in roles,
    }


async def get_admin_user(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession   = Depends(get_db),
) -> dict:
    """
    Admin 端點用。查 admin_users 表，確認 uid_firebase 存在且 active = true。
    role 欄位回傳供端點做進一步的 superadmin 判斷。
    """
    result = await db.execute(text("""
        SELECT id, role, active
        FROM admin_users
        WHERE uid_firebase = :uid
    """), {"uid": current_user["uid"]})

    admin_row = result.fetchone()

    if not admin_row:
        raise HTTPException(status_code=403, detail="Admin access required")

    if not admin_row.active:
        raise HTTPException(status_code=403, detail="Admin account is disabled")

    return {
        **current_user,
        "admin_id": str(admin_row.id),
        "role":     admin_row.role,
    }


async def get_editor_user(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Editor 端點用。確認 is_editor = true"""
    if not current_user.get("is_editor"):
        raise HTTPException(status_code=403, detail="Editor access required")

    return current_user


async def get_qa_user(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """QA 端點用。確認 is_qa = true"""
    if not current_user.get("is_qa") and not current_user.get("is_admin"):
        raise HTTPException(status_code=403, detail="QA access required")

    return current_user