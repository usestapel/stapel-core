"""
Middleware to handle admin login redirects with proper 'next' parameter.

This middleware intercepts admin page requests for unauthenticated users
and redirects them to the centralized auth service login with a 'next' parameter.
"""

import logging
from django.shortcuts import redirect
from urllib.parse import urlencode
from django.utils.deprecation import MiddlewareMixin

logger = logging.getLogger(__name__)


def _login_url() -> str:
    """Deployment-canonical admin-login URL, derived from the mount registry
    (stapel_core.django.mounts): an external auth service when
    STAPEL_AUTH_SERVICE_PREFIX / STAPEL_MOUNTS declares one, the locally
    mounted admin (reverse-based, mount/script-prefix aware) otherwise."""
    from stapel_core.django.mounts import admin_login_url

    return admin_login_url()


class AdminLoginRedirectMiddleware(MiddlewareMixin):
    """
    Middleware to redirect unauthenticated admin requests to centralized login.

    When a user tries to access an admin page without being authenticated,
    this middleware redirects them to the deployment's admin login (see
    _login_url) with a 'next' parameter pointing to the original URL.
    """

    def process_request(self, request):
        """
        Check if request is for admin page and user is not authenticated.

        Runs AFTER JWTAuthMiddleware, so if tokens were valid the user
        would already be authenticated. If user is still anonymous here,
        redirect to auth login to get fresh tokens.
        """
        # Only handle admin URLs
        if not request.path.endswith('/admin/') and '/admin/' not in request.path:
            return None

        # Skip if user is already authenticated
        if hasattr(request, 'user') and request.user.is_authenticated:
            return None

        login_url = _login_url()

        # Skip if this is already the login page
        if login_url in request.path:
            return None

        # User is not authenticated — redirect to auth login.
        # This covers both "no tokens" and "expired/invalid tokens" cases.
        # Auth login view will reissue JWT tokens before redirecting back.
        next_url = request.get_full_path()

        redirect_url = f"{login_url}?{urlencode({'next': next_url})}"
        logger.info(f"Redirecting unauthenticated admin request to {redirect_url}")

        return redirect(redirect_url)
