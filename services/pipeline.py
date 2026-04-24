"""
services/pipeline.py

觸發翻譯 Pipeline：把訂單 ID 發布到 Cloud Pub/Sub。
Cloud Workflows 訂閱後依 track_type 路由到對應的 Cloud Run Job。
"""

import json
import asyncio
import logging
from functools import lru_cache

from core.config import settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _get_publisher():
    """Pub/Sub PublisherClient 單例"""
    from google.cloud import pubsub_v1
    return pubsub_v1.PublisherClient()


async def trigger_pipeline(order_id: str) -> str:
    """
    將訂單 ID 發布到 Pub/Sub topic，觸發 Pipeline。
    回傳 message_id。
    失敗時記 log 但不拋出例外（付款已成功，pipeline 失敗可重試）。
    """
    try:
        publisher  = _get_publisher()
        topic_path = publisher.topic_path(
            settings.project_id,
            settings.pubsub_topic,
        )
        message = json.dumps({
            "order_id": order_id,
            "source":   "payment_confirmed",
        }).encode("utf-8")

        # PublisherClient.publish() 是同步的，用 run_in_executor 避免 block event loop
        loop = asyncio.get_event_loop()
        future = await loop.run_in_executor(
            None,
            lambda: publisher.publish(topic_path, message)
        )
        message_id = future.result()

        logger.info(f"Pipeline triggered: order={order_id}, message_id={message_id}")
        return message_id

    except Exception as e:
        logger.error(f"Failed to trigger pipeline for order {order_id}: {e}")
        # TODO: 寫入 dead-letter queue 或 Cloud Tasks 做延遲重試
        return ""


async def trigger_pipeline_retry(order_id: str, delay_seconds: int = 60):
    """
    延遲重試（用於 webhook 確認後 pipeline 失敗的補償）。
    TODO: 改用 Cloud Tasks 實作更可靠的延遲重試。
    """
    await asyncio.sleep(delay_seconds)
    await trigger_pipeline(order_id)
