"""
Topic naming conventions.

Pattern: ``stapel.{service}.{event-group}``.

Each constant is resolved at import time from a Django setting (if Django is
configured) so deployments can customise the wire values via
``STAPEL_TOPIC_*`` settings.
"""

from __future__ import annotations


def _setting(name: str, default: str) -> str:
    """Read a topic string from Django settings, falling back to *default*."""
    try:
        from django.conf import settings  # noqa: PLC0415
        return getattr(settings, name, default)
    except Exception:  # noqa: BLE001 — Django not configured (e.g. plain import)
        return default


TOPIC_PROFILE_CHANGED = _setting("STAPEL_TOPIC_PROFILE_CHANGED", "stapel.profiles.profile-changed")
TOPIC_NOTIFICATION_REQUESTED = _setting("STAPEL_TOPIC_NOTIFICATION_REQUESTED", "stapel.notifications.requested")
TOPIC_USER_CONTACT_CHANGED = _setting("STAPEL_TOPIC_USER_CONTACT_CHANGED", "stapel.auth.user-contact-changed")
