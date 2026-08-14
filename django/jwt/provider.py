"""
Unified JWT Provider for all Stapel services.

This module provides a SINGLE source of truth for JWT operations.
All other code should use this module instead of creating JWTConfig/TokenManager directly.

Usage:
    from stapel_core.django.jwt.provider import jwt_provider

    # Create tokens
    access, refresh = jwt_provider.create_tokens(user)

    # Validate token
    payload = jwt_provider.validate_token(token)

    # Get raw handler/manager if needed
    handler = jwt_provider.handler
    manager = jwt_provider.manager
"""

import logging
import threading
from typing import Optional, Tuple, Dict, Any

logger = logging.getLogger(__name__)


class JWTProvider:
    """
    Singleton-style JWT provider that lazily initializes JWT components.

    All JWT operations should go through this class to ensure consistency.
    """

    _instance = None
    _init_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def _ensure_initialized(self):
        """Lazy initialization to avoid import-time Django settings access.

        Uses double-checked locking so two concurrent first-request threads
        don't each build their own handler/manager (cf. bus/router.get_bus).
        """
        if self._initialized:
            return

        with self._init_lock:
            if self._initialized:
                return

            from .utils import load_jwt_config_from_settings
            from stapel_core.core.token_manager import TokenManager
            from stapel_core.core.jwt_handler import JWTHandler

            self._config = load_jwt_config_from_settings()
            self._blacklist = self._init_blacklist()
            self._handler = JWTHandler(self._config)
            self._manager = TokenManager(self._config, blacklist=self._blacklist)
            self._initialized = True

            logger.info(
                f"JWTProvider initialized: algorithm={self._config.algorithm}, "
                f"can_sign={self._config.can_sign()}, can_verify={self._config.can_verify()}"
            )

    def _init_blacklist(self):
        """Initialize token blacklist.

        TokenBlacklist is backed by Django's cache framework (LocMem in tests,
        Redis in production via django-redis) — no raw Redis client is needed.
        """
        from stapel_core.core.token_blacklist import TokenBlacklist
        try:
            return TokenBlacklist()
        except Exception as e:
            logger.warning(f"Could not initialize token blacklist: {e}")
            return TokenBlacklist()

    @property
    def config(self):
        """Get JWT configuration."""
        self._ensure_initialized()
        return self._config

    @property
    def handler(self):
        """Get JWTHandler instance."""
        self._ensure_initialized()
        return self._handler

    @property
    def manager(self):
        """Get TokenManager instance."""
        self._ensure_initialized()
        return self._manager

    def create_tokens(self, user) -> Tuple[str, str]:
        """
        Create access and refresh tokens for a Django user.

        Args:
            user: Django User instance

        Returns:
            Tuple of (access_token, refresh_token)
        """
        self._ensure_initialized()
        from .utils import serialize_user_to_jwt_data
        user_data = serialize_user_to_jwt_data(user)
        return self._manager.create_tokens(user_data)

    def create_tokens_from_data(self, user_data: Dict[str, Any]) -> Tuple[str, str]:
        """
        Create access and refresh tokens from user data dict.

        Args:
            user_data: Dictionary with user information

        Returns:
            Tuple of (access_token, refresh_token)
        """
        self._ensure_initialized()
        return self._manager.create_tokens(user_data)

    def _user_banned(self, user_data: Optional[Dict[str, Any]]) -> bool:
        """Is the user this token speaks for banned?

        Per-user revocation ("log this person out everywhere", "ban this
        account") is a django-layer concept — it reads a django cache and has
        no place in the framework-free TokenManager — so the django layer is
        where it is enforced, on the seam every django caller shares. Imported
        inside the call so tests (and the middleware) patch one name.
        """
        if not user_data:
            return False
        from .authentication import is_user_blacklisted

        user_id = user_data.get('user_id')
        if user_id and is_user_blacklisted(user_id):
            logger.warning("Token rejected: user %s is blacklisted", user_id)
            return True
        return False

    def validate_token(self, token: str) -> Optional[Dict[str, Any]]:
        """
        Validate an access token and return payload.

        "Valid" includes "not revoked": the manager refuses a blacklisted jti
        and this method additionally refuses a token belonging to a banned
        user. Every caller of this method — the DRF authentication class, the
        django auth backend, channels, and any caller written tomorrow —
        therefore gets revocation without opting in. Callers that used to run
        their own blacklist probe still may; it costs one extra cache GET.

        Args:
            token: JWT token string

        Returns:
            Token payload dict or None if invalid, revoked, or user-banned
        """
        self._ensure_initialized()
        user_data = self._manager.validate_access_token(token)
        if self._user_banned(user_data):
            return None
        return user_data

    def refresh_access_token(self, refresh_token: str, load_user_data=None) -> Optional[str]:
        """
        Refresh an access token using refresh token.

        Minting a new access token is the one operation that outlives the
        credential presented for it, so both revocation layers run BEFORE the
        mint: the manager refuses a blacklisted refresh jti, and a banned user
        gets no new token even if their refresh token is otherwise perfect.

        Args:
            refresh_token: JWT refresh token
            load_user_data: Optional callback(user_id) -> user_data dict.
                           If provided, loads fresh user data from database
                           to include updated claims in new token.

        Returns:
            New access token or None if refresh failed, was revoked, or the
            user is banned
        """
        self._ensure_initialized()
        # Validate first so the ban check sees the token's real identity, and
        # so a revoked refresh token never reaches the minting step at all.
        user_data = self._manager.validate_refresh_token(refresh_token)
        if not user_data or self._user_banned(user_data):
            return None
        return self._manager.refresh_access_token(refresh_token, load_user_data)

    def is_blacklisted(self, token: str) -> bool:
        """Check if token is blacklisted."""
        self._ensure_initialized()
        jti = self._manager.get_token_jti(token)
        if jti:
            return self._manager.is_blacklisted(jti)
        return False

    def blacklist_token(self, token: str) -> bool:
        """Add token to blacklist."""
        self._ensure_initialized()
        from datetime import datetime, timezone
        payload = self._handler.decode_token(token, verify=False)
        if payload and 'jti' in payload and 'exp' in payload:
            expires_in = datetime.fromtimestamp(payload['exp'], tz=timezone.utc) - datetime.now(timezone.utc)
            if expires_in.total_seconds() > 0:
                self._blacklist.blacklist_token(payload['jti'], expires_in)
                return True
        return False

    def get_jwks(self) -> Optional[Dict[str, Any]]:
        """Get JWKS for public key verification."""
        self._ensure_initialized()
        return self._handler.get_jwks()

    def reset(self):
        """Reset provider (useful for testing)."""
        self._initialized = False


# Global singleton instance
jwt_provider = JWTProvider()
