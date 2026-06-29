import pytest
from unittest.mock import patch, MagicMock
from django.test import RequestFactory, override_settings
from django.http import HttpResponse

from stapel_core.django.jwt.middleware import (
    JWTAuthMiddleware,
    CsrfExemptAPIMiddleware,
    ServiceAPIKeyMiddleware,
)

_factory = RequestFactory()

_USER_DATA = {
    'user_id': 'user-123',
    'email': 'u@test.com',
    'is_staff': False,
    'is_superuser': False,
}


def _get_response(req):
    return HttpResponse()


def _req(path='/api/data/', cookies=None, **meta_extra):
    req = _factory.get(path)
    req.COOKIES = cookies or {}
    req.META.update(meta_extra)
    return req


def _mock_jwt(
    is_blacklisted=False,
    user_data=None,
    refresh_result=None,
    near_expiry=False,
):
    mock = MagicMock()
    mock.is_blacklisted.return_value = is_blacklisted
    mock.validate_token.return_value = user_data
    mock.refresh_access_token.return_value = refresh_result
    mock.manager.is_near_expiry.return_value = near_expiry
    # config used in process_response cookie clearing
    mock.config.cookie_name = "iron_jwt"
    mock.config.refresh_cookie_name = "iron_refresh_jwt"
    mock.config.cookie_domain = None
    mock.config.cookie_samesite = "Lax"
    return mock


# ---------------------------------------------------------------------------
# _should_skip
# ---------------------------------------------------------------------------

class TestShouldSkip:
    m = JWTAuthMiddleware(_get_response)

    @pytest.mark.parametrize("path", [
        '/health/', '/healthz/', '/ready/', '/readyz/', '/api/metrics/', '/metrics/',
    ])
    def test_exact_skip_paths(self, path):
        assert self.m._should_skip(path)

    @pytest.mark.parametrize("path", [
        '/static/js/app.js', '/staticfiles/css/main.css', '/media/images/photo.jpg',
    ])
    def test_prefix_skip_paths(self, path):
        assert self.m._should_skip(path)

    @pytest.mark.parametrize("path", [
        '/api/auth/login/', '/api/auth/logout/', '/api/schema/', '/api/swagger/',
        '/api/redoc/', '/api/docs/',
    ])
    def test_contains_skip_paths(self, path):
        assert self.m._should_skip(path)

    @pytest.mark.parametrize("path", [
        '/api/data/', '/api/users/', '/admin/', '/',
    ])
    def test_normal_paths_not_skipped(self, path):
        assert not self.m._should_skip(path)


# ---------------------------------------------------------------------------
# JWTAuthMiddleware.process_request
# ---------------------------------------------------------------------------

