from enum import Enum
from dataclasses import dataclass
from datetime import datetime, timezone


class EventType(str, Enum):
    ORDER_CREATED_FT = "order_created_ft"
    ORDER_CREATED_LT = "order_created_lt"
    QUOTE_SET = "quote_set"
    PAYMENT_CONFIRMED = "payment_confirmed"
    DELIVERY_COMPLETE = "delivery_complete"
    QA_REVIEW_REQUIRED = "qa_review_required"
    PIPELINE_ERROR = "pipeline_error"
    EDITOR_ASSIGNED = "editor_assigned"
    PROOFREADER_ASSIGNED = "proofreader_assigned"
    USER_REGISTERED = "user_registered"
    USER_ENABLED = "user_enabled"
    USER_DISABLED = "user_disabled"
    GT_STAGE_COMPLETE = "gt_stage_complete"


@dataclass
class NotifyEvent:
    event_type: EventType
    order_id: str | None = None
    user_id: str | None = None
    recipient_email: str | None = None
    data: dict | None = None
    timestamp: str = ""


def make_event(
    event_type: EventType,
    order_id: str | None = None,
    user_id: str | None = None,
    recipient_email: str | None = None,
    data: dict | None = None,
) -> NotifyEvent:
    return NotifyEvent(
        event_type=event_type,
        order_id=order_id,
        user_id=user_id,
        recipient_email=recipient_email,
        data=data or {},
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
