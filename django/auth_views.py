"""
Django views for JWT authentication operations.

Provides logout, token refresh, and other authentication-related views.
"""

import logging
from datetime import datetime, timezone
from django.http import JsonResponse
from django.views import View
from django.contrib.auth import logout as django_logout
from django.conf import settings

from ..core.token_manager import TokenManager
from ..core.token_blacklist import TokenBlacklist
from .utils import extract_jwt_from_request, load_jwt_config_from_settings, set_jwt_cookies

logger = logging.getLogger(__name__)


class JWTLogoutView(View):
    """
    View to handle JWT logout.
    
    This view:
    1. Blacklists the current JWT tokens
    2. Clears JWT cookies
    3. Logs out Django session
    """
    
    def _get_redis_client(self):
        """Get Redis client from Django cache."""
        try:
            from django.core.cache import cache
            # Try to get Redis client from django-redis
            if hasattr(cache, 'client'):
                return cache.client.get_client()
            return None
        except Exception as e:
            logger.error(f"Error getting Redis client: {e}")
            return None
    
    def post(self, request):
        """Handle POST logout request."""
        # Tell middleware to skip setting new cookies on response
        request._jwt_skip_cookie_update = True

        try:
            # Extract tokens from request
            access_token, refresh_token = extract_jwt_from_request(request)

            # Initialize blacklist
            redis_client = self._get_redis_client()
            blacklist = TokenBlacklist(redis_client)

            # Initialize JWT handler
            config = load_jwt_config_from_settings()
            from ..core.jwt_handler import JWTHandler
            jwt_handler = JWTHandler(config)

            # Blacklist access token if present and not expired
            if access_token:
                payload = jwt_handler.decode_token(access_token, verify=False)
                if payload and 'jti' in payload and 'exp' in payload:
                    expires_in = datetime.fromtimestamp(payload['exp'], tz=timezone.utc) - datetime.now(timezone.utc)
                    if expires_in.total_seconds() > 0:
                        blacklist.blacklist_token(payload['jti'], expires_in)

            # Blacklist refresh token if present and not expired
            if refresh_token:
                payload = jwt_handler.decode_token(refresh_token, verify=False)
                if payload and 'jti' in payload and 'exp' in payload:
                    expires_in = datetime.fromtimestamp(payload['exp'], tz=timezone.utc) - datetime.now(timezone.utc)
                    if expires_in.total_seconds() > 0:
                        blacklist.blacklist_token(payload['jti'], expires_in)

            # Logout Django session
            django_logout(request)

            # Create response with cleared cookies
            response = JsonResponse({
                'status': 'success',
                'message': 'Successfully logged out'
            })

            # Clear JWT cookies
            cookie_name = getattr(settings, 'JWT_COOKIE_NAME', 'iron_jwt')
            refresh_cookie_name = getattr(settings, 'JWT_REFRESH_COOKIE_NAME', 'iron_refresh_jwt')
            cookie_domain = getattr(settings, 'JWT_COOKIE_DOMAIN', None)
            cookie_samesite = getattr(settings, 'JWT_COOKIE_SAMESITE', 'Lax')

            response.delete_cookie(cookie_name, path='/', domain=cookie_domain, samesite=cookie_samesite)
            response.delete_cookie(refresh_cookie_name, path='/', domain=cookie_domain, samesite=cookie_samesite)

            logger.info(f"User logged out successfully")
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
    View to explicitly refresh JWT tokens.
    
    Accepts refresh token and returns new access token.
    """
    
    def post(self, request):
        """Handle POST refresh request."""
        try:
            # Only allow refresh if JWT_REFRESH_ALLOWED is True (should only be in auth service)
            if not getattr(settings, 'JWT_REFRESH_ALLOWED', False):
                return JsonResponse({
                    'status': 'error',
                    'message': 'Token refresh not allowed on this service'
                }, status=403)

            # Extract refresh token from request
            _, refresh_token = extract_jwt_from_request(request)

            if not refresh_token:
                return JsonResponse({
                    'status': 'error',
                    'message': 'No refresh token provided'
                }, status=400)
            
            # Initialize JWT config and token manager
            config = load_jwt_config_from_settings()
            
            token_manager = TokenManager(config)
            
            # Refresh access token (preserves user data from refresh token)
            new_access_token = token_manager.refresh_access_token(refresh_token)
            
            if not new_access_token:
                return JsonResponse({
                    'status': 'error',
                    'message': 'Failed to refresh token'
                }, status=401)
            
            # Create response
            response = JsonResponse({
                'status': 'success',
                'message': 'Token refreshed successfully',
                'access_token': new_access_token
            })
            
            # Set new access token as cookie
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
    View to check JWT token status.
    
    Returns information about current authentication state.
    """
    
    def get(self, request):
        """Handle GET status request."""
        try:
            access_token, refresh_token = extract_jwt_from_request(request)
            
            if not access_token and not refresh_token:
                return JsonResponse({
                    'authenticated': False,
                    'message': 'No tokens found'
                })
            
            # Initialize JWT handler
            config = load_jwt_config_from_settings()
            
            from ..core.jwt_handler import JWTHandler
            jwt_handler = JWTHandler(config)
            
            # Check access token
            access_valid = False
            access_payload = None
            if access_token:
                access_payload = jwt_handler.decode_token(access_token, verify=True)
                access_valid = access_payload is not None
            
            # Check refresh token
            refresh_valid = False
            refresh_payload = None
            if refresh_token:
                refresh_payload = jwt_handler.decode_token(refresh_token, verify=True)
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