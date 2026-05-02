"""
routers/users.py

使用者個人資料相關端點。
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
import logging

from core.database import get_db
from routers.auth import get_current_user, get_admin_user, get_editor_user
from models.schemas import (
    UserProfileResponse, InvitationCreate, InvitationResponse, 
    InvitationAccept, MessageResponse
)
import uuid
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/users", tags=["users"])

@router.get("/me", response_model=UserProfileResponse)
async def get_me(
    user: dict         = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    """獲取當前登入使用者的個人資料與權限"""
    result = await db.execute(text("""
        SELECT 
            u.id, u.uid_firebase, u.client_type, 
            u.company_name, u.tax_id, u.invoice_carrier,
            u.created_at,
            array_agg(DISTINCT ur.role) FILTER (WHERE ur.role IS NOT NULL) as roles,
            json_agg(DISTINCT jsonb_build_object('source_lang', ul.source_lang, 'target_lang', ul.target_lang)) FILTER (WHERE ul.source_lang IS NOT NULL) as languages
        FROM users u
        LEFT JOIN user_roles ur ON ur.user_id = u.id
        LEFT JOIN user_languages ul ON ul.user_id = u.id
        WHERE u.uid_firebase = :uid
        GROUP BY u.id
    """), {"uid": user["uid"]})
    
    row = result.fetchone()
    data = dict(row._mapping)
    roles = data.get("roles") or []
    langs = data.get("languages") or []
    return UserProfileResponse(
        **{**data, 
           "is_admin": "admin" in roles, 
           "is_editor": "editor" in roles,
           "is_qa": "qa" in roles,
           "languages": langs}
    )


@router.post("/invite", response_model=InvitationResponse)
async def create_invitation(
    body: InvitationCreate,
    user: dict         = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    """建立邀請連結 (Admin 邀 Editor, Editor 邀 QA)"""
    if body.role == "editor":
        if not user.get("is_admin"):
            raise HTTPException(status_code=403, detail="Only admins can invite editors")
    elif body.role == "qa":
        if not user.get("is_editor") and not user.get("is_admin"):
            raise HTTPException(status_code=403, detail="Only editors or admins can invite QAs")
    else:
        raise HTTPException(status_code=400, detail="Invalid role")

    token = str(uuid.uuid4())
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    
    result = await db.execute(text("""
        INSERT INTO invitations (inviter_id, email, role, token, expires_at)
        VALUES (:inviter_id, :email, :role, :token, :expires_at)
        RETURNING id, email, role, token, status, created_at, expires_at
    """), {
        "inviter_id": user["user_id"],
        "email":      body.email,
        "role":       body.role,
        "token":      token,
        "expires_at": expires_at,
    })
    await db.commit()
    return InvitationResponse(**dict(result.fetchone()._mapping))


@router.post("/accept-invite", response_model=MessageResponse)
async def accept_invitation(
    body: InvitationAccept,
    user: dict         = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    """接受邀請並綁定權限"""
    result = await db.execute(text("""
        SELECT id, role, status, expires_at FROM invitations
        WHERE token = :token AND status = 'pending'
    """), {"token": body.token})
    invite = result.fetchone()
    
    if not invite:
        raise HTTPException(status_code=404, detail="Invitation not found or already accepted")
    
    if invite.expires_at < datetime.now(timezone.utc):
        await db.execute(text("UPDATE invitations SET status = 'expired' WHERE id = :id"), {"id": invite.id})
        await db.commit()
        raise HTTPException(status_code=400, detail="Invitation expired")

    # 綁定權限
    await db.execute(text("""
        INSERT INTO user_roles (user_id, role)
        VALUES (:user_id, :role)
        ON CONFLICT DO NOTHING
    """), {"user_id": user["user_id"], "role": invite.role})
    
    # 更新邀請狀態
    await db.execute(text("UPDATE invitations SET status = 'accepted' WHERE id = :id"), {"id": invite.id})
    await db.commit()
    
    return MessageResponse(message=f"Invitation accepted. You are now a {invite.role}.")