class TestJWTAuthMiddlewareProcessRequest:
    middleware = JWTAuthMiddleware(_get_response)

    def test_skip_path_returns_immediately(self):
        with patch('stapel_core.django.jwt.middleware.jwt_provider') as mock_jwt:
            req = _req('/health/')
            result = self.middleware.process_request(req)
        assert result is None
        mock_jwt.is_blacklisted.assert_not_called()

    def test_no_tokens_returns_none(self):
        with patch('stapel_core.django.jwt.middleware.jwt_provider') as mock_jwt:
            req = _req()
            result = self.middleware.process_request(req)
        assert result is None
        mock_jwt.is_blacklisted.assert_not_called()

    def test_recursion_guard(self):
        with patch('stapel_core.django.jwt.middleware.jwt_provider') as mock_jwt:
            req = _req()
            req._jwt_processing = True
            result = self.middleware.process_request(req)
        assert result is None
        mock_jwt.is_blacklisted.assert_not_called()

    def test_blacklisted_access_token_clears_cookies(self):
        mock_jwt = _mock_jwt(is_blacklisted=True)
        with patch('stapel_core.django.jwt.middleware.jwt_provider', mock_jwt):
            req = _req(cookies={'iron_jwt': 'bad.token'})
            result = self.middleware.process_request(req)
        assert result is None
        assert getattr(req, '_jwt_clear_cookies', False) is True

    def test_blacklisted_refresh_token_clears_cookies(self):
        mock_jwt = _mock_jwt()
        # access_token not blacklisted, but refresh is
        def _is_blacklisted(token):
            return token == 'bad.refresh'
        mock_jwt.is_blacklisted.side_effect = _is_blacklisted
        with patch('stapel_core.django.jwt.middleware.jwt_provider', mock_jwt):
            req = _req(cookies={'iron_jwt': 'good.access', 'iron_refresh_jwt': 'bad.refresh'})
            result = self.middleware.process_request(req)
        assert result is None
        assert getattr(req, '_jwt_clear_cookies', False) is True

    def test_valid_access_token_logs_in_user(self):
        mock_jwt = _mock_jwt(user_data=_USER_DATA)
        user = MagicMock(pk='user-123', is_authenticated=False)
        with (
            patch('stapel_core.django.jwt.middleware.jwt_provider', mock_jwt),
            patch('stapel_core.django.jwt.middleware.get_or_create_user_from_jwt', return_value=user),
            patch('stapel_core.django.jwt.middleware.login') as mock_login,
            patch('stapel_core.django.jwt.authentication.is_user_blacklisted', return_value=False),
        ):
            req = _req(cookies={'iron_jwt': 'valid.token'})
            req.user = MagicMock(is_authenticated=False)
            result = self.middleware.process_request(req)
        assert result is None
        mock_login.assert_called_once()

    def test_already_authenticated_same_user_skips_login(self):
        mock_jwt = _mock_jwt(user_data=_USER_DATA)
        user = MagicMock(pk='user-123')
        with (
            patch('stapel_core.django.jwt.middleware.jwt_provider', mock_jwt),
            patch('stapel_core.django.jwt.middleware.get_or_create_user_from_jwt', return_value=user),
            patch('stapel_core.django.jwt.middleware.login') as mock_login,
            patch('stapel_core.django.jwt.authentication.is_user_blacklisted', return_value=False),
        ):
            req = _req(cookies={'iron_jwt': 'valid.token'})
            req.user = MagicMock(is_authenticated=True, pk='user-123')
            result = self.middleware.process_request(req)
        assert result is None
        mock_login.assert_not_called()

    def test_user_not_found_clears_cookies(self):
        mock_jwt = _mock_jwt(user_data=_USER_DATA)
        with (
            patch('stapel_core.django.jwt.middleware.jwt_provider', mock_jwt),
            patch('stapel_core.django.jwt.middleware.get_or_create_user_from_jwt', return_value=None),
            patch('stapel_core.django.jwt.authentication.is_user_blacklisted', return_value=False),
        ):
            req = _req(cookies={'iron_jwt': 'valid.token'})
            req.user = MagicMock(is_authenticated=False)
            self.middleware.process_request(req)
        assert getattr(req, '_jwt_clear_cookies', False) is True

    def test_user_level_blacklisted_clears_cookies(self):
        mock_jwt = _mock_jwt(user_data=_USER_DATA)
        with (
            patch('stapel_core.django.jwt.middleware.jwt_provider', mock_jwt),
            patch('stapel_core.django.jwt.authentication.is_user_blacklisted', return_value=True),
        ):
            req = _req(cookies={'iron_jwt': 'valid.token'})
            req.user = MagicMock(is_authenticated=False)
            self.middleware.process_request(req)
        assert getattr(req, '_jwt_clear_cookies', False) is True

    @override_settings(JWT_REFRESH_ALLOWED=False)
    def test_invalid_token_no_refresh_allowed(self):
        mock_jwt = _mock_jwt(user_data=None)
        with patch('stapel_core.django.jwt.middleware.jwt_provider', mock_jwt):
            req = _req(cookies={'iron_jwt': 'expired.token', 'iron_refresh_jwt': 'ref.tok'})
            result = self.middleware.process_request(req)
        assert result is None
        mock_jwt.refresh_access_token.assert_not_called()

    @override_settings(JWT_REFRESH_ALLOWED=True)
    def test_invalid_token_refresh_allowed_succeeds(self):
        mock_jwt = _mock_jwt(user_data=None, refresh_result='new.access.token')
        # After refresh, validate_token should return user_data for the new token
        mock_jwt.validate_token.side_effect = [None, _USER_DATA]  # first call None, second call user_data
        user = MagicMock(pk='user-123', is_authenticated=False)
        with (
            patch('stapel_core.django.jwt.middleware.jwt_provider', mock_jwt),
            patch('stapel_core.django.jwt.middleware.get_or_create_user_from_jwt', return_value=user),
            patch('stapel_core.django.jwt.middleware.login'),
            patch('stapel_core.django.jwt.authentication.is_user_blacklisted', return_value=False),
        ):
            req = _req(cookies={'iron_jwt': 'expired.token', 'iron_refresh_jwt': 'ref.tok'})
            req.user = MagicMock(is_authenticated=False)
            self.middleware.process_request(req)
        assert getattr(req, '_jwt_refreshed', False) is True
        assert getattr(req, '_new_access_token', None) == 'new.access.token'

    @override_settings(JWT_REFRESH_ALLOWED=True)
    def test_refresh_fails_no_new_access_token(self):
        mock_jwt = _mock_jwt(user_data=None, refresh_result=None)
        with patch('stapel_core.django.jwt.middleware.jwt_provider', mock_jwt):
            req = _req(cookies={'iron_jwt': 'expired.token', 'iron_refresh_jwt': 'ref.tok'})
            req.user = MagicMock(is_authenticated=False)
            result = self.middleware.process_request(req)
        assert result is None
        assert not getattr(req, '_jwt_refreshed', False)

    @override_settings(JWT_REFRESH_ALLOWED=True)
    def test_valid_token_near_expiry_triggers_proactive_refresh(self):
        mock_jwt = _mock_jwt(user_data=_USER_DATA, near_expiry=True, refresh_result='refreshed.tok')
        user = MagicMock(pk='user-123', is_authenticated=False)
        with (
            patch('stapel_core.django.jwt.middleware.jwt_provider', mock_jwt),
            patch('stapel_core.django.jwt.middleware.get_or_create_user_from_jwt', return_value=user),
            patch('stapel_core.django.jwt.middleware.login'),
            patch('stapel_core.django.jwt.authentication.is_user_blacklisted', return_value=False),
        ):
            req = _req(cookies={'iron_jwt': 'near.expiry.token', 'iron_refresh_jwt': 'ref.tok'})
            req.user = MagicMock(is_authenticated=False)
            self.middleware.process_request(req)
        assert getattr(req, '_jwt_refreshed', False) is True

    def test_operational_error_returns_503(self):
        from django.db import OperationalError
        mock_jwt = _mock_jwt(user_data=_USER_DATA)
        with (
            patch('stapel_core.django.jwt.middleware.jwt_provider', mock_jwt),
            patch('stapel_core.django.jwt.middleware.get_or_create_user_from_jwt', side_effect=OperationalError()),
            patch('stapel_core.django.jwt.authentication.is_user_blacklisted', return_value=False),
        ):
            req = _req(cookies={'iron_jwt': 'valid.token'})
            req.user = MagicMock(is_authenticated=False)
            result = self.middleware.process_request(req)
        assert result is not None
        assert result.status_code == 503


