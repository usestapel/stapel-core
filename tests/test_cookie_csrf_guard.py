"""The CSRF guard on the cookie-first DRF authenticators.

Security audit 2026-09-11, §7 item 5(c). DRF views are ``csrf_exempt`` by
construction, so ``CsrfExemptAPIMiddleware`` — which already knows that a
cookie-only browser request must prove same-origin — never gets to decide
anything about them: ``CsrfViewMiddleware`` skips the view entirely. The JWT
cookie is ambient authority exactly like a session cookie, and the only thing
standing between it and a cross-site POST was ``SameSite=Lax``.

The proof this module accepts is the one the middleware's own docstring
describes: the ``X-Requested-With`` custom header (not settable cross-origin
without a CORS preflight), or an ``Origin``/``Referer`` this deployment
serves — the same two facts Django's own CSRF origin check reads. A bearer in
the ``Authorization`` header is not ambient and is never asked for a proof.
"""

from unittest.mock import patch

import pytest
from django.test import RequestFactory
from rest_framework import exceptions

from stapel_core.django.jwt.authentication import (
    JWTCookieAuthentication,
    enforce_cookie_csrf,
)

factory = RequestFactory()

AUTH_PROVIDER = "stapel_core.django.jwt.provider.jwt_provider"
GET_OR_CREATE = "stapel_core.django.jwt.utils.get_or_create_user_from_jwt"
IS_USER_BL = "stapel_core.django.jwt.authentication.is_user_blacklisted"

COOKIE = {"stapel_jwt": "aaa.bbb.ccc"}
HOST = "app.example.com"


@pytest.fixture(autouse=True)
def _serve_the_host(settings):
    settings.ALLOWED_HOSTS = [HOST]


def _post(cookies=None, **extra):
    request = factory.post("/api/rooms/", HTTP_HOST=HOST, **extra)
    request.COOKIES = cookies or {}
    return request


def _get(cookies=None, **extra):
    request = factory.get("/api/rooms/", HTTP_HOST=HOST, **extra)
    request.COOKIES = cookies or {}
    return request


class TestEnforceCookieCsrf:

    def test_cookie_only_post_without_a_proof_is_refused(self):
        with pytest.raises(exceptions.PermissionDenied):
            enforce_cookie_csrf(_post(cookies=COOKIE))

    def test_requested_with_header_is_a_proof(self):
        enforce_cookie_csrf(
            _post(cookies=COOKIE, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        )

    def test_same_origin_header_is_a_proof(self):
        enforce_cookie_csrf(
            _post(cookies=COOKIE, HTTP_ORIGIN=f"http://{HOST}")
        )

    def test_foreign_origin_is_refused(self):
        with pytest.raises(exceptions.PermissionDenied):
            enforce_cookie_csrf(
                _post(cookies=COOKIE, HTTP_ORIGIN="https://evil.example.net")
            )

    def test_a_trusted_origin_is_a_proof(self, settings):
        settings.CSRF_TRUSTED_ORIGINS = ["https://*.example.com"]
        enforce_cookie_csrf(
            _post(cookies=COOKIE, HTTP_ORIGIN="https://studio.example.com")
        )

    def test_same_origin_referer_is_a_proof_when_no_origin_is_sent(self):
        enforce_cookie_csrf(
            _post(cookies=COOKIE, HTTP_REFERER=f"http://{HOST}/rooms/abc")
        )

    def test_foreign_referer_is_refused(self):
        with pytest.raises(exceptions.PermissionDenied):
            enforce_cookie_csrf(
                _post(cookies=COOKIE, HTTP_REFERER="https://evil.example.net/x")
            )

    def test_a_safe_method_is_never_asked_for_a_proof(self):
        enforce_cookie_csrf(_get(cookies=COOKIE))


class TestJWTCookieAuthenticationAsksForTheProof:
    """With STAPEL_JWT_COOKIE_CSRF on, the guard runs inside authenticate(),
    so every view using the class inherits it without a permission class or a
    mixin of its own."""

    auth = JWTCookieAuthentication()

    @pytest.fixture(autouse=True)
    def _switch_on(self, settings):
        settings.STAPEL_JWT_COOKIE_CSRF = True

    @staticmethod
    def _mocked(fn):
        """Run *fn* with a valid token and a resolvable user."""
        user = object()
        with patch(AUTH_PROVIDER) as provider, \
                patch(GET_OR_CREATE, return_value=user), \
                patch(IS_USER_BL, return_value=False):
            provider.is_blacklisted.return_value = False
            provider.validate_token.return_value = {"user_id": "u-1"}
            return fn(), user

    def test_cookie_only_post_without_a_proof_is_refused(self):
        with pytest.raises(exceptions.PermissionDenied):
            self._mocked(lambda: self.auth.authenticate(_post(cookies=COOKIE)))

    def test_cookie_only_post_with_the_proof_authenticates(self):
        result, user = self._mocked(lambda: self.auth.authenticate(
            _post(cookies=COOKIE, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        ))
        assert result == (user, None)

    def test_a_bearer_header_is_not_ambient_and_needs_no_proof(self):
        result, user = self._mocked(lambda: self.auth.authenticate(
            _post(HTTP_AUTHORIZATION="Bearer aaa.bbb.ccc")
        ))
        assert result == (user, None)

    def test_a_cookie_get_needs_no_proof(self):
        result, user = self._mocked(
            lambda: self.auth.authenticate(_get(cookies=COOKIE))
        )
        assert result == (user, None)


class TestTheSwitchIsOffByDefault:
    """Nothing an existing deployment sends stops working on the upgrade."""

    auth = JWTCookieAuthentication()

    def test_a_cookie_only_post_still_authenticates_with_the_switch_unset(self):
        result, user = TestJWTCookieAuthenticationAsksForTheProof._mocked(
            lambda: self.auth.authenticate(_post(cookies=COOKIE))
        )
        assert result == (user, None)
