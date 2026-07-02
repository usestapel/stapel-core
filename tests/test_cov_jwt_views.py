"""Coverage tests for stapel_core.django.jwt.views and login_views."""
import json
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.views import LoginView
from django.http import HttpResponse
from django.test import RequestFactory, override_settings

from stapel_core.django.jwt.login_views import JWTCookieLoginView
from stapel_core.django.jwt.views import JWTLogoutView, JWTRefreshView, JWTStatusView

factory = RequestFactory()

# redirect() resolves URLs against ROOT_URLCONF; conftest leaves it empty ('').
_URLCONF = "test_cov_jwt_views_urlconf"
_mod = types.ModuleType(_URLCONF)
_mod.urlpatterns = []
sys.modules.setdefault(_URLCONF, _mod)

PROVIDER = "stapel_core.django.jwt.views.jwt_provider"


def _request(method="get", path="/x/", cookies=None, user=None, data=None):
    req = getattr(factory, method)(path, data or {})
    req.COOKIES = cookies or {}
    req.session = MagicMock()
    if user is not None:
        req.user = user
    return req


def _body(resp):
    return json.loads(resp.content)


# ---------------------------------------------------------------------------
# JWTLogoutView
# ---------------------------------------------------------------------------

class TestJWTLogoutView:
    def test_post_blacklists_tokens_and_clears_cookies(self):
        with patch(PROVIDER) as provider:
            req = _request(
                "post",
                cookies={"stapel_jwt": "acc.tok", "stapel_refresh_jwt": "ref.tok"},
            )
            resp = JWTLogoutView.as_view()(req)
        assert resp.status_code == 200
        assert _body(resp)["status"] == "success"
        assert provider.blacklist_token.call_count == 2
        assert resp.cookies["stapel_jwt"]["max-age"] == 0
        assert resp.cookies["stapel_refresh_jwt"]["max-age"] == 0
        assert req._jwt_skip_cookie_update is True
        req.session.flush.assert_called_once()

    def test_post_without_tokens_still_succeeds(self):
        with patch(PROVIDER) as provider:
            req = _request("post")
            resp = JWTLogoutView.as_view()(req)
        assert resp.status_code == 200
        provider.blacklist_token.assert_not_called()

    def test_get_delegates_to_post(self):
        with patch(PROVIDER):
            req = _request("get", cookies={"stapel_jwt": "acc.tok"})
            resp = JWTLogoutView.as_view()(req)
        assert resp.status_code == 200
        assert _body(resp)["status"] == "success"

    def test_error_returns_500(self):
        with (
            patch(PROVIDER),
            patch(
                "stapel_core.django.jwt.views.extract_jwt_from_request",
                side_effect=RuntimeError("boom"),
            ),
        ):
            req = _request("post")
            resp = JWTLogoutView.as_view()(req)
        assert resp.status_code == 500
        assert _body(resp)["status"] == "error"


# ---------------------------------------------------------------------------
# JWTRefreshView
# ---------------------------------------------------------------------------

class TestJWTRefreshView:
    def test_refresh_forbidden_by_default(self):
        req = _request("post", cookies={"stapel_refresh_jwt": "ref.tok"})
        resp = JWTRefreshView.as_view()(req)
        assert resp.status_code == 403
        assert "not allowed" in _body(resp)["message"]

    @override_settings(JWT_REFRESH_ALLOWED=True)
    def test_no_refresh_token_returns_400(self):
        with patch(PROVIDER):
            req = _request("post")
            resp = JWTRefreshView.as_view()(req)
        assert resp.status_code == 400

    @override_settings(JWT_REFRESH_ALLOWED=True)
    def test_failed_refresh_returns_401(self):
        with patch(PROVIDER) as provider:
            provider.refresh_access_token.return_value = None
            req = _request("post", cookies={"stapel_refresh_jwt": "ref.tok"})
            resp = JWTRefreshView.as_view()(req)
        assert resp.status_code == 401

    @override_settings(JWT_REFRESH_ALLOWED=True)
    def test_successful_refresh_sets_cookie(self):
        with patch(PROVIDER) as provider:
            provider.refresh_access_token.return_value = "new.access.tok"
            req = _request("post", cookies={"stapel_refresh_jwt": "ref.tok"})
            resp = JWTRefreshView.as_view()(req)
        assert resp.status_code == 200
        body = _body(resp)
        assert body["status"] == "success"
        assert body["access_token"] == "new.access.tok"
        assert resp.cookies["stapel_jwt"].value == "new.access.tok"
        provider.refresh_access_token.assert_called_once_with("ref.tok")

    @override_settings(JWT_REFRESH_ALLOWED=True)
    def test_error_returns_500(self):
        with patch(PROVIDER) as provider:
            provider.refresh_access_token.side_effect = RuntimeError("boom")
            req = _request("post", cookies={"stapel_refresh_jwt": "ref.tok"})
            resp = JWTRefreshView.as_view()(req)
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# JWTStatusView
# ---------------------------------------------------------------------------

