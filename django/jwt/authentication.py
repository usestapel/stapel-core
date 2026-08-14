"""
DRF authentication classes for Stapel services.

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
    """Return a raw Redis client, or None when the backend is not django_redis.

    Only django_redis exposes ``.client``, and only through it can we bypass
    Django's cache ``KEY_PREFIX`` so one ban is visible to every service.
    ``None`` is a normal answer, not a failure — callers fall back to the
    Django cache framework, which works on every backend. Exceptions are
    *not* swallowed here: whether an unreachable store means "not banned" or
    "cannot tell" is the caller's decision, and it used to be silently
    answered "not banned".
    """
    from django.core.cache import cache
    if hasattr(cache, 'client'):
        return cache.client.get_client()
    return None


def _blacklist_fail_open() -> bool:
    """Honour the one blacklist escape hatch (shared with ``TokenBlacklist``).

    A deployment that has decided availability outranks revocation sets this
    once and both blacklists read it — a second knob would let the two halves
    of revocation drift apart.
    """
    from django.conf import settings
    return bool(getattr(settings, "STAPEL_BLACKLIST_FAIL_OPEN", False))


def blacklist_user(user_id: str, ttl: int = 7200) -> bool:
    """
    Blacklist a user so all their tokens are rejected.

    Prefers raw Redis so the key is visible across all services regardless of
    Django's cache ``KEY_PREFIX``, and falls back to the Django cache
    framework on any other backend. The fallback matters: with Django's
    default LocMemCache this function used to only log an error, so a ban was
    a permanent no-op and nothing said so. The fallback is scoped by whatever
    ``KEY_PREFIX`` the backend carries, so on a non-django_redis cache a ban
    reaches every service sharing that cache and prefix — an enforced local
    ban beats an unenforceable global one.

    Args:
        user_id: UUID of the user to blacklist
        ttl: Time to live in seconds (default 2h, should be >= access token lifetime)

    Returns:
        True when the ban was stored, False when the store rejected it — a
        caller that ignores the result cannot tell a ban from a no-op.
    """
    key = f'{_USER_BLACKLIST_PREFIX}{user_id}'
    try:
        redis_client = _get_redis_client()
        if redis_client is not None:
            redis_client.setex(key, ttl, '1')
        else:
            from django.core.cache import cache
            cache.set(key, '1', ttl)
    except Exception as e:
        logger.error(f"Cannot blacklist user {user_id}: {e}")
        return False
    logger.info(f"User blacklisted: {user_id} for {ttl}s")
    return True


def unblacklist_user(user_id: str) -> bool:
    """Remove user from blacklist. Returns True when the store accepted it."""
    key = f'{_USER_BLACKLIST_PREFIX}{user_id}'
    try:
        redis_client = _get_redis_client()
        if redis_client is not None:
            redis_client.delete(key)
        else:
            from django.core.cache import cache
            cache.delete(key)
    except Exception as e:
        logger.error(f"Cannot unblacklist user {user_id}: {e}")
        return False
    return True


def is_user_blacklisted(user_id: str) -> bool:
    """Check if a user is blacklisted.

    Fails CLOSED, matching ``stapel_core.core.token_blacklist.TokenBlacklist``:
    with the store unreachable, answering "not banned" resurrects every banned
    session exactly when the system is degraded, and a ban is the one answer an
    operator issues because they cannot wait. Override with
    ``STAPEL_BLACKLIST_FAIL_OPEN`` for availability-over-security deployments.
    """
    key = f'{_USER_BLACKLIST_PREFIX}{user_id}'
    try:
        redis_client = _get_redis_client()
        if redis_client is not None:
            return bool(redis_client.exists(key))
        from django.core.cache import cache
        return bool(cache.get(key))
    except Exception as e:
        logger.error(f"Error checking user blacklist for {user_id}: {e}")
        return not _blacklist_fail_open()


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
