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
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from .provider import jwt_provider
from .utils import (
    extract_jwt_from_request,
    jwt_cookie_names,
    load_user_by_uid,
    set_jwt_cookies,
)

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
            cookie_name, refresh_cookie_name = jwt_cookie_names()
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

            # Re-mint from the DATABASE (load_user_by_uid), never from the
            # refresh token's own claims — identical to what the middleware
            # does on its two refresh paths, and for the identical reason: a
            # refresh token lives up to JWT_REFRESH_TOKEN_LIFETIME (7 days by
            # default), so re-minting from its claims resurrects a staff
            # role/flag that was revoked in between, and hands a fresh access
            # token to an account that has since been deactivated or deleted.
            # This endpoint used to be the one refresh path that skipped the
            # loader; the whole point of the seam is that it has no exceptions.
            new_access_token = jwt_provider.refresh_access_token(
                refresh_token, load_user_by_uid
            )

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


class JWTStatusTokensSerializer(serializers.Serializer):
    """The ``tokens`` block of the status payload."""

    access_token_valid = serializers.BooleanField()
    refresh_token_valid = serializers.BooleanField()
    access_token_exp = serializers.IntegerField(allow_null=True)
    refresh_token_exp = serializers.IntegerField(allow_null=True)


class JWTStatusSerializer(serializers.Serializer):
    """The status payload, as the contract describes it.

    Declaration only — the view answers with the dict it always answered
    with, and this says what that dict is. ``profile`` is deliberately an
    open object: its shape is the deployment's, decided by the swappable
    ``USERS_PROFILE_PRESENTER``, and pinning one presenter's fields here
    would describe a payload a host that swapped it does not send.
    """

    authenticated = serializers.BooleanField()
    profile = serializers.DictField(allow_null=True, required=False)
    tokens = JWTStatusTokensSerializer(required=False)
    message = serializers.CharField(
        required=False, help_text="Present when the request carried no tokens."
    )


class JWTStatusView(APIView):
    """
    Check JWT token status.

    Returns the current authentication state, token validity, and the
    presented user profile (``profile``) — the latter built through the
    swappable ``USERS_PROFILE_PRESENTER`` (``stapel_core.django.swappable``),
    so a host that config-swaps the presenter changes this endpoint's
    profile payload without forking core.

    A DRF view, not a plain Django one, for one reason: drf-spectacular emits
    nothing for a plain ``View``, so this route was live and polled while its
    consumer's ``docs/schema.json`` had no entry for it and the contract gate
    passed over a route it could not see (``stapel_core.contract.W001``).

    Two declarations keep the wire exactly where it was:

    * ``authentication_classes = []`` — the deployment's default is usually
      :class:`~stapel_core.django.jwt.authentication.JWTCookieAuthentication`,
      which CREATES the local user row for a token it has not seen. This
      endpoint is the read-only sibling that audit AUTH-02 spared precisely
      because it mints nothing; running the deployment's authenticator here
      would hand it a side effect it has never had. The state it reports is
      the one the JWT middleware already resolved onto the Django request.
    * ``permission_classes = [AllowAny]`` — a plain ``View`` had no gate, and
      a service whose DRF default is ``IsAuthenticated`` would otherwise turn
      "am I logged in?" into 401 for exactly the caller who needs to ask.

    The handlers keep returning ``JsonResponse``, so the bytes, the keys and
    the status codes are the ones the previous implementation sent.
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes: list = []

    def perform_authentication(self, request):
        """Leave ``request.user`` exactly as the middleware left it.

        DRF resolves ``request.user`` eagerly here, and with no authenticator
        that resolution WRITES ``AnonymousUser`` back onto the underlying
        Django request (``Request.user`` has a setter that does). The status
        of the session would then be reported as "not authenticated" for every
        caller — the endpoint's whole answer, inverted. Not authenticating is
        the point of this view; not clobbering is the same decision.
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

    @extend_schema(
        operation_id="jwt_status",
        summary="Current JWT session state",
        description=(
            "Decodes the caller's own access/refresh tokens and reports "
            "whether they are still valid, together with the presented "
            "profile of the authenticated user. Mints nothing and never "
            "refreshes a token."
        ),
        responses={200: JWTStatusSerializer},
        auth=[],
    )
    def get(self, request):
        """Handle GET status request."""
        # The Django request, not DRF's wrapper: `request.user` on the wrapper
        # would run the deployment's authenticators (one of which creates user
        # rows), while the state this endpoint reports is the one the JWT
        # middleware already resolved.
        request = getattr(request, "_request", request)
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
