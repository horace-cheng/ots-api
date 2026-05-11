import json
import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from .types import EventType
from .templates import render_template
from .factory import get_notification_provider

logger = logging.getLogger(__name__)

LANG_LABELS = {
    "tai-lo": "Taiwanese Hokkien", "hakka": "Hakka",
    "indigenous": "Taiwanese Indigenous", "zh-tw": "繁體中文",
    "en": "English", "ja": "日本語", "ko": "한국어",
}

_SUBJECT_MAP: dict[str, dict[str, str]] = {
    "order_created_ft": {
        "zh-tw": "OTS 訂單已建立 — 待付款", "en": "OTS Order Created — Awaiting Payment",
        "ja": "OTS 注文完了 — お支払い待ち", "ko": "OTS 주문 생성 — 결제 대기",
    },
    "order_created_lt": {
        "zh-tw": "OTS Literary Track 訂單已建立 — 待報價", "en": "OTS Literary Track Order Created — Awaiting Quote",
        "ja": "OTS Literary Track 注文完了 — 見積り待ち", "ko": "OTS Literary Track 주문 생성 — 견적 대기",
    },
    "quote_set": {
        "zh-tw": "OTS 報價已出爐", "en": "Your OTS Quote is Ready",
        "ja": "OTS 見積り完了", "ko": "OTS 견적 안내",
    },
    "payment_confirmed": {
        "zh-tw": "OTS 付款成功 — 訂單處理中", "en": "OTS Payment Confirmed — Order Processing",
        "ja": "OTS 支払い完了 — 処理中", "ko": "OTS 결제 완료 — 처리 중",
    },
    "delivery_complete": {
        "zh-tw": "OTS 翻譯已完成", "en": "Your OTS Translation is Ready",
        "ja": "OTS 翻訳完了", "ko": "OTS 번역 완료",
    },
    "qa_review_required": {
        "zh-tw": "OTS 訂單需要人工審閱", "en": "OTS Order Requires Review",
        "ja": "OTS 注文の確認が必要です", "ko": "OTS 주문 검토 필요",
    },
    "pipeline_error": {
        "zh-tw": "[管理員] OTS Pipeline 錯誤通知", "en": "[Admin] OTS Pipeline Error",
        "ja": "[管理者] OTS Pipeline エラー", "ko": "[관리자] OTS Pipeline 오류",
    },
    "editor_assigned": {
        "zh-tw": "您已被指派為 OTS 編輯", "en": "You've Been Assigned as OTS Editor",
        "ja": "OTS 編集者に任命されました", "ko": "OTS 편집자로 배정되었습니다",
    },
    "proofreader_assigned": {
        "zh-tw": "您已被指派為 OTS 校對", "en": "You've Been Assigned as OTS Proofreader",
        "ja": "OTS 校正者に任命されました", "ko": "OTS 교정자로 배정되었습니다",
    },
    "user_registered": {
        "zh-tw": "[管理員] OTS 新用戶註冊通知", "en": "[Admin] New User Registration",
        "ja": "[管理者] 新規ユーザー登録", "ko": "[관리자] 신규 사용자 등록",
    },
    "user_enabled": {
        "zh-tw": "OTS 帳號已啟用", "en": "Your OTS Account Has Been Re-enabled",
        "ja": "OTS アカウントが再有効化されました", "ko": "OTS 계정이 재활성화되었습니다",
    },
    "user_disabled": {
        "zh-tw": "OTS 帳號已停用", "en": "Your OTS Account Has Been Disabled",
        "ja": "OTS アカウントが無効化されました", "ko": "OTS 계정이 비활성화되었습니다",
    },
}

_HEADER_MAP: dict[str, dict[str, str]] = {
    "user_registered": {
        "zh-tw": "新用戶註冊通知", "en": "New User Registration",
        "ja": "新規ユーザー登録通知", "ko": "신규 사용자 등록 알림",
    },
    "user_enabled": {
        "zh-tw": "帳號已啟用", "en": "Account Re-enabled",
        "ja": "アカウント再有効化", "ko": "계정 재활성화",
    },
    "user_disabled": {
        "zh-tw": "帳號已停用", "en": "Account Disabled",
        "ja": "アカウント無効化", "ko": "계정 비활성화",
    },
    "delivery_complete": {
        "zh-tw": "翻譯完成", "en": "Translation Complete",
        "ja": "翻訳完了", "ko": "번역 완료",
    },
}

