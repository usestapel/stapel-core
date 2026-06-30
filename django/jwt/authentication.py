"""
DRF authentication classes for Iron services.

These classes integrate with the JWT middleware to provide authentication
for DRF views and Swagger documentation.
"""

import logging
from rest_framework import authentication

logger = logging.getLogger(__name__)

# User-level blacklist key prefix — uses raw Redis to bypass Django KEY_PREFIX,
# so the blacklist works across all services (auth, catalog, profiles, etc.)
_USER_BLACKLIST_PREFIX = 'user_blacklisted:'


def _get_redis_client():
    """Get raw Redis client, bypassing Django cache KEY_PREFIX."""
    try:
        from django.core.cache import cache
        if hasattr(cache, 'client'):
            return cache.client.get_client()
    except Exception as e:
        logger.error(f"Error getting Redis client for user blacklist: {e}")
    return None


def blacklist_user(user_id: str, ttl: int = 7200):
    """
    Blacklist a user so all their tokens are rejected.

    Uses raw Redis to ensure the key is visible across all services
    regardless of Django cache KEY_PREFIX.

    Args:
        user_id: UUID of the user to blacklist
        ttl: Time to live in seconds (default 2h, should be >= access token lifetime)
    """
    redis_client = _get_redis_client()
    if redis_client:
        redis_client.setex(f'{_USER_BLACKLIST_PREFIX}{user_id}', ttl, '1')
        logger.info(f"User blacklisted: {user_id} for {ttl}s")
    else:
        logger.error(f"Cannot blacklist user {user_id}: Redis client unavailable")


def unblacklist_user(user_id: str):
    """Remove user from blacklist."""
    redis_client = _get_redis_client()
    if redis_client:
        redis_client.delete(f'{_USER_BLACKLIST_PREFIX}{user_id}')


def is_user_blacklisted(user_id: str) -> bool:
    """Check if a user is blacklisted."""
    redis_client = _get_redis_client()
    if redis_client:
        return bool(redis_client.exists(f'{_USER_BLACKLIST_PREFIX}{user_id}'))
    return False


class JWTCookieAuthentication(authentication.BaseAuthentication):
    """
    DRF authentication class that uses JWT from cookies.

    Uses unified jwt_provider for all JWT operations.

    Usage:
        In settings.py:
        REST_FRAMEWORK = {
            'DEFAULT_AUTHENTICATION_CLASSES': [
                'stapel_core.django.jwt.authentication.JWTCookieAuthentication',
            ],
        }
    """

    def authenticate(self, request):
        """
        Authenticate the request using JWT from cookies.

        Args:
            request: Django request object

        Returns:
            tuple: (user, None) if authenticated, None otherwise
        """
        from .utils import extract_jwt_from_request, get_or_create_user_from_jwt
        from .provider import jwt_provider

        # Extract JWT from cookies
        access_token, _ = extract_jwt_from_request(request)

        if not access_token:
            return None

        # Extract metadata for debugging
        user_agent = request.headers.get('user-agent', 'unknown')
        client_ip = self._get_client_ip(request)
        token_suffix = access_token[-10:] if len(access_token) >= 10 else 'short_token'
        path = request.path

        try:
            # Check if token is blacklisted
            if jwt_provider.is_blacklisted(access_token):
                logger.warning(
                    f"JWT Auth Failed - Blacklisted token - "
                    f"token_suffix={token_suffix}, "
                    f"client_ip={client_ip}, "
                    f"user_agent={user_agent}, "
                    f"path={path}"
                )
                return None

            # Validate and get user data from token
            user_data = jwt_provider.validate_token(access_token)

            if not user_data:
                logger.warning(
                    f"JWT Auth Failed - Invalid token - "
                    f"token_suffix={token_suffix}, "
                    f"client_ip={client_ip}, "
                    f"user_agent={user_agent}, "
                    f"path={path}"
                )
                return None

            # Check if user is banned (user-level blacklist)
            user_id = user_data.get('user_id')
            if user_id and is_user_blacklisted(user_id):
                logger.warning(
                    f"JWT Auth Failed - User blacklisted - "
                    f"user_id={user_id}, "
                    f"token_suffix={token_suffix}, "
                    f"path={path}"
                )
                return None

            # Get or create user from JWT data
            user = get_or_create_user_from_jwt(user_data)

            if not user:
                logger.error(
                    f"JWT Auth Failed - User creation failed - "
                    f"user_id={user_data.get('user_id', 'unknown')}, "
                    f"token_suffix={token_suffix}, "
                    f"client_ip={client_ip}, "
                    f"path={path}"
                )
                return None

            return (user, None)

        except Exception as e:
            logger.error(
                f"JWT Auth Failed - Exception - "
                f"error_type={type(e).__name__}, "
                f"error_msg={str(e)}, "
                f"token_suffix={token_suffix}, "
                f"client_ip={client_ip}, "
                f"user_agent={user_agent}, "
                f"path={path}",
                exc_info=True
            )
            return None

    def _get_client_ip(self, request):
        """Extract client IP from request, handling proxies"""
        x_forwarded_for = request.headers.get('x-forwarded-for')
        if x_forwarded_for:
            return x_forwarded_for.split(',')[0].strip()
        return request.META.get('REMOTE_ADDR', 'unknown')

    def authenticate_header(self, request):
        """
        Return the WWW-Authenticate header value.

        This is shown in 401 responses to indicate the authentication scheme.
        Uses ASCII-only characters to comply with ISO-8859-1 encoding requirement.
        """
        return 'Bearer'
