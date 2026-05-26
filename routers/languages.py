"""
routers/languages.py

語言設定端點（Language Configs）
提供公開的選項列表，以及 Admin 的管理介面。
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from typing import List
import logging

from core.database import get_db
from routers.auth import get_admin_user
from core.languages import SUPPORTED_LANGUAGES, SUPPORTED_CODES
from models.schemas import (
    LanguageConfigResponse,
    LanguageConfigCreate,
    LanguageConfigUpdate,
    LanguageConfigListResponse,
    SupportedLanguageResponse,
    MessageResponse
)

logger = logging.getLogger(__name__)

# Public router
router = APIRouter(prefix="/languages", tags=["languages"])

# Admin router (needs to be included in main.py)
admin_router = APIRouter(prefix="/admin/languages", tags=["admin-languages"])


# ── Public Endpoints ────────────────────────────────────────────────────────

@router.get("", response_model=LanguageConfigListResponse)
async def list_active_languages(db: AsyncSession = Depends(get_db)):
    """
    Public: List all ACTIVE languages for the frontend order form dropdowns.
    """
    result = await db.execute(text("""
        SELECT id, code, label_zh, label_en, direction, is_active, sort_order, price_multiplier, created_at
        FROM language_configs
        WHERE is_active = true
        ORDER BY sort_order ASC, id ASC
    """))
    rows = result.fetchall()
    return LanguageConfigListResponse(languages=[LanguageConfigResponse(**dict(r._mapping)) for r in rows])


@router.get("/supported", response_model=List[SupportedLanguageResponse])
async def list_supported_languages(admin: dict = Depends(get_admin_user)):
    """
    Admin: List all supported language codes from the master list.
    Used for populating the 'Add Language' dropdown.
    """
    return [SupportedLanguageResponse(**lang) for lang in SUPPORTED_LANGUAGES]


# ── Admin Endpoints ─────────────────────────────────────────────────────────

@admin_router.get("", response_model=LanguageConfigListResponse)
async def admin_list_languages(db: AsyncSession = Depends(get_db), admin: dict = Depends(get_admin_user)):
    """
    Admin: List ALL configured languages (active and inactive).
    """
    result = await db.execute(text("""
        SELECT id, code, label_zh, label_en, direction, is_active, sort_order, price_multiplier, created_at
        FROM language_configs
        ORDER BY sort_order ASC, id ASC
    """))
    rows = result.fetchall()
    return LanguageConfigListResponse(languages=[LanguageConfigResponse(**dict(r._mapping)) for r in rows])


@admin_router.post("", response_model=LanguageConfigResponse)
async def admin_add_language(
    body: LanguageConfigCreate,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_admin_user)
):
    """
    Admin: Enable a new language from the master list.
    """
    if body.code not in SUPPORTED_CODES:
        raise HTTPException(status_code=400, detail=f"Language code '{body.code}' is not supported by the pipeline.")

    if body.direction not in ('source', 'target', 'both'):
        raise HTTPException(status_code=400, detail="Direction must be 'source', 'target', or 'both'")

    # Find the master list entry to get default labels
    master_lang = next((lang for lang in SUPPORTED_LANGUAGES if lang["code"] == body.code), None)
    if not master_lang:
        raise HTTPException(status_code=500, detail="Master list mismatch")
    
    # Validate direction is compatible with language's default_direction
    if master_lang["default_direction"] != "both" and body.direction != master_lang["default_direction"]:
        raise HTTPException(status_code=400, detail=f"Language '{body.code}' only supports direction '{master_lang['default_direction']}', got '{body.direction}'")
        
    # Check if already exists for this direction (or if there's a conflict with 'both')
    check = await db.execute(text("""
        SELECT id, direction FROM language_configs WHERE code = :code
    """), {"code": body.code})
    existing = check.fetchall()
    
    for row in existing:
        if row.direction == body.direction or row.direction == 'both' or body.direction == 'both':
            raise HTTPException(status_code=400, detail=f"Language code '{body.code}' already configured for this direction.")

    result = await db.execute(text("""
        INSERT INTO language_configs (code, label_zh, label_en, direction, sort_order, price_multiplier)
        VALUES (:code, :label_zh, :label_en, :direction, :sort_order, :price_multiplier)
        RETURNING id, code, label_zh, label_en, direction, is_active, sort_order, price_multiplier, created_at
    """), {
        "code": body.code,
        "label_zh": master_lang["label_zh"],
        "label_en": master_lang["label_en"],
        "direction": body.direction,
        "sort_order": body.sort_order,
        "price_multiplier": body.price_multiplier,
    })
    await db.commit()
    row = result.fetchone()
    return LanguageConfigResponse(**dict(row._mapping))


@admin_router.patch("/{config_id}", response_model=LanguageConfigResponse)
async def admin_update_language(
    config_id: int,
    body: LanguageConfigUpdate,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_admin_user)
):
    """
    Admin: Update an existing language config (toggle active, change sort order, labels, multiplier).
    """
    check = await db.execute(text("SELECT id, code FROM language_configs WHERE id = :id"), {"id": config_id})
    config_row = check.fetchone()
    if not config_row:
        raise HTTPException(status_code=404, detail="Language config not found")

    updates = []
    params = {"id": config_id}

    if body.label_zh is not None:
        updates.append("label_zh = :label_zh")
        params["label_zh"] = body.label_zh
    if body.label_en is not None:
        updates.append("label_en = :label_en")
        params["label_en"] = body.label_en
    if body.direction is not None:
        if body.direction not in ('source', 'target', 'both'):
            raise HTTPException(status_code=400, detail="Direction must be 'source', 'target', or 'both'")
        master = next((lang for lang in SUPPORTED_LANGUAGES if lang["code"] == config_row.code), None)
        if master and master["default_direction"] != "both" and body.direction != master["default_direction"]:
            raise HTTPException(status_code=400, detail=f"Language '{config_row.code}' only supports direction '{master['default_direction']}', got '{body.direction}'")
        updates.append("direction = :direction")
        params["direction"] = body.direction
    if body.is_active is not None:
        updates.append("is_active = :is_active")
        params["is_active"] = body.is_active
    if body.sort_order is not None:
        updates.append("sort_order = :sort_order")
        params["sort_order"] = body.sort_order
    if body.price_multiplier is not None:
        if body.price_multiplier <= 0:
            raise HTTPException(status_code=400, detail="Price multiplier must be > 0")
        updates.append("price_multiplier = :price_multiplier")
        params["price_multiplier"] = body.price_multiplier

    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided for update")

    result = await db.execute(text(f"""
        UPDATE language_configs
        SET {', '.join(updates)}
        WHERE id = :id
        RETURNING id, code, label_zh, label_en, direction, is_active, sort_order, price_multiplier, created_at
    """), params)
    await db.commit()
    row = result.fetchone()
    return LanguageConfigResponse(**dict(row._mapping))


@admin_router.delete("/{config_id}", response_model=MessageResponse)
async def admin_delete_language(
    config_id: int,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_admin_user)
):
    """
    Admin: Delete a language config.
    Only allowed if no orders reference this language code.
    """
    check = await db.execute(text("SELECT code, direction FROM language_configs WHERE id = :id"), {"id": config_id})
    lang = check.fetchone()
    if not lang:
        raise HTTPException(status_code=404, detail="Language config not found")

    # Check for existing orders
    if lang.direction in ('source', 'both'):
        count_res = await db.execute(text("SELECT COUNT(*) FROM orders WHERE source_lang = :code"), {"code": lang.code})
        if count_res.scalar() > 0:
            raise HTTPException(status_code=400, detail=f"Cannot delete: orders exist with source_lang='{lang.code}'")
            
    if lang.direction in ('target', 'both'):
        count_res = await db.execute(text("SELECT COUNT(*) FROM orders WHERE target_lang = :code"), {"code": lang.code})
        if count_res.scalar() > 0:
            raise HTTPException(status_code=400, detail=f"Cannot delete: orders exist with target_lang='{lang.code}'")

    await db.execute(text("DELETE FROM language_configs WHERE id = :id"), {"id": config_id})
    await db.commit()
    
    return MessageResponse(message="Language config deleted")