_FOOTER_MAP: dict[str, str] = {
    "zh-tw": "本郵件由 OTS 翻譯服務自動發送，請勿直接回覆。", "en": "This email was sent automatically by OTS Translation Service. Please do not reply.",
    "ja": "このメールは OTS 翻訳サービスより自動送信されています。返信しないでください。",
    "ko": "이 이메일은 OTS 번역 서비스에서 자동으로 발송되었습니다. 답장하지 마세요.",
}

_FOOTER_CONTACT: dict[str, str] = {
    "zh-tw": "如有任何疑問，請聯繫 service@ots.tw", "en": "For inquiries, contact service@ots.tw",
    "ja": "お問い合わせ：service@ots.tw", "ko": "문의: service@ots.tw",
}


def _subject(event_type: str, lang: str) -> str:
    return _SUBJECT_MAP.get(event_type, {}).get(lang, _SUBJECT_MAP.get(event_type, {}).get("zh-tw", event_type))


def _header(event_type: str, lang: str) -> str:
    return _HEADER_MAP.get(event_type, {}).get(lang, _subject(event_type, lang))


def _footer(lang: str) -> str:
    return _FOOTER_MAP.get(lang, _FOOTER_MAP["zh-tw"])


def _footer_contact(lang: str) -> str:
    return _FOOTER_CONTACT.get(lang, _FOOTER_CONTACT["zh-tw"])


def _is_valid_uuid(val: str | None) -> bool:
    if not val:
        return False
    try:
        uuid.UUID(val)
        return True
    except (ValueError, AttributeError):
        return False


async def resolve_user_email(db: AsyncSession, user_id: str) -> str | None:
    result = await db.execute(
        text("SELECT email FROM users WHERE id = :user_id"),
        {"user_id": user_id},
    )
    row = result.fetchone()
    return row.email if row else None


async def resolve_order_user_email(db: AsyncSession, order_id: str) -> str | None:
    if not _is_valid_uuid(order_id):
        return None
    result = await db.execute(text("""
        SELECT u.email FROM orders o
        JOIN users u ON u.id = o.user_id
        WHERE o.id = :order_id
    """), {"order_id": order_id})
    row = result.fetchone()
    return row.email if row else None


async def resolve_order_info(db: AsyncSession, order_id: str) -> dict | None:
    if not _is_valid_uuid(order_id):
        return None
    result = await db.execute(text("""
        SELECT o.id, o.source_lang, o.target_lang, o.status, o.word_count, o.price_ntd, o.quoted_price
        FROM orders o WHERE o.id = :order_id
    """), {"order_id": order_id})
    row = result.fetchone()
    if not row:
        return None
    return dict(row._mapping)


async def resolve_admin_emails(db: AsyncSession) -> list[str]:
    result = await db.execute(
        text("SELECT email FROM admin_users WHERE active = true"),
    )
    return [row.email for row in result.fetchall()]


async def resolve_assignment_user_email(db: AsyncSession, order_id: str, role: str) -> str | None:
    if not _is_valid_uuid(order_id):
        return None
    col = "editor_id" if role == "editor" else "proofreader_id"
    result = await db.execute(
        text(f"""
            SELECT u.email FROM assignments a
            JOIN users u ON u.id = a.{col}
            WHERE a.order_id = :order_id AND a.{col} IS NOT NULL
        """),
        {"order_id": order_id},
    )
    row = result.fetchone()
    return row.email if row else None


