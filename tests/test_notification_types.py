from services.notification.types import EventType, NotifyEvent, make_event


def test_event_type_values():
    assert EventType.ORDER_CREATED_FT.value == "order_created_ft"
    assert EventType.ORDER_CREATED_LT.value == "order_created_lt"
    assert EventType.QUOTE_SET.value == "quote_set"
    assert EventType.PAYMENT_CONFIRMED.value == "payment_confirmed"
    assert EventType.DELIVERY_COMPLETE.value == "delivery_complete"
    assert EventType.QA_REVIEW_REQUIRED.value == "qa_review_required"
    assert EventType.PIPELINE_ERROR.value == "pipeline_error"
    assert EventType.EDITOR_ASSIGNED.value == "editor_assigned"
    assert EventType.PROOFREADER_ASSIGNED.value == "proofreader_assigned"
    assert EventType.USER_REGISTERED.value == "user_registered"
    assert EventType.USER_ENABLED.value == "user_enabled"
    assert EventType.USER_DISABLED.value == "user_disabled"


def test_make_event():
    event = make_event(
        EventType.USER_REGISTERED,
        user_id="user-123",
        recipient_email="test@example.com",
        data={"user_email": "test@example.com"},
    )
    assert event.event_type == EventType.USER_REGISTERED
    assert event.user_id == "user-123"
    assert event.recipient_email == "test@example.com"
    assert event.data == {"user_email": "test@example.com"}
    assert event.timestamp


def test_make_event_minimal():
    event = make_event(EventType.ORDER_CREATED_FT, order_id="ord-001")
    assert event.event_type == EventType.ORDER_CREATED_FT
    assert event.order_id == "ord-001"
    assert event.data == {}
