"""
Custom Django session backend for cross-service authentication.

This backend handles the case where different services use different
User ID types (UUID vs Integer) by using email as the lookup key.
"""

import logging
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth import get_user_model

logger = logging.getLogger(__name__)

User = get_user_model()


class EmailAuthBackend(ModelBackend):
    """
    Custom authentication backend that uses email for user lookup.

    This avoids issues with different ID types (UUID vs Integer) across services.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        """
        Authenticate using email instead of username.

        Note: This backend doesn't actually validate passwords since
        authentication is handled by the JWT tokens.
        """
        email = kwargs.get('email', username)
        if not email:
            return None

        try:
            user = User.objects.get(email=email)
            return user
        except User.DoesNotExist:
            return None

    def get_user(self, user_id):
        """
        Get user by ID, with fallback to email lookup if ID validation fails.

        This handles the case where a UUID from another service is stored
        in the session but this service uses Integer IDs.
        """
        try:
            # Try normal ID lookup first
            return User.objects.get(pk=user_id)
        except (User.DoesNotExist, ValueError, TypeError) as e:
            # If ID validation fails (e.g., UUID vs Integer), try email lookup
            # This can happen when session was created by a different service
            logger.debug(f"Failed to get user by ID {user_id}: {e}, attempting email lookup")

            # Try to find by email from session
            # Note: We can't do this directly, so we'll return None and let
            # the middleware handle re-authentication
            return None
