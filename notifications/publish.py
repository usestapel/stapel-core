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
    content_html: str = None,
    content_text: str = None,
) -> bool:
    """
    Publish a notification request to the bus.

    Returns True if the event was queued, False on error.
    Raises ValueError on a malformed request (see schema
    ``notifications/schemas/emits/notification.requested.json``): this edge
    validates the payload *shape* only — whether ``notification_type`` is
    actually registered is validated on the notifications side (consumer +
    ``manage.py check_notifications`` lint), because stapel-core cannot
    import the notifications type registry.

    Args:
        notification_type: Type of notification (e.g. 'otp_code', 'new_message')
        user_id: Target user UUID (for registered users)
        variables: Template variables (e.g. {'code': '1234', 'expiry_minutes': 10})
        email: Direct email (for unauthenticated flows like OTP)
        phone: Direct phone (for unauthenticated flows like OTP)
        language: Language from accept-language header of the originating request
        source_service: Name of the calling service (for tracing)
        content_html: Raw HTML body (escape hatch: rendered inside the base
            brand layout instead of a registered per-type template; allows
            unregistered types)
        content_text: Raw plain-text body (escape hatch, as above; also used
            for push/SMS fallback text)
    """
    if not notification_type or not isinstance(notification_type, str):
        raise ValueError(
            "request_notification requires a non-empty notification_type "
            "string (pass content_html/content_text to send an ad-hoc body "
            "for an unregistered type)"
        )
    for arg_name, arg in (("content_html", content_html), ("content_text", content_text)):
        if arg is not None and not isinstance(arg, str):
            raise ValueError(f"request_notification: {arg_name} must be a string or None")

    if not (user_id or email or phone):
        logger.error("request_notification called without user_id, email, or phone")
        return False

    payload = {
        "notification_type": notification_type,
        "user_id": user_id,
        "email": email,
        "phone": phone,
        "language": language,
        "variables": variables or {},
    }
    if content_html is not None:
        payload["content_html"] = content_html
    if content_text is not None:
        payload["content_text"] = content_text

    try:
        publish(
            TOPIC_NOTIFICATION_REQUESTED,
            Event(
                event_type=EventType.NOTIFICATION_REQUESTED,
                service=source_service or "unknown",
                payload=payload,
                key=user_id or email or phone,
            ),
        )
        return True
    except Exception:
        logger.exception("request_notification failed")
        return False