# ---------------------------------------------------------------------------
# JWTAuthMiddleware.process_response
# ---------------------------------------------------------------------------

class TestJWTAuthMiddlewareProcessResponse:
    middleware = JWTAuthMiddleware(_get_response)

    @override_settings(JWT_REFRESH_ALLOWED=False)
    def test_no_cookie_management_when_not_allowed(self):
        req = _req()
        req._jwt_clear_cookies = True  # would normally clear, but not allowed
        response = HttpResponse()
        result = self.middleware.process_response(req, response)
        assert result is response  # unchanged

    @override_settings(JWT_REFRESH_ALLOWED=True)
    def test_clears_cookies_when_flagged(self):
        mock_jwt = _mock_jwt()
        with patch('stapel_core.django.jwt.middleware.jwt_provider', mock_jwt):
            req = _req()
            req._jwt_clear_cookies = True
            response = HttpResponse()
            result = self.middleware.process_response(req, response)
        assert "iron_jwt" in result.cookies or result.cookies.get("iron_jwt", None) is not None
        # Cookie deletion sets max_age=0 or expires in the past
        # Django sets delete_cookie by setting max_age=0

    @override_settings(JWT_REFRESH_ALLOWED=True)
    def test_skip_cookie_update_when_flagged(self):
        mock_jwt = _mock_jwt()
        with patch('stapel_core.django.jwt.middleware.jwt_provider', mock_jwt):
            req = _req()
            req._jwt_skip_cookie_update = True
            req._jwt_refreshed = True
            req._new_access_token = 'new.tok'
            response = HttpResponse()
            result = self.middleware.process_response(req, response)
        # set_jwt_cookies should NOT have been called
        assert 'iron_jwt' not in result.cookies

    @override_settings(JWT_REFRESH_ALLOWED=True)
    def test_sets_cookie_when_refreshed(self):
        mock_jwt = _mock_jwt()
        with (
            patch('stapel_core.django.jwt.middleware.jwt_provider', mock_jwt),
            patch('stapel_core.django.jwt.middleware.set_jwt_cookies') as mock_set,
        ):
            req = _req()
            req._jwt_refreshed = True
            req._new_access_token = 'new.access.tok'
            response = HttpResponse()
            self.middleware.process_response(req, response)
        mock_set.assert_called_once_with(response, 'new.access.tok')

    @override_settings(JWT_REFRESH_ALLOWED=True)
    def test_passthrough_when_no_flags(self):
        mock_jwt = _mock_jwt()
        with (
            patch('stapel_core.django.jwt.middleware.jwt_provider', mock_jwt),
            patch('stapel_core.django.jwt.middleware.set_jwt_cookies') as mock_set,
        ):
            req = _req()
            response = HttpResponse()
            result = self.middleware.process_response(req, response)
        assert result is response
        mock_set.assert_not_called()


