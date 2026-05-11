import json
from unittest.mock import MagicMock, patch

import pytest

from services.notification import publish_event_sync, EventType
from services.notification.types import make_event
from services.notification.publisher import publish_event


async def test_publish_event_returns_message_id():
    mock_publisher = MagicMock()
    mock_future = MagicMock()
    mock_future.result.return_value = "msg-001"
    mock_publisher.publish.return_value = mock_future
    mock_publisher.topic_path.return_value = "projects/test/topics/ots-notify-dev"

    with patch("services.notification.publisher._get_publisher", return_value=mock_publisher):
        result = await publish_event(make_event(
            EventType.USER_REGISTERED,
            user_id="user-123",
            data={"user_email": "test@test.com"},
        ))

    assert result == "msg-001"
    mock_publisher.publish.assert_called_once()


async def test_publish_event_contains_correct_data():
    mock_publisher = MagicMock()
    mock_future = MagicMock()
    mock_future.result.return_value = "msg-002"
    mock_publisher.publish.return_value = mock_future

    with patch("services.notification.publisher._get_publisher", return_value=mock_publisher):
        await publish_event_sync(
            event_type=EventType.ORDER_CREATED_FT,
            order_id="ORD-001",
            data={"deadline": "2026-06-01"},
        )

    call_args = mock_publisher.publish.call_args
    message_bytes = call_args[0][1]
    message = json.loads(message_bytes.decode())
    assert message["event_type"] == "order_created_ft"
    assert message["order_id"] == "ORD-001"
    assert message["data"]["deadline"] == "2026-06-01"


async def test_publish_failure_returns_empty_string():
    with patch("services.notification.publisher._get_publisher", side_effect=Exception("no pubsub")):
        result = await publish_event(make_event(EventType.ORDER_CREATED_FT))

    assert result == ""
