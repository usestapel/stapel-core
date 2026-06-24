"""
Token blacklist implementation using Redis.

Provides functionality to blacklist JWT tokens (for logout) and check if tokens are revoked.
"""

import logging
from typing import Optional
from datetime import timedelta

logger = logging.getLogger(__name__)


class TokenBlacklist:
    """
    Token blacklist manager using Redis for distributed storage.
    
    This allows tokens to be revoked across all services in the system.
    """
    
    def __init__(self, redis_client=None, key_prefix: str = "jwt_blacklist"):
        """
        Initialize token blacklist.
        
        Args:
            redis_client: Redis client instance (can be None for testing)
            key_prefix: Prefix for Redis keys
        """
        self.redis_client = redis_client
        self.key_prefix = key_prefix
        self._enabled = redis_client is not None
    
    def blacklist_token(self, jti: str, expires_in: timedelta) -> bool:
        """
        Add token to blacklist.
        
        Args:
            jti: Token identifier (jti claim from JWT)
            expires_in: Time until token naturally expires
        
        Returns:
            True if successfully blacklisted, False otherwise
        """
        if not self._enabled:
            logger.warning("Token blacklist not enabled (no Redis client)")
            return False
        
        try:
            key = f"{self.key_prefix}:{jti}"
            # Set with TTL matching token expiration
            # After token expires naturally, no need to keep it in blacklist
            self.redis_client.setex(
                key,
                int(expires_in.total_seconds()),
                "1"
            )
            logger.info(f"Token {jti[:8]}... blacklisted for {expires_in.total_seconds()}s")
            return True
        except Exception as e:
            logger.error(f"Error blacklisting token: {e}")
            return False
    
    def is_blacklisted(self, jti: str) -> bool:
        """
        Check if token is blacklisted.
        
        Args:
            jti: Token identifier (jti claim from JWT)
        
        Returns:
            True if token is blacklisted, False otherwise
        """
        if not self._enabled:
            return False
        
        try:
            key = f"{self.key_prefix}:{jti}"
            return bool(self.redis_client.exists(key))
        except Exception as e:
            logger.error(f"Error checking blacklist: {e}")
            # Fail open: if Redis is down, don't block valid tokens
            return False
    
    def remove_from_blacklist(self, jti: str) -> bool:
        """
        Remove token from blacklist (rarely needed).
        
        Args:
            jti: Token identifier (jti claim from JWT)
        
        Returns:
            True if successfully removed, False otherwise
        """
        if not self._enabled:
            return False
        
        try:
            key = f"{self.key_prefix}:{jti}"
            self.redis_client.delete(key)
            logger.info(f"Token {jti[:8]}... removed from blacklist")
            return True
        except Exception as e:
            logger.error(f"Error removing from blacklist: {e}")
            return False
    
    def clear_all(self) -> bool:
        """
        Clear all blacklisted tokens (use with caution).
        
        Returns:
            True if successfully cleared, False otherwise
        """
        if not self._enabled:
            return False
        
        try:
            pattern = f"{self.key_prefix}:*"
            keys = self.redis_client.keys(pattern)
            if keys:
                self.redis_client.delete(*keys)
                logger.warning(f"Cleared {len(keys)} blacklisted tokens")
            return True
        except Exception as e:
            logger.error(f"Error clearing blacklist: {e}")
            return False