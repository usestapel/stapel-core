"""
Django middleware for JWT authentication.

Clear flow:
1. Skip paths that don't need auth (health, static, login/logout pages)
2. Extract tokens from cookies/header
3. Check blacklist - reject if either token is blacklisted
4. Validate access token
5. If valid -> authenticate user
6. If invalid/expired AND refresh allowed -> try refresh
7. Set new cookies in response if refreshed
"""

import logging
from django.db import OperationalError
from django.http import HttpResponse
from django.utils.deprecation import MiddlewareMixin
from django.contrib.auth import login
from django.conf import settings

from .utils import (
    get_or_create_user_from_jwt,
    extract_jwt_from_request,
    set_jwt_cookies,
)
from .provider import jwt_provider

logger = logging.getLogger(__name__)


class JWTAuthMiddleware(MiddlewareMixin):
    """
    Django middleware for JWT authentication.

    Simple responsibilities:
    1. Extract tokens
    2. Check blacklist
    3. Validate tokens
    4. Refresh if needed AND allowed
    5. Authenticate user
    """

    # Paths to skip entirely - no auth processing at all
    SKIP_PATHS = (
        '/api/metrics/',
        '/metrics/',
        '/health/',
        '/healthz/',
        '/ready/',
        '/readyz/',
    )
    SKIP_PREFIXES = (
        '/static/',
        '/staticfiles/',
        '/media/',
    )
    SKIP_CONTAINS = (
        '/login/',
        '/logout/',
        '/schema/',
        '/swagger/',
        '/redoc/',
        '/docs/',
    )

    def __init__(self, get_response):
        super().__init__(get_response)
        # Use unified JWT provider - single source of truth
        logger.info("JWTAuthMiddleware initialized using jwt_provider")

    def _should_skip(self, path: str) -> bool:
        """Check if this path should skip JWT processing."""
        if any(path.endswith(p) for p in self.SKIP_PATHS):
            return True
        if any(path.startswith(p) for p in self.SKIP_PREFIXES):
            return True
        if any(p in path for p in self.SKIP_CONTAINS):
            return True
        return False

    def process_request(self, request):
        """
        Process incoming request for JWT authentication.

        Flow:
        1. Skip if path doesn't need auth
        2. Extract tokens
        3. Check blacklist -> reject if blacklisted
        4. Validate access token -> authenticate if valid
        5. If invalid, try refresh (if allowed)
        """
        path = request.path

        # Step 1: Skip paths that don't need auth
        if self._should_skip(path):
            return None

        # Prevent recursion
        if getattr(request, '_jwt_processing', False):
            return None
        request._jwt_processing = True

        try:
            return self._authenticate(request)
        except OperationalError:
            logger.error("Database unavailable during JWT authentication", exc_info=True)
            return HttpResponse("Service temporarily unavailable", status=503)
        finally:
            request._jwt_processing = False

    def _authenticate(self, request):
        """Main authentication logic."""
        # Step 2: Extract tokens
        access_token, refresh_token = extract_jwt_from_request(request)

        if not access_token and not refresh_token:
            return None  # No tokens, skip auth

        # Step 3: Check blacklist for BOTH tokens
        if access_token and jwt_provider.is_blacklisted(access_token):
            logger.debug("Blacklisted access token rejected")
            request._jwt_clear_cookies = True
            return None

        if refresh_token and jwt_provider.is_blacklisted(refresh_token):
            logger.debug("Blacklisted refresh token rejected")
            request._jwt_clear_cookies = True
            return None

        # Step 4: Try to validate access token
        user_data = None
        if access_token:
            user_data = jwt_provider.validate_token(access_token)

        # Step 5: If access token valid, authenticate user
        if user_data:
            # Check user-level blacklist
            from .authentication import is_user_blacklisted
            uid = user_data.get('user_id')
            if uid and is_user_blacklisted(uid):
                logger.warning(f"User blacklisted in middleware: {uid}")
                request._jwt_clear_cookies = True
                return None

            self._login_user(request, user_data)

            # Check if we should proactively refresh (only if allowed)
            if self._refresh_allowed() and refresh_token and access_token:
                if jwt_provider.manager.is_near_expiry(access_token):
                    self._do_refresh(request, refresh_token)
            return None

        # Step 6: Access token invalid/expired, try refresh
        if refresh_token and self._refresh_allowed():
            new_access_token = jwt_provider.refresh_access_token(refresh_token)
            if new_access_token:
                # Validate new token to get user data
                user_data = jwt_provider.validate_token(new_access_token)
                if user_data:
                    # Check user-level blacklist for refreshed token too
                    from .authentication import is_user_blacklisted as _is_bl
                    uid = user_data.get('user_id')
                    if uid and _is_bl(uid):
                        logger.warning(f"User blacklisted in middleware (refresh): {uid}")
                        request._jwt_clear_cookies = True
                        return None
                    self._login_user(request, user_data)
                    request._jwt_refreshed = True
                    request._new_access_token = new_access_token
                    logger.info(f"Token refreshed for {user_data.get('email')}")
                    return None

        # No valid tokens
        logger.debug("JWT authentication failed - no valid tokens")
        return None

    def _refresh_allowed(self) -> bool:
        """Check if token refresh is allowed on this service."""
        return getattr(settings, 'JWT_REFRESH_ALLOWED', False)

    def _can_manage_cookies(self) -> bool:
        """Check if this service can set/clear JWT cookies. Only auth service should manage cookies."""
        return getattr(settings, 'JWT_REFRESH_ALLOWED', False)

    def _do_refresh(self, request, refresh_token: str):
        """Proactively refresh access token."""
        new_access_token = jwt_provider.refresh_access_token(refresh_token)
        if new_access_token:
            request._jwt_refreshed = True
            request._new_access_token = new_access_token
            logger.debug("Proactive token refresh")

    def _login_user(self, request, user_data: dict):
        """Get or create user and establish Django session."""
        user = get_or_create_user_from_jwt(user_data)
        if not user:
            request._jwt_clear_cookies = True
            return

        # Skip login() if user is already authenticated via session —
        # login() calls rotate_token() which breaks CSRF for admin forms.
        if hasattr(request, 'user') and request.user.is_authenticated and request.user.pk == user.pk:
            return

        # Preserve CSRF secret across login() — login() calls rotate_token()
        # which changes the CSRF secret, breaking admin forms that were loaded
        # with the old token (e.g. when session was invalidated by another service
        # sharing the same session cookie name).
        csrf_cookie = request.META.get("CSRF_COOKIE")
        csrf_needs_update = request.META.get("CSRF_COOKIE_NEEDS_UPDATE", False)

        login(request, user, backend='stapel_core.django.jwt.session.EmailAuthBackend')

        # Restore CSRF state if there was an existing cookie.
        # If no prior cookie existed (first visit), keep the new token from login().
        if csrf_cookie is not None:
            request.META["CSRF_COOKIE"] = csrf_cookie
            request.META["CSRF_COOKIE_NEEDS_UPDATE"] = csrf_needs_update

    def process_response(self, request, response):
        """
        Process response to set refreshed JWT cookies.

        Args:
            request: Django HttpRequest instance
            response: Django HttpResponse instance

        Returns:
            Modified response with JWT cookies
        """
        # Only auth service should manage cookies
        if not self._can_manage_cookies():
            return response

        # Check if we need to clear stale cookies (user not found in DB)
        if getattr(request, '_jwt_clear_cookies', False):
            # Clear JWT cookies to force re-login (must match all attributes used when setting)
            config = jwt_provider.config
            logger.debug(f"Clearing JWT cookies for {request.path}")
            response.delete_cookie(config.cookie_name, path='/', domain=config.cookie_domain, samesite=config.cookie_samesite)
            response.delete_cookie(config.refresh_cookie_name, path='/', domain=config.cookie_domain, samesite=config.cookie_samesite)
            return response

        # Check if logout requested skipping cookie updates
        if getattr(request, '_jwt_skip_cookie_update', False):
            logger.debug("Skipping JWT cookie update (logout in progress)")
            return response

        # Check if token was refreshed during request processing
        if hasattr(request, '_jwt_refreshed') and request._jwt_refreshed:
            new_access_token = getattr(request, '_new_access_token', None)
            if new_access_token:
                # Set new access token as cookie
                set_jwt_cookies(response, new_access_token)
                logger.debug("Set refreshed JWT token in response cookies")

        return response


