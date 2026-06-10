"""
routers/internal.py

內部端點，供 Cloud Workflows 呼叫。
使用 OIDC token（Google SA）驗證，不用 Firebase token。
不對外公開（docs_url=None 時不顯示，但 URL 仍可存取）。
"""

import base64
import json
import os
import logging

from fastapi import APIRouter, Depends, HTTPException, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
import google.auth.transport.requests
import google.oauth2.id_token

from core.database import get_db
from core.config import settings
from services.notification import publish_event_sync, EventType
from services.notification.sender import handle_notify_event

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/internal", tags=["internal"])

# Workflow SA email 白名單
ALLOWED_SA = os.environ.get(
    "INTERNAL_ALLOWED_SA",
    "ots-workflow-dev@ots-translation.iam.gserviceaccount.com"
).split(",")


async def verify_oidc_token(
    authorization: str = Header(...),
) -> dict:
    """
    驗證 Cloud Workflows 的 OIDC token。
    確認 token 的 email 在 ALLOWED_SA 白名單內。
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    token = authorization.removeprefix("Bearer ").strip()

    try:
        request   = google.auth.transport.requests.Request()
        id_info   = google.oauth2.id_token.verify_oauth2_token(
            token, request, audience=None
        )
        email = id_info.get("email", "")

        if not any(email == sa or email.endswith("@" + sa.split("@")[-1])
                   for sa in ALLOWED_SA):
            # 在 dev 環境下，也允許 Workflow SA
            if not email.endswith(".iam.gserviceaccount.com"):
                raise HTTPException(status_code=403, detail=f"SA not allowed: {email}")

        return {"email": email, "sub": id_info.get("sub")}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"OIDC verification failed: {e}")


# ── GET /internal/orders/{order_id} ──────────────────────────────────────────
@router.get("/orders/{order_id}")
async def get_order_internal(
    order_id:  str,
    caller:    dict       = Depends(verify_oidc_token),
    db:        AsyncSession = Depends(get_db),
):
    """
    Workflow 用來取得訂單的 track_type。
    只回傳 Workflow 需要的欄位。
    """
    result = await db.execute(text("""
        SELECT id, track_type, status, source_lang, target_lang
        FROM orders
        WHERE id = :order_id
    """), {"order_id": order_id})

    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Order not found: {order_id}")

    logger.info(f"Internal order query: {order_id} by {caller['email']}")
    return dict(row._mapping)


# ── POST /internal/notify ─────────────────────────────────────────────────────
@router.post("/notify")
async def notify_internal(
    body:   dict,
    caller: dict       = Depends(verify_oidc_token),
    db:     AsyncSession = Depends(get_db),
):
    """
    Workflow 發送通知（pipeline 狀態變更、人工 QA 需求等）。
    將事件發布到 Pub/Sub notify topic，由 email sender 非同步處理。
    """
    notify_type = body.get("type", "unknown")
    order_id    = body.get("order_id", "")

    logger.info(f"Internal notify: type={notify_type}, order={order_id}, from={caller['email']}")

    if notify_type == "pipeline_error":
        await db.execute(text("""
            UPDATE orders SET status = 'qa_review'
            WHERE id = :order_id AND status = 'processing'
        """), {"order_id": order_id})
        await db.commit()

    event_type = _map_notify_type(notify_type)
    if event_type:
        data = {}
        if notify_type == "pipeline_error":
            data["error_message"] = body.get("error", "")
        elif notify_type == "human_qa_required":
            data["flag_count"] = body.get("flag_count", 0)
        elif notify_type in ("delivery_complete",):
            data.update({
                "qa_score": body.get("qa_score", ""),
                "output_url": body.get("output_url", ""),
            })
        elif notify_type == "gt_stage_complete":
            data["stage"] = body.get("stage", "")
            data["stage_label"] = body.get("stage_label", "")

        await publish_event_sync(
            event_type=event_type,
            order_id=order_id,
            data=data,
        )

    return {"message": "notification received", "type": notify_type}


_NOTIFY_TYPE_MAP = {
    "delivery_complete": EventType.DELIVERY_COMPLETE,
    "human_qa_required": EventType.QA_REVIEW_REQUIRED,
    "pipeline_error": EventType.PIPELINE_ERROR,
    "literary_assign_editor": EventType.EDITOR_ASSIGNED,
    "literary_assign_proofreader": EventType.PROOFREADER_ASSIGNED,
    "literary_editor_timeout": EventType.EDITOR_ASSIGNED,
    "literary_proofreader_timeout": EventType.PROOFREADER_ASSIGNED,
    "gt_stage_complete": EventType.GT_STAGE_COMPLETE,
}


def _map_notify_type(notify_type: str) -> EventType | None:
    return _NOTIFY_TYPE_MAP.get(notify_type)


# ── POST /internal/pubsub-notify ───────────────────────────────────────────────
@router.post("/pubsub-notify")
async def pubsub_notify(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Pub/Sub push subscription 接收端點。
    接收來自 ots-notify-{env} topic 的事件，處理 email 發送。
    使用 Cloud IAM 驗證（Pub/Sub push 會自動附加 OIDC token）。
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    envelope = body.get("message", {})
    data_b64 = envelope.get("data", "")
    if not data_b64:
        return {"status": "ok", "detail": "no data"}

    try:
        decoded = base64.b64decode(data_b64).decode("utf-8")
        event_data = json.loads(decoded)
    except Exception as e:
        logger.warning(f"Failed to decode Pub/Sub message: {e}")
        return {"status": "ok", "detail": "decode failed"}

    logger.info(f"Pub/Sub notify: type={event_data.get('event_type')}, order={event_data.get('order_id')}")

    await handle_notify_event(db, event_data)

    return {"status": "ok"}


# ── GET /internal/qa-flags ────────────────────────────────────────────────────
@router.get("/qa-flags")
async def get_qa_flags_internal(
    order_id:   str,
    flag_level: str | None = None,
    resolved:   bool       = False,
    caller:     dict       = Depends(verify_oidc_token),
    db:         AsyncSession = Depends(get_db),
):
    """Workflow 輪詢 must_fix QA flags 是否全部解決"""
    conditions = [
        "pj.order_id = :order_id",
        "qf.resolved = :resolved",
    ]
    params: dict = {"order_id": order_id, "resolved": resolved}

    if flag_level:
        conditions.append("qf.flag_level = :flag_level")
        params["flag_level"] = flag_level

    where = " AND ".join(conditions)

    count_result = await db.execute(text(f"""
        SELECT COUNT(*) as total
        FROM qa_flags qf
        JOIN pipeline_jobs pj ON pj.id = qf.job_id
        WHERE {where}
    """), params)

    total = count_result.scalar()
    return {"total": total, "order_id": order_id, "flag_level": flag_level}


# ── GET /internal/assignments/{order_id} ──────────────────────────────────────
@router.get("/assignments/{order_id}")
async def get_assignment_internal(
    order_id:  str,
    caller:    dict       = Depends(verify_oidc_token),
    db:        AsyncSession = Depends(get_db),
):
    """
    Workflow 用來輪詢 Literary Track 的編輯/校對狀態。
    使用 OIDC 驗證（Google SA），而非 Firebase token。
    """
    result = await db.execute(text("""
        SELECT status FROM assignments WHERE order_id = :order_id
    """), {"order_id": order_id})

    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Assignment not found")

    return {"order_id": order_id, "status": row.status}
