"""The refresh cookie's Path — security audit 2026-09-11, §7 item 3(c).

A refresh token at ``Path=/`` rides every request the browser makes to the
deployment: every page load, every poll, every static asset served from the
same origin. The token the whole session hangs on has one legitimate
destination — the refresh endpoint — and ``JWT_REFRESH_COOKIE_PATH`` is how a
deployment says so.

The default stays ``/``: narrowing the path also stops the refresh cookie
reaching the two places core reads it from OTHER paths — the HTTP
middleware's proactive refresh and the Channels handshake's cookie refresh
(``channels._authenticate_cookie``, the 0.44.1 fix for "the tab left open past
expiry"). That is a deployment's decision, not a library default, so the
switch is explicit and the value is honoured by every place that writes or
clears the cookie.
"""

from django.http import HttpResponse
from django.test import override_settings

from stapel_core.django.jwt.utils import (
    jwt_refresh_cookie_path,
    set_jwt_cookies,
)

REFRESH_PATH = "/auth/api/v1/token/refresh/"


def _morsel(response, name):
    return response.cookies[name]


class TestJwtRefreshCookiePath:

    def test_default_is_the_root_path(self):
        assert jwt_refresh_cookie_path() == "/"

    @override_settings(JWT_REFRESH_COOKIE_PATH=REFRESH_PATH)
    def test_setting_is_honoured(self):
        assert jwt_refresh_cookie_path() == REFRESH_PATH

    @override_settings(JWT_REFRESH_COOKIE_PATH=REFRESH_PATH)
    def test_set_jwt_cookies_scopes_only_the_refresh_cookie(self):
        response = HttpResponse()
        set_jwt_cookies(response, "access.tok.en", "refresh.tok.en")
        assert _morsel(response, "stapel_refresh_jwt")["path"] == REFRESH_PATH
        # The access cookie must stay site-wide: it authenticates every call.
        assert _morsel(response, "stapel_jwt")["path"] == "/"

    def test_root_path_by_default(self):
        response = HttpResponse()
        set_jwt_cookies(response, "access.tok.en", "refresh.tok.en")
        assert _morsel(response, "stapel_refresh_jwt")["path"] == "/"

    @override_settings(JWT_REFRESH_COOKIE_PATH=REFRESH_PATH)
    def test_logout_clears_the_refresh_cookie_at_its_own_path(self):
        from stapel_core.django.jwt.login_views import JWTCookieLoginView

        response = JWTCookieLoginView._clear_jwt_cookies(HttpResponse())
        assert _morsel(response, "stapel_refresh_jwt")["path"] == REFRESH_PATH
        assert _morsel(response, "stapel_jwt")["path"] == "/"
