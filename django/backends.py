"""
Django authentication backend for JWT authentication.

This backend allows Django to authenticate users via JWT tokens.
"""

import logging
from django.contrib.auth.backends import BaseBackend
from django.contrib.auth import get_user_model

from ..core.token_manager import TokenManager
from .utils import get_or_create_user_from_jwt

logger = logging.getLogger(__name__)

User = get_user_model()


class JWTAuthBackend(BaseBackend):
    """
    Django authentication backend for JWT tokens.

    This backend can authenticate users based on JWT tokens
    and is compatible with Django's authentication system.
    """

    def authenticate(self, request, jwt_token=None, **kwargs):
        """
        Authenticate user using JWT token.

        Args:
            request: Django HttpRequest instance
            jwt_token: JWT token string
            **kwargs: Additional keyword arguments

        Returns:
            User instance if authentication successful, None otherwise
        """
        if not jwt_token:
            return None

        try:
            # Load JWT configuration from settings
            from .utils import load_jwt_config_from_settings

            config = load_jwt_config_from_settings()

            # Create token manager
            token_manager = TokenManager(config)

            # Validate token and extract user data
            user_data = token_manager.validate_access_token(jwt_token)
            if not user_data:
                logger.debug("Invalid JWT token")
                return None

            # Get or create user from JWT data
            user = get_or_create_user_from_jwt(user_data)
            if not user:
                logger.error("Failed to get/create user from JWT")
                return None
            return user

        except Exception as e:
            logger.error(f"Error in JWTAuthBackend: {e}", exc_info=True)
            return None

    def get_user(self, user_id):
        """
        Get user by ID.

        Required by Django authentication system.

        Args:
            user_id: User primary key

        Returns:
            User instance or None
        """
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None
