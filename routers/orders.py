"""
routers/orders.py

訂單相關端點。
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from datetime import datetime, timezone, timedelta
import uuid
import json
import logging

from core.database import get_db
from core.config import settings
from routers.auth import get_current_user
from models.schemas import (
    OrderCreate, OrderUpdate, OrderResponse, OrderDetail, OrderListResponse, MessageResponse, QuoteUpdate,
    SamplePackageGenerateResponse,
)
from services.payment import get_payment_gateway, PaymentRequest, PaymentMethod
from core import storage
from services.document_converter import convert_document
from services.gemini import generate_synopsis, generate_book_fact_sheet, generate_market_analysis
from services.notification import publish_event_sync, EventType

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/orders", tags=["orders"])


def _calc_deadline(track_type: str) -> datetime:
    """Fast Track: +48hr；Literary Track: +30天（協議後可延長）"""
    now = datetime.now(timezone.utc)
    if track_type == "fast":
        return now + timedelta(hours=48)
    return now + timedelta(days=30)


def _calc_price(track_type: str, word_count: int, lang_multiplier: float = 1.0) -> int:
    """
    簡易報價計算（實際報價依業務規則調整）。
    Fast Track:    NT$2/字，最低 NT$2,000
    Literary Track: NT$6/字，最低 NT$20,000
    """
    base_rate = {"fast": 2, "literary": 6}.get(track_type, 2)
    price = int(word_count * base_rate * lang_multiplier)
    minimum = {"fast": 2000, "literary": 20000}.get(track_type, 2000)
    return max(price, minimum)


# ── POST /orders ──────────────────────────────────────────────────────────────
@router.post("", response_model=OrderResponse, status_code=201)
async def create_order(
    body: OrderCreate,
    user: dict = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    建立翻譯訂單。
    回傳 order_id 和 payment_url（付款頁面）。
    """
    order_id = str(uuid.uuid4())
    now      = datetime.now(timezone.utc)
    title    = (body.title or "").strip() or None
    is_lit   = body.track_type == "literary"

    # Validate languages against DB active configs
    lang_res = await db.execute(text("""
        SELECT code, direction, price_multiplier FROM language_configs WHERE is_active = true
    """))
    active_langs = lang_res.fetchall()

    source_lang_ok = any(l.code == body.source_lang and l.direction in ('source', 'both') for l in active_langs)
    if not source_lang_ok:
        raise HTTPException(status_code=422, detail=f"Invalid or inactive source language: {body.source_lang}")

    target_lang_entry = next((l for l in active_langs if l.code == body.target_lang and l.direction in ('target', 'both')), None)
    if not target_lang_entry:
        raise HTTPException(status_code=422, detail=f"Invalid or inactive target language: {body.target_lang}")

    lang_multiplier = float(target_lang_entry.price_multiplier)

    # Fast Track: upfront pricing; Literary Track: awaiting quote
    price    = 0 if is_lit else _calc_price(body.track_type, body.word_count, lang_multiplier)
    ref_price = None if not is_lit else _calc_price(body.track_type, body.word_count, lang_multiplier)
    deadline = _calc_deadline(body.track_type)
    status   = "awaiting_quote" if is_lit else "pending_payment"

    sample_pkg = body.sample_package and is_lit

    # 建立訂單
    await db.execute(text("""
        INSERT INTO orders (
            id, user_id, track_type, status,
            source_lang, target_lang, word_count, price_ntd,
            reference_price,
            title, notes, created_at, deadline_at,
            has_sample_package
        )
        SELECT
            :id, u.id, :track_type, :status,
            :source_lang, :target_lang, :word_count, :price_ntd,
            :reference_price,
            :title, :notes, :now, :deadline,
            :has_sample_package
        FROM users u WHERE u.uid_firebase = :uid
    """), {
        "id":                 order_id,
        "track_type":         body.track_type,
        "status":             status,
        "source_lang":        body.source_lang,
        "target_lang":        body.target_lang,
        "word_count":         body.word_count,
        "price_ntd":          price,
        "reference_price":    ref_price,
        "title":              title,
        "notes":              body.notes,
        "now":                now,
        "deadline":           deadline,
        "uid":                user["uid"],
        "has_sample_package": sample_pkg,
    })

    if sample_pkg:
        await db.execute(text("""
            INSERT INTO order_sample_packages (order_id, status)
            VALUES (:order_id, 'draft')
        """), {"order_id": order_id})

    # 建立付款記錄（LT 報價後才建立）
    if not is_lit:
        await db.execute(text("""
            INSERT INTO payments (order_id, amount_ntd, payment_status)
            VALUES (:order_id, :amount, 'pending')
        """), {"order_id": order_id, "amount": price})

    # 建立指派記錄（所有訂單都使用 assignments 表）
    await db.execute(text("""
        INSERT INTO assignments (order_id, status)
        VALUES (:order_id, 'pending')
    """), {"order_id": order_id})

    # 語料 log（預設 consent = false，待客戶確認）
    await db.execute(text("""
        INSERT INTO corpus_log (order_id, consent_given)
        VALUES (:order_id, false)
    """), {"order_id": order_id})

    await db.commit()

    # 建立付款 URL（LT 報價後才建立）
    if is_lit:
        payment_url = ""
    else:
        gateway = get_payment_gateway()
        base_url = settings.web_portal_url
        payment_req = PaymentRequest(
            order_id    = order_id,
            amount_ntd  = price,
            description = f"OTS {body.track_type.upper()} 翻譯服務 ({body.word_count}字)",
            return_url  = f"{base_url}/orders/{order_id}",
            notify_url  = f"{base_url}/payments/webhook",
            method      = PaymentMethod.CREDIT_CARD,
        )
        payment_result = gateway.create_payment(payment_req)

        # 回存 gateway_trade_no
        await db.execute(text("""
            UPDATE payments SET ecpay_trade_no = :trade_no WHERE order_id = :order_id
        """), {"trade_no": payment_result.gateway_trade_no, "order_id": order_id})
        await db.commit()

        payment_url = payment_result.payment_url

    logger.info(f"Order created: {order_id} ({body.track_type}, {body.word_count}字, status={status})")

    event_type = EventType.ORDER_CREATED_LT if is_lit else EventType.ORDER_CREATED_FT
    await publish_event_sync(
        event_type=event_type,
        order_id=order_id,
        data={
            "deadline": deadline.isoformat(),
            "word_count": body.word_count,
        },
    )

    return OrderResponse(
        order_id           = order_id,
        status             = status,
        payment_url        = payment_url,
        track_type         = body.track_type,
        word_count         = body.word_count,
        price_ntd          = price,
        has_sample_package = sample_pkg,
        created_at         = now,
    )


