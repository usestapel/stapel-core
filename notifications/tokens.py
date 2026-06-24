"""
HMAC-signed tokens for one-click email unsubscribe (RFC 8058).
"""

import hashlib
import hmac
import time

from django.conf import settings


def _get_key() -> bytes:
    key = getattr(settings, 'NOTIFICATION_UNSUBSCRIBE_KEY', None) or settings.SECRET_KEY
    return key.encode('utf-8') if isinstance(key, str) else key


def generate_unsubscribe_token(user_id: str, group: str, channel: str) -> str:
    """
    Generate an HMAC-signed unsubscribe token.

    Format: {user_id}:{group}:{channel}:{timestamp}:{signature}
    """
    if ':' in group or ':' in channel:
        raise ValueError("group and channel must not contain ':'")
    ts = str(int(time.time()))
    msg = f"{user_id}:{group}:{channel}:{ts}"
    sig = hmac.new(_get_key(), msg.encode('utf-8'), hashlib.sha256).hexdigest()[:32]
    return f"{user_id}:{group}:{channel}:{ts}:{sig}"


def verify_unsubscribe_token(token: str, max_age: int = 30 * 24 * 3600) -> dict | None:
    """
    Verify and decode an unsubscribe token.

    Args:
        token: The token string
        max_age: Maximum token age in seconds (default: 30 days)

    Returns:
        Dict with user_id, group, channel if valid; None otherwise.
    """
    try:
        parts = token.split(':')
        if len(parts) != 5:
            return None

        user_id, group, channel, ts, sig = parts
        msg = f"{user_id}:{group}:{channel}:{ts}"
        expected = hmac.new(_get_key(), msg.encode('utf-8'), hashlib.sha256).hexdigest()[:32]

        if not hmac.compare_digest(sig, expected):
            return None

        if max_age and (time.time() - int(ts)) > max_age:
            return None

        return {"user_id": user_id, "group": group, "channel": channel}
    except (ValueError, TypeError):
        return None