async def handle_notify_event(db: AsyncSession, event_data: dict):
    event_type = event_data.get("event_type", "")
    order_id = event_data.get("order_id")
    user_id = event_data.get("user_id")
    recipient_email = event_data.get("recipient_email")
    data = event_data.get("data") or {}

    logger.info(
        f"Handling notify event: type={event_type}, order={order_id}, "
        f"user={user_id}, recipient_email={recipient_email}, data_keys={list(data.keys())}"
    )

    provider = get_notification_provider()
    lang = "zh-tw"
    to_emails: list[str] = []
    ctx: dict = {"order_id": order_id or "", "env": settings.env}

    if event_type == EventType.USER_REGISTERED:
        logger.info(f"Resolving admin emails for USER_REGISTERED event")
        to_emails = await resolve_admin_emails(db)
        ctx.update({
            "user_email": data.get("user_email", recipient_email or ""),
            "user_id": data.get("user_id", user_id or ""),
            "admin_dashboard_url": f"{settings.web_portal_url}/admin/users",
        })

    elif event_type in (EventType.USER_ENABLED, EventType.USER_DISABLED):
        email = recipient_email or (user_id and await resolve_user_email(db, user_id))
        logger.info(f"USER_ENABLED/DISABLED: resolved_email={email}, user_id={user_id}, recipient_email={recipient_email}")
        if email:
            to_emails = [email]
        ctx["portal_url"] = settings.web_portal_url

    elif event_type in (EventType.EDITOR_ASSIGNED, EventType.PROOFREADER_ASSIGNED):
        role = "editor" if event_type == EventType.EDITOR_ASSIGNED else "proofreader"
        email = (recipient_email or await resolve_assignment_user_email(db, order_id, role))
        logger.info(f"ASSIGNMENT: role={role}, resolved_email={email}, order={order_id}")
        if email:
            to_emails = [email]
        order_info = order_id and await resolve_order_info(db, order_id)
        ctx.update({
            "source_lang": LANG_LABELS.get((order_info or {}).get("source_lang", ""), ""),
            "target_lang": LANG_LABELS.get((order_info or {}).get("target_lang", ""), ""),
            "portal_url": f"{settings.web_portal_url}/admin/literary",
        })

    elif event_type == EventType.PIPELINE_ERROR:
        to_emails = await resolve_admin_emails(db)
        ctx["error_message"] = data.get("error_message", "")

    elif event_type == EventType.QUOTE_SET:
        email = recipient_email or await resolve_order_user_email(db, order_id)
        if email:
            to_emails = [email]
        order_info = order_id and await resolve_order_info(db, order_id)
        ctx.update({
            "quoted_price": (order_info or {}).get("quoted_price", data.get("quoted_price", "")),
            "payment_url": f"{settings.web_portal_url}/orders/{order_id}",
        })

    elif event_type in (EventType.ORDER_CREATED_FT, EventType.ORDER_CREATED_LT):
        email = recipient_email or await resolve_order_user_email(db, order_id)
        if email:
            to_emails = [email]
        order_info = order_id and await resolve_order_info(db, order_id)
        ctx.update({
            "source_lang": LANG_LABELS.get((order_info or {}).get("source_lang", ""), ""),
            "target_lang": LANG_LABELS.get((order_info or {}).get("target_lang", ""), ""),
            "word_count": str((order_info or {}).get("word_count", "")),
            "price_ntd": str((order_info or {}).get("price_ntd", "")),
            "deadline": data.get("deadline", ""),
            "payment_url": f"{settings.web_portal_url}/orders/{order_id}",
        })

    elif event_type == EventType.PAYMENT_CONFIRMED:
        email = recipient_email or await resolve_order_user_email(db, order_id)
        if email:
            to_emails = [email]
        ctx["amount"] = str(data.get("amount", ""))

    elif event_type == EventType.DELIVERY_COMPLETE:
        email = recipient_email or await resolve_order_user_email(db, order_id)
        if email:
            to_emails = [email]
        order_info = order_id and await resolve_order_info(db, order_id)
        ctx.update({
            "source_lang": LANG_LABELS.get((order_info or {}).get("source_lang", ""), ""),
            "target_lang": LANG_LABELS.get((order_info or {}).get("target_lang", ""), ""),
            "qa_score": data.get("qa_score", ""),
            "output_url": data.get("output_url", f"{settings.web_portal_url}/orders/{order_id}"),
        })

    elif event_type == EventType.QA_REVIEW_REQUIRED:
        email = recipient_email or await resolve_order_user_email(db, order_id)
        if email:
            to_emails = [email]
        ctx["flag_count"] = str(data.get("flag_count", ""))

    if not to_emails:
        logger.info(f"No recipients resolved for {event_type}, order={order_id}")
        return

    logger.info(f"Recipients resolved: {to_emails} for {event_type}, order={order_id}")

    ctx["header_title"] = _header(event_type, lang)
    ctx["footer_text"] = _footer(lang)
    ctx["footer_contact"] = _footer_contact(lang)
    ctx["lang"] = lang

    subject = _subject(event_type, lang)
    logger.info(f"Rendering template: lang={lang}, event_type={event_type}, subject={subject!r}")
    body_html, body_text = render_template(lang, event_type, ctx)
    logger.info(f"Template rendered: html_len={len(body_html)}, text_len={len(body_text)}")

    for to_email in to_emails:
        try:
            logger.info(f"Sending email via {type(provider).__name__}: to={to_email}, subject={subject!r}")
            provider.send_email(to=to_email, subject=subject, body_html=body_html, body_text=body_text)
            logger.info(f"Email sent: {event_type} → {to_email}, order={order_id}")
        except Exception as e:
            logger.error(f"Failed to send email to {to_email} for {event_type}: {e}")