# ── GET /orders ───────────────────────────────────────────────────────────────
@router.get("", response_model=OrderListResponse)
async def list_orders(
    status:     str | None = Query(None, description="篩選訂單狀態"),
    track_type: str | None = Query(None, description="篩選軌道類型"),
    limit:      int        = Query(20, ge=1, le=100),
    offset:     int        = Query(0, ge=0),
    user: dict             = Depends(get_current_user),
    db:   AsyncSession     = Depends(get_db),
):
    """列出當前用戶的訂單"""
    conditions = ["u.uid_firebase = :uid"]
    params: dict = {"uid": user["uid"], "limit": limit, "offset": offset}

    if status:
        conditions.append("o.status = :status")
        params["status"] = status
    if track_type:
        conditions.append("o.track_type = :track_type")
        params["track_type"] = track_type

    where = " AND ".join(conditions)

    result = await db.execute(text(f"""
        SELECT
            o.id, o.track_type, o.status, o.source_lang, o.target_lang,
            o.word_count, o.price_ntd, o.quoted_price, o.reference_price, o.title, o.notes,
            o.has_sample_package,
            o.created_at, o.deadline_at, o.delivered_at,
            o.gcs_output_path,
            p.payment_status, p.invoice_no
        FROM orders o
        JOIN users u ON u.id = o.user_id
        LEFT JOIN payments p ON p.order_id = o.id
        WHERE {where}
        ORDER BY o.created_at DESC
        LIMIT :limit OFFSET :offset
    """), params)

    rows = result.fetchall()

    count_result = await db.execute(text(f"""
        SELECT COUNT(*) FROM orders o
        JOIN users u ON u.id = o.user_id
        WHERE {where}
    """), {k: v for k, v in params.items() if k not in ("limit", "offset")})
    total = count_result.scalar()

    orders = [OrderDetail(**dict(r._mapping)) for r in rows]
    return OrderListResponse(orders=orders, total=total)


