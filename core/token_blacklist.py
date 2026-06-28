"""
Token blacklist backed by Django's cache framework.

Uses whatever cache backend is configured (LocMemCache in tests, Redis in production).
No raw Redis client needed — the Django cache layer handles backend selection.
"""
import logging
from datetime import timedelta

logger = logging.getLogger(__name__)


class TokenBlacklist:
    """Token blacklist manager using Django's cache framework."""

    def __init__(self, key_prefix: str = "jwt_blacklist"):
        self.key_prefix = key_prefix

    def _key(self, jti: str) -> str:
        return f"{self.key_prefix}:{jti}"

    def blacklist_token(self, jti: str, expires_in: timedelta) -> bool:
        from django.core.cache import cache
        try:
            cache.set(self._key(jti), "1", int(expires_in.total_seconds()))
            logger.info(f"Token {jti[:8]}... blacklisted for {expires_in.total_seconds()}s")
            return True
        except Exception as e:
            logger.error(f"Error blacklisting token: {e}")
            return False

    def is_blacklisted(self, jti: str) -> bool:
        from django.core.cache import cache
        try:
            return bool(cache.get(self._key(jti)))
        except Exception as e:
            logger.error(f"Error checking blacklist: {e}")
            return False

    def remove_from_blacklist(self, jti: str) -> bool:
        from django.core.cache import cache
        try:
            cache.delete(self._key(jti))
            return True
        except Exception as e:
            logger.error(f"Error removing from blacklist: {e}")
            return False

    def clear_all(self) -> bool:
        from django.core.cache import cache
        try:
            cache.clear()
            return True
        except Exception as e:
            logger.error(f"Error clearing blacklist: {e}")
            return False
