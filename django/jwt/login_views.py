"""
Django views for JWT authentication.

Provides custom login view that generates JWT tokens and sets them as cookies.
"""

import logging
from django.contrib.auth.views import LoginView
from django.contrib.auth import login
from django.conf import settings

from .utils import set_jwt_cookies
from .provider import jwt_provider

logger = logging.getLogger(__name__)


class JWTCookieLoginView(LoginView):
    """
    Custom login view that generates JWT tokens and sets them as HTTP-only cookies.

    This view extends Django's standard LoginView and adds JWT token generation
    after successful authentication.
    """

    template_name = 'admin/login.html'

    def dispatch(self, request, *args, **kwargs):
        """
        Redirect if user is already authenticated AND has admin access.
        Shows login form if user is not staff (to allow switching accounts).
        """
        from django.shortcuts import redirect

        if request.user.is_authenticated:
            # Only redirect if user has admin access (is_staff or is_superuser)
            is_staff = getattr(request.user, 'is_staff', False)
            is_superuser = getattr(request.user, 'is_superuser', False)
            if is_staff or is_superuser:
                next_url = request.GET.get('next', '')
                # Prevent redirect loop - if next is login page, go to admin index
                if not next_url or '/login' in next_url:
                    next_url = '/auth/admin/'
                logger.info(f"Staff user {request.user} redirecting to {next_url}")
                return redirect(next_url)
            else:
                # User is authenticated but not staff - clear JWT cookies and session
                from django.contrib.auth import logout
                logger.info(f"Non-staff user {request.user}, clearing auth and showing login form")
                logout(request)
                # Clear JWT cookies by returning response with deleted cookies
                response = super().dispatch(request, *args, **kwargs)
                cookie_name = getattr(settings, 'JWT_COOKIE_NAME', 'iron_jwt')
                refresh_cookie_name = getattr(settings, 'JWT_REFRESH_COOKIE_NAME', 'iron_refresh_jwt')
                cookie_domain = getattr(settings, 'JWT_COOKIE_DOMAIN', None)
                cookie_samesite = getattr(settings, 'JWT_COOKIE_SAMESITE', 'Lax')
                response.delete_cookie(cookie_name, path='/', domain=cookie_domain, samesite=cookie_samesite)
                response.delete_cookie(refresh_cookie_name, path='/', domain=cookie_domain, samesite=cookie_samesite)
                return response

        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        """
        Handle valid form submission.

        Authenticates user, generates JWT tokens, and sets them as cookies.

        Args:
            form: Django authentication form

        Returns:
            HTTP response with JWT cookies set
        """
        # Get authenticated user
        user = form.get_user()

        # Log the user in (establishes Django session)
        login(self.request, user, backend='django.contrib.auth.backends.ModelBackend')

        try:
            # Use unified JWT provider for token generation
            access_token, refresh_token = jwt_provider.create_tokens(user)

            response = super().form_valid(form)
            set_jwt_cookies(response, access_token, refresh_token)

            return response

        except Exception as e:
            logger.error(f"Error generating JWT tokens: {e}", exc_info=True)
            return super().form_valid(form)
