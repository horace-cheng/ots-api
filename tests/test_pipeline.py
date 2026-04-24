import pytest
from unittest.mock import MagicMock, patch
from services.pipeline import trigger_pipeline


async def test_trigger_returns_message_id():
    mock_publisher = MagicMock()
    mock_future = MagicMock()
    mock_future.result.return_value = "msg-id-001"
    mock_publisher.publish.return_value = mock_future
    mock_publisher.topic_path.return_value = "projects/test/topics/test-topic"

    with patch("services.pipeline._get_publisher", return_value=mock_publisher):
        result = await trigger_pipeline("ORDER-001")

    assert result == "msg-id-001"
    mock_publisher.publish.assert_called_once()


async def test_trigger_failure_returns_empty_string_not_raises():
    with patch("services.pipeline._get_publisher", side_effect=Exception("no pubsub")):
        result = await trigger_pipeline("ORDER-001")

    assert result == ""


async def test_trigger_publishes_order_id_in_message():
    import json

    mock_publisher = MagicMock()
    mock_future = MagicMock()
    mock_future.result.return_value = "msg-001"
    mock_publisher.publish.return_value = mock_future
    mock_publisher.topic_path.return_value = "projects/test/topics/topic"

    with patch("services.pipeline._get_publisher", return_value=mock_publisher):
        await trigger_pipeline("ORDER-XYZ")

    call_args = mock_publisher.publish.call_args
    message_bytes = call_args[0][1]  # second positional arg is the message data
    message = json.loads(message_bytes.decode())
    assert message["order_id"] == "ORDER-XYZ"
    assert message["source"] == "payment_confirmed"