# ---------------------------------------------------------------------------
# CsrfExemptAPIMiddleware
# ---------------------------------------------------------------------------

class TestCsrfExemptAPIMiddleware:
    middleware = CsrfExemptAPIMiddleware(_get_response)

    def test_api_path_marked_csrf_exempt(self):
        req = _req('/api/users/')
        self.middleware.process_request(req)
        assert getattr(req, '_dont_enforce_csrf_checks', False) is True

    def test_non_api_path_not_marked(self):
        req = _req('/admin/dashboard/')
        self.middleware.process_request(req)
        assert not getattr(req, '_dont_enforce_csrf_checks', False)

    @override_settings(
        CSRF_COOKIE_NAME='csrftoken',
        SESSION_COOKIE_NAME='sessionid',
    )
    def test_api_response_removes_csrf_cookie(self):
        req = _req('/api/data/')
        response = HttpResponse()
        response.cookies['csrftoken'] = 'csrf-value'
        response.cookies['sessionid'] = 'session-value'
        result = self.middleware.process_response(req, response)
        assert 'csrftoken' not in result.cookies
        assert 'sessionid' not in result.cookies

    @override_settings(
        CSRF_COOKIE_NAME='csrftoken',
        SESSION_COOKIE_NAME='sessionid',
    )
    def test_non_api_response_keeps_cookies(self):
        req = _req('/admin/dashboard/')
        response = HttpResponse()
        response.cookies['csrftoken'] = 'csrf-value'
        response.cookies['sessionid'] = 'session-value'
        result = self.middleware.process_response(req, response)
        assert 'csrftoken' in result.cookies
        assert 'sessionid' in result.cookies

    def test_process_request_returns_none(self):
        req = _req('/api/anything/')
        assert self.middleware.process_request(req) is None


# ---------------------------------------------------------------------------
# ServiceAPIKeyMiddleware
# ---------------------------------------------------------------------------

class TestServiceAPIKeyMiddleware:
    middleware = ServiceAPIKeyMiddleware(_get_response)

    def test_no_api_key_header_returns_none(self):
        req = _req()
        result = self.middleware.process_request(req)
        assert result is None
        assert not getattr(req, 'is_service_request', False)

    @override_settings(SERVICE_API_KEY='shared-secret-key')
    def test_valid_shared_key_marks_service_request(self):
        req = _req(HTTP_X_API_KEY='shared-secret-key')
        self.middleware.process_request(req)
        assert req.is_service_request is True
        assert req.service_name == 'internal'

    @override_settings(SERVICE_API_KEY='shared-secret-key')
    def test_invalid_shared_key_does_not_mark(self):
        req = _req(HTTP_X_API_KEY='wrong-key')
        self.middleware.process_request(req)
        assert not getattr(req, 'is_service_request', False)

    @override_settings(SERVICE_API_KEYS={'billing': 'billing-key', 'cdn': 'cdn-key'})
    def test_valid_per_service_key(self):
        req = _req(HTTP_X_API_KEY='billing-key')
        self.middleware.process_request(req)
        assert req.is_service_request is True
        assert req.service_name == 'billing'

    @override_settings(SERVICE_API_KEYS={'billing': 'billing-key'})
    def test_wrong_per_service_key_does_not_mark(self):
        req = _req(HTTP_X_API_KEY='wrong-key')
        self.middleware.process_request(req)
        assert not getattr(req, 'is_service_request', False)

    @override_settings(SERVICE_API_KEY='shared-key', SERVICE_API_KEYS={'svc': 'svc-key'})
    def test_shared_key_takes_priority_over_per_service(self):
        req = _req(HTTP_X_API_KEY='shared-key')
        self.middleware.process_request(req)
        assert req.service_name == 'internal'

    @override_settings(SERVICE_API_KEY=None, SERVICE_API_KEYS={'svc': 'svc-key'})
    def test_falls_through_to_per_service_when_no_shared_key(self):
        req = _req(HTTP_X_API_KEY='svc-key')
        self.middleware.process_request(req)
        assert req.is_service_request is True
        assert req.service_name == 'svc'

    def test_process_request_always_returns_none(self):
        req = _req(HTTP_X_API_KEY='some-key')
        result = self.middleware.process_request(req)
        assert result is None
