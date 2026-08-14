"""
Token manager - clean separation of concerns.

Provides:
- Token creation (access + refresh pair)
- Token validation (separate from refresh)
- Token refresh (explicit, not automatic)
- Blacklist checking

The CALLER (middleware) decides WHEN to refresh. TokenManager just provides tools.
"""

import logging
from typing import Dict, Any, Optional, Tuple, Callable, TYPE_CHECKING

from .jwt_handler import JWTHandler
from .config import JWTConfig

if TYPE_CHECKING:
    from .token_blacklist import TokenBlacklist


logger = logging.getLogger(__name__)


class TokenManager:
    """
    Token manager with clean separation of concerns.

    Responsibilities:
    - Create tokens
    - Validate tokens (check signature, expiry, type)
    - Check blacklist
    - Refresh tokens (when explicitly asked)

    NOT responsible for:
    - Deciding when to refresh (that's the caller's job)
    - Skip paths or other request-level logic
    """

    def __init__(self, config: JWTConfig, blacklist: Optional['TokenBlacklist'] = None):
        """
        Initialize token manager.

        Args:
            config: JWTConfig instance with JWT settings
            blacklist: Optional TokenBlacklist for checking revoked tokens
        """
        self.config = config
        self.jwt_handler = JWTHandler(config)
        self.blacklist = blacklist

    # =========================================================================
    # Token Creation
    # =========================================================================

    def create_tokens(self, user_data: Dict[str, Any]) -> Tuple[str, str]:
        """
        Create new access and refresh tokens for user.

        Args:
            user_data: User information to encode in tokens

        Returns:
            Tuple of (access_token, refresh_token)
        """
        return self.jwt_handler.generate_token_pair(user_data)

    def create_access_token(self, user_data: Dict[str, Any]) -> str:
        """
        Create new access token only.

        Args:
            user_data: User information to encode in token

        Returns:
            Access token string
        """
        return self.jwt_handler.generate_access_token(user_data)

    # =========================================================================
    # Blacklist Operations
    # =========================================================================

    def is_blacklisted(self, jti: str) -> bool:
        """
        Check if token JTI is blacklisted.

        Args:
            jti: Token identifier from JWT payload

        Returns:
            True if blacklisted, False otherwise
        """
        if not self.blacklist:
            return False
        return self.blacklist.is_blacklisted(jti)

    def get_token_jti(self, token: str) -> Optional[str]:
        """
        Extract JTI from token without full validation.

        Args:
            token: JWT token string

        Returns:
            JTI string or None if not found
        """
        payload = self.jwt_handler.decode_token(token, verify=False)
        if payload:
            return payload.get('jti')
        return None

    # =========================================================================
    # Token Validation (pure validation, no refresh)
    # =========================================================================

    def _revoked(self, payload: Dict[str, Any]) -> bool:
        """Is the token this *verified* payload came from revoked?

        Revocation is part of validity, not a separate courtesy check: a
        manager that holds a blacklist and does not run it means every caller
        who reads ``validate_*`` as "is this token good?" admits revoked
        tokens. The jti is taken from the SIGNATURE-VERIFIED payload, never
        from an unverified decode, so an attacker cannot pick which jti gets
        looked up.
        """
        if not self.blacklist:
            return False
        jti = payload.get('jti')
        if not jti:
            # A token with no jti can never be revoked individually. Say so
            # once instead of failing open in silence.
            logger.debug("Token carries no jti; revocation cannot be checked")
            return False
        if self.blacklist.is_blacklisted(jti):
            logger.debug("Revoked token rejected (jti=%s...)", str(jti)[:8])
            return True
        return False

    def validate_access_token(self, access_token: str) -> Optional[Dict[str, Any]]:
        """
        Validate access token and return user data.

        Validation means: correct type, valid signature, not expired, and NOT
        REVOKED — when this manager was built with a blacklist, a blacklisted
        jti is rejected here. Callers do not have to remember a second step;
        forgetting it was how revoked tokens kept authenticating.

        This method still does NOT refresh tokens or decide what happens next.

        Args:
            access_token: JWT access token string

        Returns:
            User data dictionary or None if invalid/expired/revoked
        """
        if not self.jwt_handler.validate_token_type(access_token, "access"):
            logger.debug("Token is not an access token")
            return None

        payload = self.jwt_handler.decode_token(access_token, verify=True)
        if not payload:
            return None

        if self._revoked(payload):
            return None

        return self.jwt_handler.extract_user_data(payload)

    def validate_refresh_token(self, refresh_token: str) -> Optional[Dict[str, Any]]:
        """
        Validate refresh token and return user data.

        As with :meth:`validate_access_token`, a blacklisted jti is refused
        here when this manager holds a blacklist.

        Args:
            refresh_token: JWT refresh token string

        Returns:
            User data dictionary or None if invalid or revoked
        """
        if not self.jwt_handler.validate_token_type(refresh_token, "refresh"):
            logger.debug("Token is not a refresh token")
            return None

        payload = self.jwt_handler.decode_token(refresh_token, verify=True)
        if not payload:
            return None

        if self._revoked(payload):
            return None

        return self.jwt_handler.extract_user_data(payload)

    # =========================================================================
    # Token State Checks
    # =========================================================================

    def is_near_expiry(self, access_token: str) -> bool:
        """
        Check if access token is near expiry.

        Args:
            access_token: JWT access token string

        Returns:
            True if token is near expiry, False otherwise
        """
        return self.jwt_handler.is_token_near_expiry(access_token)

    # =========================================================================
    # Token Refresh (explicit, not automatic)
    # =========================================================================

    def refresh_access_token(
        self,
        refresh_token: str,
        load_user_data: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None
    ) -> Optional[str]:
        """
        Generate new access token using refresh token.

        The refresh token goes through :meth:`validate_refresh_token`, which
        refuses a revoked one. The old "caller MUST check the blacklist first"
        contract is gone: a contract whose violation is silent admission of a
        revoked token is not a contract, it is a hole with a comment.

        Args:
            refresh_token: Refresh token; revoked ones are refused here
            load_user_data: Optional callback(user_id) -> user_data dict.
                           If provided, loads fresh user data from database
                           instead of using data from refresh token.

        Returns:
            New access token or None if refresh token is invalid
        """
        # Validate refresh token
        user_data = self.validate_refresh_token(refresh_token)
        if not user_data:
            logger.debug("Invalid refresh token")
            return None

        # Optionally load fresh user data from database
        if load_user_data:
            user_id = user_data.get('user_id')
            if user_id:
                fresh_data = load_user_data(user_id)
                if fresh_data:
                    user_data = fresh_data
                else:
                    logger.warning(f"User not found for refresh token: {user_id}")
                    return None

        # Generate new access token with user data
        return self.jwt_handler.generate_access_token(user_data)