def _auth_user(**kwargs):
    defaults = dict(
        is_authenticated=True,
        id="uid-1",
        email="s@example.com",
        username="statususer",
        is_staff=True,
        is_superuser=False,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class TestJWTStatusView:
    def test_no_tokens_returns_unauthenticated(self):
        req = _request("get", user=SimpleNamespace(is_authenticated=False))
        resp = JWTStatusView.as_view()(req)
        assert resp.status_code == 200
        body = _body(resp)
        assert body["authenticated"] is False
        assert body["message"] == "No tokens found"

    def test_valid_tokens_for_authenticated_user(self):
        with patch(PROVIDER) as provider:
            provider.handler.decode_token.side_effect = [
                {"exp": 111},  # access
                {"exp": 222},  # refresh
            ]
            req = _request(
                "get",
                cookies={"stapel_jwt": "acc.tok", "stapel_refresh_jwt": "ref.tok"},
                user=_auth_user(),
            )
            resp = JWTStatusView.as_view()(req)
        body = _body(resp)
        assert body["authenticated"] is True
        assert body["user"]["user_id"] == "uid-1"
        assert body["user"]["email"] == "s@example.com"
        assert body["user"]["is_staff"] is True
        assert body["tokens"]["access_token_valid"] is True
        assert body["tokens"]["refresh_token_valid"] is True
        assert body["tokens"]["access_token_exp"] == 111
        assert body["tokens"]["refresh_token_exp"] == 222

    def test_invalid_access_token_reported(self):
        with patch(PROVIDER) as provider:
            provider.handler.decode_token.return_value = None
            req = _request(
                "get",
                cookies={"stapel_jwt": "acc.tok"},
                user=SimpleNamespace(is_authenticated=False),
            )
            resp = JWTStatusView.as_view()(req)
        body = _body(resp)
        assert body["authenticated"] is False
        assert body["user"]["user_id"] is None
        assert body["tokens"]["access_token_valid"] is False
        assert body["tokens"]["access_token_exp"] is None

    def test_error_returns_500(self):
        with patch(PROVIDER) as provider:
            provider.handler.decode_token.side_effect = RuntimeError("boom")
            req = _request(
                "get",
                cookies={"stapel_jwt": "acc.tok"},
                user=SimpleNamespace(is_authenticated=False),
            )
            resp = JWTStatusView.as_view()(req)
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# JWTCookieLoginView
# ---------------------------------------------------------------------------

LOGIN_PROVIDER = "stapel_core.django.jwt.login_views.jwt_provider"


@pytest.mark.urls(_URLCONF)
class TestJWTCookieLoginViewDispatch:
    def test_staff_user_redirected_to_next(self):
        req = _request(
            "get",
            path="/auth/admin/login/",
            data={"next": "/svc/admin/"},
            user=SimpleNamespace(is_authenticated=True, is_staff=True, is_superuser=False),
        )
        resp = JWTCookieLoginView.as_view()(req)
        assert resp.status_code == 302
        assert resp.url == "/svc/admin/"

    def test_staff_user_login_next_avoids_redirect_loop(self):
        req = _request(
            "get",
            path="/auth/admin/login/",
            data={"next": "/auth/admin/login/"},
            user=SimpleNamespace(is_authenticated=True, is_staff=False, is_superuser=True),
        )
        resp = JWTCookieLoginView.as_view()(req)
        assert resp.status_code == 302
        assert resp.url == "/auth/admin/"

    def test_staff_user_without_next_goes_to_admin_index(self):
        req = _request(
            "get",
            path="/auth/admin/login/",
            user=SimpleNamespace(is_authenticated=True, is_staff=True, is_superuser=False),
        )
        resp = JWTCookieLoginView.as_view()(req)
        assert resp.url == "/auth/admin/"

    def test_non_staff_user_logged_out_and_cookies_cleared(self):
        req = _request(
            "get",
            path="/auth/admin/login/",
            user=SimpleNamespace(is_authenticated=True, is_staff=False, is_superuser=False),
        )
        with patch.object(LoginView, "dispatch", return_value=HttpResponse()):
            resp = JWTCookieLoginView.as_view()(req)
        assert resp.cookies["stapel_jwt"]["max-age"] == 0
        assert resp.cookies["stapel_refresh_jwt"]["max-age"] == 0
        req.session.flush.assert_called_once()

    def test_anonymous_user_falls_through_to_login_form(self):
        marker = HttpResponse("login-form")
        req = _request(
            "get",
            path="/auth/admin/login/",
            user=SimpleNamespace(is_authenticated=False),
        )
        with patch.object(LoginView, "dispatch", return_value=marker):
            resp = JWTCookieLoginView.as_view()(req)
        assert resp is marker


class TestJWTCookieLoginViewFormValid:
    def _view_and_form(self):
        view = JWTCookieLoginView()
        view.request = _request("post", path="/auth/admin/login/")
        form = MagicMock()
        form.get_user.return_value = MagicMock()
        return view, form

    def test_form_valid_sets_jwt_cookies(self):
        view, form = self._view_and_form()
        with (
            patch("stapel_core.django.jwt.login_views.login") as mock_login,
            patch(LOGIN_PROVIDER) as provider,
            patch.object(LoginView, "form_valid", return_value=HttpResponse()),
        ):
            provider.create_tokens.return_value = ("acc.tok", "ref.tok")
            resp = view.form_valid(form)
        mock_login.assert_called_once()
        assert resp.cookies["stapel_jwt"].value == "acc.tok"
        assert resp.cookies["stapel_refresh_jwt"].value == "ref.tok"

    def test_form_valid_token_error_still_returns_response(self):
        view, form = self._view_and_form()
        fallback = HttpResponse("no-jwt")
        with (
            patch("stapel_core.django.jwt.login_views.login"),
            patch(LOGIN_PROVIDER) as provider,
            patch.object(LoginView, "form_valid", return_value=fallback),
        ):
            provider.create_tokens.side_effect = RuntimeError("keys unavailable")
            resp = view.form_valid(form)
        assert resp is fallback
        assert "stapel_jwt" not in resp.cookies
