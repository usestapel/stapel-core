"""
Topic naming conventions.

Pattern: iron.{service}.{event-group}
"""

TOPIC_PROFILE_CHANGED = "iron.profiles.profile-changed"
TOPIC_NOTIFICATION_REQUESTED = "iron.notifications.requested"
TOPIC_USER_CONTACT_CHANGED = "iron.auth.user-contact-changed"
TOPIC_TRANSLATIONS_CHANGED = "iron.translate.translations-changed"

# Dead letter queue prefix
DLQ_PREFIX = "iron.dlq"


def dlq_topic(topic: str) -> str:
    """Get the dead letter queue topic for a given topic."""
    return f"{DLQ_PREFIX}.{topic}"
