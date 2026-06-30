"""
Django authentication backend for JWT authentication.

This backend allows Django to authenticate users via JWT tokens.
"""

import logging
from django.contrib.auth.backends import BaseBackend
from django.contrib.auth import get_user_model

from .provider import jwt_provider
from .utils import get_or_create_user_from_jwt

logger = logging.getLogger(__name__)

User = get_user_model()


class JWTAuthBackend(BaseBackend):
    """
    Django authentication backend for JWT tokens.

    Authentication is delegated to the shared ``jwt_provider`` singleton so the
    JWT config, token manager and blacklist are initialised exactly once.
    """

    def authenticate(self, request, jwt_token=None, **kwargs):
        """
        Authenticate user using JWT token.

        Args:
            request: Django HttpRequest instance
            jwt_token: JWT token string
            **kwargs: Additional arguments

        Returns:
            User instance if authentication successful, None otherwise
        """
        if not jwt_token:
            return None

        try:
            user_data = jwt_provider.validate_token(jwt_token)
            if not user_data:
                logger.debug("Invalid JWT token")
                return None

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
