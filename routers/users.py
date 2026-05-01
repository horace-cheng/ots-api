"""
routers/users.py

使用者個人資料相關端點。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
import logging

from core.database import get_db
from routers.auth import get_current_user
from models.schemas import UserProfileResponse

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
            u.is_editor, u.created_at,
            (au.id IS NOT NULL AND au.active = true) AS is_admin
        FROM users u
        LEFT JOIN admin_users au ON au.uid_firebase = u.uid_firebase AND au.active = true
        WHERE u.uid_firebase = :uid
    """), {"uid": user["uid"]})
    
    row = result.fetchone()
    return UserProfileResponse(**dict(row._mapping))