class CsrfExemptAPIMiddleware(MiddlewareMixin):
    """
    Middleware to exempt API endpoints from CSRF verification.

    Since API endpoints use JWT authentication (not session-based),
    CSRF protection is not needed and would interfere with cross-origin
    requests from the frontend.

    Exempts any path containing '/api/'.
    """

    def process_request(self, request):
        """Mark API requests as CSRF exempt."""
        if '/api/' in request.path:
            setattr(request, '_dont_enforce_csrf_checks', True)
        return None

    def process_response(self, request, response):
        """Remove CSRF/session cookies from API responses to avoid interfering with admin sessions."""
        if '/api/' in request.path:
            response.cookies.pop(settings.CSRF_COOKIE_NAME, None)
            response.cookies.pop(settings.SESSION_COOKIE_NAME, None)
        return response


class ServiceAPIKeyMiddleware(MiddlewareMixin):
    """
    Middleware to authenticate internal service-to-service requests via X-API-KEY.
    Marks the request as service-originated when the key matches configured values.
    Supports a single shared key (`SERVICE_API_KEY`) or a mapping (`SERVICE_API_KEYS`).
    """

    header_name = "HTTP_X_API_KEY"

    def process_request(self, request):
        import hmac

        api_key = request.META.get(self.header_name)
        if not api_key:
            return None

        # Prefer single shared key (constant-time compare)
        shared_key = getattr(settings, "SERVICE_API_KEY", None)
        if shared_key and hmac.compare_digest(api_key, shared_key):
            request.is_service_request = True
            request.service_name = "internal"
            return None

        service_keys = getattr(settings, "SERVICE_API_KEYS", {})
        for service_name, key in service_keys.items():
            if key and hmac.compare_digest(api_key, key):
                request.is_service_request = True
                request.service_name = service_name
                return None

        logger.warning("Invalid service API key attempt")
        return None
