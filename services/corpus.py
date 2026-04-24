"""
services/corpus.py

語料寫入 BigQuery。
只有 corpus_log.consent_given = TRUE 的訂單才執行。
"""

import logging
from datetime import datetime, timezone
from functools import lru_cache

from core.config import settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _get_bq_client():
    from google.cloud import bigquery
    return bigquery.Client(project=settings.project_id)


async def log_corpus_pair(
    order_id:       str,
    source_lang:    str,
    target_lang:    str,
    source_text:    str,
    bridge_text:    str,      # 台語→華文的中間產物
    translated_text: str,
    qa_score:       float | None,
    track_type:     str,
) -> str | None:
    """
    將翻譯語料對寫入 BigQuery corpus_pairs 表。
    回傳 insert_id，失敗回傳 None。
    """
    try:
        client  = _get_bq_client()
        dataset = f"ots_corpus_{settings.env}"
        table   = f"{settings.project_id}.{dataset}.corpus_pairs"

        row = {
            "order_id":        order_id,
            "source_lang":     source_lang,
            "target_lang":     target_lang,
            "source_text":     source_text,
            "bridge_text":     bridge_text,
            "translated_text": translated_text,
            "qa_score":        qa_score,
            "track_type":      track_type,
            "consent_given":   True,
            "created_at":      datetime.now(timezone.utc).isoformat(),
        }

        import asyncio
        loop = asyncio.get_event_loop()

        errors = await loop.run_in_executor(
            None,
            lambda: client.insert_rows_json(table, [row])
        )

        if errors:
            logger.error(f"BigQuery insert errors for order {order_id}: {errors}")
            return None

        # BigQuery streaming insert 沒有 row id，用 order_id 作為識別
        insert_id = f"bq-{order_id}"
        logger.info(f"Corpus logged to BigQuery: order={order_id}")
        return insert_id

    except Exception as e:
        logger.error(f"Failed to log corpus for order {order_id}: {e}")
        return None


async def update_corpus_consent(order_id: str, consent: bool, db) -> None:
    """
    更新 corpus_log 的 consent_given 欄位。
    由客戶在 Web Portal 勾選同意後呼叫。
    """
    from sqlalchemy import text
    await db.execute(text("""
        UPDATE corpus_log SET consent_given = :consent WHERE order_id = :order_id
    """), {"consent": consent, "order_id": order_id})
    await db.commit()
    logger.info(f"Corpus consent updated: order={order_id}, consent={consent}")
