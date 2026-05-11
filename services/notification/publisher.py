import json
import asyncio
import logging
from functools import lru_cache

from core.config import settings
from .types import EventType, NotifyEvent

logger = logging.getLogger(__name__)

NOTIFY_TOPIC = f"ots-notify-{settings.env}"


@lru_cache(maxsize=1)
def _get_publisher():
    from google.cloud import pubsub_v1
    return pubsub_v1.PublisherClient()


async def publish_event(event: NotifyEvent) -> str:
    try:
        publisher = _get_publisher()
        topic_path = publisher.topic_path(settings.project_id, NOTIFY_TOPIC)
        logger.info(f"Publishing to topic: {topic_path}")
        message = json.dumps({
            "event_type": event.event_type.value,
            "order_id": event.order_id,
            "user_id": event.user_id,
            "recipient_email": event.recipient_email,
            "data": event.data or {},
            "timestamp": event.timestamp,
        }).encode("utf-8")

        loop = asyncio.get_event_loop()
        future = await loop.run_in_executor(
            None,
            lambda: publisher.publish(topic_path, message),
        )
        message_id = future.result()
        logger.info(
            f"Notify event published: type={event.event_type.value}, "
            f"order={event.order_id}, message_id={message_id}"
        )
        return message_id

    except Exception as e:
        logger.error(f"Failed to publish notify event: {e}")
        return ""


async def publish_event_sync(
    event_type: EventType,
    order_id: str | None = None,
    user_id: str | None = None,
    recipient_email: str | None = None,
    data: dict | None = None,
) -> str:
    from .types import make_event
    event = make_event(event_type, order_id, user_id, recipient_email, data)
    return await publish_event(event)
