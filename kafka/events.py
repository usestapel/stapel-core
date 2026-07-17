"""
Event type constants.

The envelope dataclass is :class:`stapel_core.bus.Event`.
"""


class EventType:
    """Event type constants."""
    PROFILE_CHANGED = "profile.changed"
    NOTIFICATION_REQUESTED = "notification.requested"
    USER_CONTACT_CHANGED = "user.contact.changed"