# ── GET /orders/{order_id} ────────────────────────────────────────────────────
@router.get("/{order_id}", response_model=OrderDetail)
async def get_order(
    order_id: str,
    user: dict         = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    """取得單筆訂單詳情"""
    result = await db.execute(text("""
        SELECT
            o.id, o.track_type, o.status, o.source_lang, o.target_lang,
            o.word_count, o.price_ntd, o.quoted_price, o.reference_price, o.title, o.notes,
            o.has_sample_package,
            o.created_at, o.deadline_at, o.delivered_at,
            o.gcs_output_path,
            p.payment_status, p.invoice_no
        FROM orders o
        JOIN users u ON u.id = o.user_id
        LEFT JOIN payments p ON p.order_id = o.id
        WHERE o.id = :order_id AND u.uid_firebase = :uid
    """), {"order_id": order_id, "uid": user["uid"]})

    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")

    return OrderDetail(**dict(row._mapping))


# ── DELETE /orders/{order_id} ─────────────────────────────────────────────────
@router.delete("/{order_id}", response_model=MessageResponse)
async def cancel_order(
    order_id: str,
    user: dict         = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    取消訂單。
    所有未付款狀態（pending_payment / awaiting_quote / quoted）均可取消。
    """
    result = await db.execute(text("""
        SELECT o.id, o.status FROM orders o
        JOIN users u ON u.id = o.user_id
        WHERE o.id = :order_id AND u.uid_firebase = :uid
    """), {"order_id": order_id, "uid": user["uid"]})

    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    if row.status not in ("pending_payment", "awaiting_quote", "quoted"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot cancel order with status '{row.status}'"
        )

    await db.execute(text("""
        UPDATE orders SET status = 'cancelled' WHERE id = :order_id
    """), {"order_id": order_id})
    await db.commit()

    logger.info(f"Order cancelled: {order_id}")
    return MessageResponse(message="Order cancelled")


# ── PATCH /orders/{order_id} ────────────────────────────────────────────────────
@router.patch("/{order_id}", response_model=OrderDetail)
async def update_order(
    order_id: str,
    body: OrderUpdate,
    user: dict         = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    更新訂單標題。僅可在訂單交付前修改。
    """
    result = await db.execute(text("""
        SELECT o.id, o.status, o.delivered_at FROM orders o
        JOIN users u ON u.id = o.user_id
        WHERE o.id = :order_id AND u.uid_firebase = :uid
    """), {"order_id": order_id, "uid": user["uid"]})

    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    if row.status in ("delivered", "cancelled"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot update order with status '{row.status}'"
        )

    if body.title is not None:
        title = body.title.strip() or None
        await db.execute(text("""
            UPDATE orders SET title = :title WHERE id = :order_id
        """), {"title": title, "order_id": order_id})
        await db.commit()
        logger.info(f"Order title updated: {order_id} → {title!r}")

    # Return updated order
    result = await db.execute(text("""
        SELECT
            o.id, o.track_type, o.status, o.source_lang, o.target_lang,
            o.word_count, o.price_ntd, o.quoted_price, o.reference_price, o.title, o.notes,
            o.has_sample_package,
            o.created_at, o.deadline_at, o.delivered_at,
            o.gcs_output_path,
            p.payment_status, p.invoice_no
        FROM orders o
        JOIN users u ON u.id = o.user_id
        LEFT JOIN payments p ON p.order_id = o.id
        WHERE o.id = :order_id AND u.uid_firebase = :uid
    """), {"order_id": order_id, "uid": user["uid"]})

    return OrderDetail(**dict(result.fetchone()._mapping))


# ── Sample Translation Package ──────────────────────────────────────────────

@router.post("/{order_id}/sample-package/generate", response_model=SamplePackageGenerateResponse)
async def generate_sample_package(
    order_id: str,
    user: dict         = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    """Generate Sample Translation Package content from support files."""
    # 1. Verify order
    result = await db.execute(text("""
        SELECT o.id, o.has_sample_package, o.status, o.title, o.word_count,
               o.source_lang, o.target_lang, u.id as user_id
        FROM orders o
        JOIN users u ON u.id = o.user_id
        WHERE o.id = :order_id AND u.uid_firebase = :uid
    """), {"order_id": order_id, "uid": user["uid"]})
    order = result.fetchone()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if not order.has_sample_package:
        raise HTTPException(status_code=400, detail="Order does not have a sample package")
    # 2. Read support files
    support_files = await db.execute(text("""
        SELECT sf.gcs_path, sf.filename, sf.file_role
        FROM order_support_files sf
        WHERE sf.order_id = :order_id
        ORDER BY sf.created_at ASC
    """), {"order_id": order_id})
    sf_rows = support_files.fetchall()

    if not sf_rows:
        raise HTTPException(
            status_code=400,
            detail="請先上傳至少一份參考文件才能產生試譯包。Please upload at least one support file to generate the sample package."
        )

    # 3. Extract text from support files
    all_text = ""
    background_text = ""
    for sf in sf_rows:
        try:
            raw_bytes, _ = storage.read_blob(sf.gcs_path)
            doc = convert_document(raw_bytes, sf.filename)
            doc_text = doc.text.strip()
            all_text += f"\n\n--- {sf.filename} ({sf.file_role}) ---\n\n{doc_text}"
            if sf.file_role == "background":
                background_text += f"\n\n{doc_text}"
        except Exception as e:
            logger.warning(f"Failed to read support file {sf.gcs_path}: {e}")

    source_text = background_text or all_text

    # 4. Generate all components via Gemini (parallel calls)
    import asyncio
    synopsis_task = generate_synopsis(
        source_text=source_text,
        source_lang=order.source_lang,
        target_lang=order.target_lang,
        api_key=settings.gemini_api_key,
    )
    fact_sheet_task = generate_book_fact_sheet(
        source_text=source_text,
        source_lang=order.source_lang,
        target_lang=order.target_lang,
        title=order.title or "",
        word_count=order.word_count,
        api_key=settings.gemini_api_key,
    )
    market_task = generate_market_analysis(
        source_text=source_text,
        source_lang=order.source_lang,
        target_lang=order.target_lang,
        api_key=settings.gemini_api_key,
    )
    synopsis, book_fact_sheet, market_analysis = await asyncio.gather(
        synopsis_task, fact_sheet_task, market_task,
    )

    if not synopsis and source_text:
        synopsis = source_text[:800]

    # 5. Pre-fill translator_bio from assigned editor's profile
    translator_bio = ""
    editor_res = await db.execute(text("""
        SELECT u.bio FROM assignments a
        JOIN users u ON u.id = a.editor_id
        WHERE a.order_id = :order_id AND a.editor_id IS NOT NULL AND u.bio != ''
        LIMIT 1
    """), {"order_id": order_id})
    editor_row = editor_res.fetchone()
    if editor_row:
        translator_bio = editor_row.bio

    # 6. Update package
    await db.execute(text("""
        UPDATE order_sample_packages
        SET status = 'generated',
            translator_bio = :translator_bio,
            book_fact_sheet = CAST(:book_fact_sheet AS jsonb),
            synopsis = :synopsis,
            market_analysis = :market_analysis,
            updated_at = NOW()
        WHERE order_id = :order_id
    """), {
        "order_id": order_id,
        "translator_bio": translator_bio,
        "book_fact_sheet": json.dumps(book_fact_sheet),
        "synopsis": synopsis,
        "market_analysis": market_analysis,
    })
    await db.commit()

    logger.info(f"Sample package generated: order={order_id}")
    return SamplePackageGenerateResponse(
        message="Sample package generated",
        translator_bio=translator_bio,
        book_fact_sheet=book_fact_sheet,
        synopsis=synopsis,
        market_analysis=market_analysis,
    )


@router.get("/{order_id}/sample-package/download")
async def download_sample_package(
    order_id: str,
    user: dict         = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    """Download combined Sample Translation Package (all 5 components) as HTML."""
    # 1. Verify order + delivery status
    result = await db.execute(text("""
        SELECT o.status, o.has_sample_package, o.id
        FROM orders o
        JOIN users u ON u.id = o.user_id
        WHERE o.id = :order_id AND u.uid_firebase = :uid
    """), {"order_id": order_id, "uid": user["uid"]})
    order = result.fetchone()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if not order.has_sample_package:
        raise HTTPException(status_code=400, detail="Order does not have a sample package")
    if order.status != "delivered":
        raise HTTPException(status_code=400, detail="Sample package is only available after delivery")

    # 2. Fetch package data
    pkg_res = await db.execute(text("""
        SELECT p.*, u.bio as editor_bio
        FROM order_sample_packages p
        LEFT JOIN assignments a ON a.order_id = p.order_id
        LEFT JOIN users u ON u.id = a.editor_id
        WHERE p.order_id = :order_id
    """), {"order_id": order_id})
    pkg = pkg_res.fetchone()
    if not pkg:
        raise HTTPException(status_code=404, detail="Sample package not found")

    # 3. Fetch translated segments (component 4)
    translations = storage.read_temp_json(order_id, "translations.json")
    translated_text = ""
    if translations:
        translated_text = "\n\n".join(
            t.get("translated", "") for t in sorted(translations, key=lambda x: x.get("index", 0))
        )

    # 4. Build combined HTML
    fact_sheet = pkg.book_fact_sheet or {}
    if isinstance(fact_sheet, str):
        import json as _json
        fact_sheet = _json.loads(fact_sheet)

    def _h(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")

    html_parts = ["""
<!DOCTYPE html>
<html lang="zh-TW">
<head><meta charset="utf-8"><title>Sample Translation Package</title>
<style>
  body { font-family: 'Noto Sans TC', 'Segoe UI', sans-serif; max-width: 800px; margin: 2rem auto; padding: 0 1rem; color: #1a1a2e; line-height: 1.8; }
  h1 { font-size: 1.5rem; border-bottom: 2px solid #b8852a; padding-bottom: 0.5rem; margin-top: 2rem; color: #b8852a; }
  h2 { font-size: 1.2rem; margin-top: 1.5rem; color: #333; }
  .section { margin-bottom: 2rem; padding: 1rem; background: #fafafa; border-radius: 8px; border-left: 3px solid #b8852a; }
  table { width: 100%; border-collapse: collapse; margin: 0.5rem 0; }
  td { padding: 0.4rem 0.5rem; border-bottom: 1px solid #e2e8f0; vertical-align: top; }
  td:first-child { font-weight: 600; width: 120px; color: #64748b; }
  .translation-sample { white-space: pre-wrap; background: #f8f5f0; padding: 1rem; border-radius: 8px; font-size: 0.95rem; }
</style>
</head><body>
"""]

    # Component 1: Translator Bio
    bio_text = pkg.translator_bio or fact_sheet.get("translator_bio", "")
    html_parts.append(f'<h1>1. Translator Bio</h1><div class="section"><p>{_h(bio_text)}</p></div>')

    # Component 2: Book Fact Sheet
    fs_html = "<table>"
    for key, label in [("title", "Title"), ("author", "Author"), ("publisher", "Publisher"),
                        ("pub_date", "Publication Date"), ("word_count", "Word Count"),
                        ("category", "Category"), ("sales", "Sales Info")]:
        val = fact_sheet.get(key, "")
        if val:
            fs_html += f"<tr><td>{label}</td><td>{_h(str(val))}</td></tr>"
    fs_html += "</table>"
    html_parts.append(f'<h1>2. Book Fact Sheet</h1><div class="section">{fs_html}</div>')

    # Component 3: Synopsis
    synopsis_text = pkg.synopsis or ""
    html_parts.append(f'<h1>3. Synopsis</h1><div class="section"><p>{_h(synopsis_text)}</p></div>')

    # Component 4: Translation Sample
    html_parts.append(f'<h1>4. Translation Sample</h1><div class="section"><div class="translation-sample">{_h(translated_text)}</div></div>')

    # Component 5: Market Analysis
    ma_text = pkg.market_analysis or ""
    html_parts.append(f'<h1>5. Market Analysis</h1><div class="section"><p>{_h(ma_text)}</p></div>')

    html_parts.append("</body></html>")
    full_html = "\n".join(html_parts)

    # 5. Upload to GCS and return signed URL
    from datetime import timedelta as td
    from core.storage import get_storage_client, _get_signing_credentials

    gcs_path = f"orders/{order_id}/sample-package.html"
    client = get_storage_client()
    bucket = client.bucket(settings.gcs_outputs_bucket)
    blob = bucket.blob(gcs_path)
    blob.upload_from_string(full_html, content_type="text/html; charset=utf-8")

    signed_url = blob.generate_signed_url(
        version="v4",
        expiration=td(hours=24),
        method="GET",
        credentials=_get_signing_credentials(),
    )

    return {"download_url": signed_url, "message": "Sample package ready"}
