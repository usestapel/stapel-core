"""
Unified JWT Provider for all Iron services.

This module provides a SINGLE source of truth for JWT operations.
All other code should use this module instead of creating JWTConfig/TokenManager directly.

Usage:
    from stapel_core.django.jwt_provider import jwt_provider

    # Create tokens
    access, refresh = jwt_provider.create_tokens(user)

    # Validate token
    payload = jwt_provider.validate_token(token)

    # Get raw handler/manager if needed
    handler = jwt_provider.handler
    manager = jwt_provider.manager
"""

import logging
from typing import Optional, Tuple, Dict, Any

logger = logging.getLogger(__name__)


class JWTProvider:
    """
    Singleton-style JWT provider that lazily initializes JWT components.

    All JWT operations should go through this class to ensure consistency.
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def _ensure_initialized(self):
        """Lazy initialization to avoid import-time Django settings access."""
        if self._initialized:
            return

        from .utils import load_jwt_config_from_settings
        from ..core.token_manager import TokenManager
        from ..core.jwt_handler import JWTHandler

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
        """Initialize token blacklist with Redis client."""
        from ..core.token_blacklist import TokenBlacklist
        try:
            from django.core.cache import cache
            redis_client = None
            if hasattr(cache, 'client'):
                redis_client = cache.client.get_client()
            return TokenBlacklist(redis_client)
        except Exception as e:
            logger.warning(f"Could not initialize token blacklist: {e}")
            return TokenBlacklist(None)

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

    def validate_token(self, token: str) -> Optional[Dict[str, Any]]:
        """
        Validate an access token and return payload.

        Args:
            token: JWT token string

        Returns:
            Token payload dict or None if invalid
        """
        self._ensure_initialized()
        return self._manager.validate_access_token(token)

    def refresh_access_token(self, refresh_token: str, load_user_data=None) -> Optional[str]:
        """
        Refresh an access token using refresh token.

        Args:
            refresh_token: JWT refresh token
            load_user_data: Optional callback(user_id) -> user_data dict.
                           If provided, loads fresh user data from database
                           to include updated claims in new token.

        Returns:
            New access token or None if refresh failed
        """
        self._ensure_initialized()
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
