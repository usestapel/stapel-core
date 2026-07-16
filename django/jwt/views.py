"""
Django views for JWT authentication operations.

All JWT operations route through the shared ``jwt_provider`` singleton so that
configuration, the token manager and the blacklist are initialised exactly once.
"""

import logging

from django.conf import settings
from django.contrib.auth import logout as django_logout
from django.http import JsonResponse
from django.views import View

from .provider import jwt_provider
from .utils import extract_jwt_from_request, set_jwt_cookies

logger = logging.getLogger(__name__)


class JWTLogoutView(View):
    """
    Handle JWT logout.

    Blacklists the current access/refresh tokens (via the provider's blacklist,
    backed by Django's cache), clears the JWT cookies, and logs out the Django
    session.
    """

    def post(self, request):
        """Handle POST logout request."""
        # Tell middleware to skip setting new cookies on response
        request._jwt_skip_cookie_update = True

        try:
            access_token, refresh_token = extract_jwt_from_request(request)

            # Blacklist both tokens if they are present and not yet expired.
            for token in (access_token, refresh_token):
                if token:
                    jwt_provider.blacklist_token(token)

            django_logout(request)

            response = JsonResponse({
                'status': 'success',
                'message': 'Successfully logged out'
            })

            # Clear JWT cookies
            cookie_name = getattr(settings, 'JWT_COOKIE_NAME', 'stapel_jwt')
            refresh_cookie_name = getattr(settings, 'JWT_REFRESH_COOKIE_NAME', 'stapel_refresh_jwt')
            cookie_domain = getattr(settings, 'JWT_COOKIE_DOMAIN', None)
            cookie_samesite = getattr(settings, 'JWT_COOKIE_SAMESITE', 'Lax')

            response.delete_cookie(cookie_name, path='/', domain=cookie_domain, samesite=cookie_samesite)
            response.delete_cookie(refresh_cookie_name, path='/', domain=cookie_domain, samesite=cookie_samesite)

            logger.info("User logged out successfully")
            return response

        except Exception as e:
            logger.error(f"Error during logout: {e}", exc_info=True)
            return JsonResponse({
                'status': 'error',
                'message': 'Logout failed'
            }, status=500)

    def get(self, request):
        """Handle GET logout request (for compatibility)."""
        return self.post(request)


class JWTRefreshView(View):
    """
    Explicitly refresh JWT tokens.

    Accepts a refresh token and returns a new access token.
    """

    def post(self, request):
        """Handle POST refresh request."""
        try:
            # Only allow refresh if JWT_REFRESH_ALLOWED is True (auth service only).
            if not getattr(settings, 'JWT_REFRESH_ALLOWED', False):
                return JsonResponse({
                    'status': 'error',
                    'message': 'Token refresh not allowed on this service'
                }, status=403)

            _, refresh_token = extract_jwt_from_request(request)

            if not refresh_token:
                return JsonResponse({
                    'status': 'error',
                    'message': 'No refresh token provided'
                }, status=400)

            # Refresh via the provider (preserves user data from the refresh token).
            new_access_token = jwt_provider.refresh_access_token(refresh_token)

            if not new_access_token:
                return JsonResponse({
                    'status': 'error',
                    'message': 'Failed to refresh token'
                }, status=401)

            response = JsonResponse({
                'status': 'success',
                'message': 'Token refreshed successfully',
                'access_token': new_access_token
            })

            set_jwt_cookies(response, new_access_token)

            logger.info("Token refreshed successfully")
            return response

        except Exception as e:
            logger.error(f"Error during token refresh: {e}", exc_info=True)
            return JsonResponse({
                'status': 'error',
                'message': 'Token refresh failed'
            }, status=500)


class JWTStatusView(View):
    """
    Check JWT token status.

    Returns the current authentication state, token validity, and the
    presented user profile (``profile``) — the latter built through the
    swappable ``USERS_PROFILE_PRESENTER`` (``stapel_core.django.swappable``),
    so a host that config-swaps the presenter changes this endpoint's
    profile payload without forking core. The flat ``user`` block is the
    legacy auth-identity shape, kept for wire compatibility.
    """

    @staticmethod
    def _presented_profile(user):
        """The active (possibly host-swapped) profile DTO as a dict, or None.

        Reference consumer of the §55 get_presenter() canon: never imports
        UserProfilePresenter directly — resolution goes through the
        STAPEL_SWAP registry, so ``STAPEL_SWAP["USERS_PROFILE_PRESENTER"]``
        reaches this call site (the exact thing a direct import would
        silently break — SWAP001).
        """
        if not user.is_authenticated:
            return None
        import dataclasses

        from stapel_core.django.users.presenters import get_user_profile_presenter

        presenter = get_user_profile_presenter()
        return dataclasses.asdict(presenter.present(user))

    def get(self, request):
        """Handle GET status request."""
        try:
            access_token, refresh_token = extract_jwt_from_request(request)

            if not access_token and not refresh_token:
                return JsonResponse({
                    'authenticated': False,
                    'message': 'No tokens found'
                })

            handler = jwt_provider.handler

            access_valid = False
            access_payload = None
            if access_token:
                access_payload = handler.decode_token(access_token, verify=True)
                access_valid = access_payload is not None

            refresh_valid = False
            refresh_payload = None
            if refresh_token:
                refresh_payload = handler.decode_token(refresh_token, verify=True)
                refresh_valid = refresh_payload is not None

            return JsonResponse({
                'authenticated': request.user.is_authenticated,
                'user': {
                    'user_id': str(request.user.id) if request.user.is_authenticated else None,
                    'email': request.user.email if request.user.is_authenticated else None,
                    'username': request.user.username if request.user.is_authenticated else None,
                    'is_staff': request.user.is_staff if request.user.is_authenticated else False,
                    'is_superuser': request.user.is_superuser if request.user.is_authenticated else False,
                },
                'profile': self._presented_profile(request.user),
                'tokens': {
                    'access_token_valid': access_valid,
                    'refresh_token_valid': refresh_valid,
                    'access_token_exp': access_payload.get('exp') if access_payload else None,
                    'refresh_token_exp': refresh_payload.get('exp') if refresh_payload else None,
                }
            })

        except Exception as e:
            logger.error(f"Error checking status: {e}", exc_info=True)
            return JsonResponse({
                'status': 'error',
                'message': 'Status check failed'
            }, status=500)
