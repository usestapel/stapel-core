"""
Middleware to handle admin login redirects with proper 'next' parameter.

This middleware intercepts admin page requests for unauthenticated users
and redirects them to the centralized auth service login with a 'next' parameter.
"""

import logging
from django.shortcuts import redirect
from django.urls import resolve
from urllib.parse import urlencode
from django.utils.deprecation import MiddlewareMixin

logger = logging.getLogger(__name__)


class AdminLoginRedirectMiddleware(MiddlewareMixin):
    """
    Middleware to redirect unauthenticated admin requests to centralized login.
    
    When a user tries to access an admin page without being authenticated,
    this middleware redirects them to /auth/admin/login/ with a 'next' parameter
    pointing to the original requested URL.
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

        # Skip if this is already the login page
        if '/auth/admin/login/' in request.path:
            return None

        # User is not authenticated — redirect to auth login.
        # This covers both "no tokens" and "expired/invalid tokens" cases.
        # Auth login view will reissue JWT tokens before redirecting back.
        login_url = '/auth/admin/login/'
        next_url = request.get_full_path()

        redirect_url = f"{login_url}?{urlencode({'next': next_url})}"
        logger.info(f"Redirecting unauthenticated admin request to {redirect_url}")

        return redirect(redirect_url)