"""
Token blacklist backed by Django's cache framework.

Uses whatever cache backend is configured (LocMemCache in tests, Redis in
production), but through the SHARED revocation namespace
(``stapel_core.core.revocation_store``) rather than
``django.core.cache.cache`` — otherwise each service's own ``KEY_PREFIX``
isolates the entry and a token revoked in one service stays valid in every
other. See that module for the full account of the defect.
"""
import logging
from datetime import timedelta

from .revocation_store import revocation_cache

logger = logging.getLogger(__name__)


class TokenBlacklist:
    """Token blacklist manager using Django's cache framework."""

    def __init__(self, key_prefix: str = "jwt_blacklist"):
        self.key_prefix = key_prefix

    def _key(self, jti: str) -> str:
        return f"{self.key_prefix}:{jti}"

    def blacklist_token(self, jti: str, expires_in: timedelta) -> bool:
        try:
            cache = revocation_cache()
            cache.set(self._key(jti), "1", int(expires_in.total_seconds()))
            logger.info(f"Token {jti[:8]}... blacklisted for {expires_in.total_seconds()}s")
            return True
        except Exception as e:
            logger.error(f"Error blacklisting token: {e}")
            return False

    def is_blacklisted(self, jti: str) -> bool:
        try:
            cache = revocation_cache()
            return bool(cache.get(self._key(jti)))
        except Exception as e:
            # Fail CLOSED: with the blacklist store down, treating every
            # token as valid would resurrect revoked sessions exactly when
            # the system is degraded. Override only for availability-over-
            # security deployments.
            logger.error(f"Error checking blacklist: {e}")
            from django.conf import settings
            if getattr(settings, "STAPEL_BLACKLIST_FAIL_OPEN", False):
                return False
            return True

    def remove_from_blacklist(self, jti: str) -> bool:
        try:
            cache = revocation_cache()
            cache.delete(self._key(jti))
            return True
        except Exception as e:
            logger.error(f"Error removing from blacklist: {e}")
            return False

    def clear_all(self) -> bool:
        try:
            cache = revocation_cache()
            cache.clear()
            return True
        except Exception as e:
            logger.error(f"Error clearing blacklist: {e}")
            return False
