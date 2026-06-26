"""
Publish notification requests to the bus.
"""

import logging

from stapel_core.bus import Event, publish
from stapel_core.kafka.events import EventType
from stapel_core.kafka.topics import TOPIC_NOTIFICATION_REQUESTED

logger = logging.getLogger(__name__)


def request_notification(
    notification_type: str,
    user_id: str = None,
    variables: dict = None,
    email: str = None,
    phone: str = None,
    language: str = None,
    source_service: str = "",
) -> None:
    """
    Publish a notification request to the bus.

    Args:
        notification_type: Type of notification (e.g. 'otp_code', 'new_message')
        user_id: Target user UUID (for registered users)
        variables: Template variables (e.g. {'code': '1234', 'expiry_minutes': 10})
        email: Direct email (for unauthenticated flows like OTP)
        phone: Direct phone (for unauthenticated flows like OTP)
        language: Language from accept-language header of the originating request
        source_service: Name of the calling service (for tracing)
    """
    if not (user_id or email or phone):
        logger.error("request_notification called without user_id, email, or phone")
        return

    payload = {
        "notification_type": notification_type,
        "user_id": user_id,
        "email": email,
        "phone": phone,
        "language": language,
        "variables": variables or {},
    }

    publish(
        TOPIC_NOTIFICATION_REQUESTED,
        Event(
            event_type=EventType.NOTIFICATION_REQUESTED,
            service=source_service or "unknown",
            payload=payload,
            key=user_id or email or phone,
        ),
    )
