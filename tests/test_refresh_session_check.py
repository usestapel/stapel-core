"""The middleware's mint-from-refresh asks the session authority first.

Production, 2026-09-19..24 (a client fleet): a browser presented a refresh token
the refresh endpoint refused (its jti was no longer its session's current
one), while ``JWTAuthMiddleware`` kept minting access tokens from the same
token on every expired request to the auth service. The two refresh paths
disagreed, the client saw "refresh 401, /me 200", and a 7 GB upload was lost
at its ``complete`` call. These tests walk that exact pair: the real
provider, the real middleware, a validly signed refresh token.
"""

import logging

import pytest
from django.http import HttpResponse
from django.test import RequestFactory

from stapel_core.django.jwt.middleware import JWTAuthMiddleware
from stapel_core.django.jwt.provider import (
    register_refresh_check,
    unregister_refresh_check,
)


@pytest.fixture
def provider():
    from stapel_core.django.jwt.provider import jwt_provider

    jwt_provider.reset()
    yield jwt_provider
    jwt_provider.reset()


@pytest.fixture
def refused_jtis():
    refused: set = set()

    def check(payload):
        return payload.get("jti") not in refused

    register_refresh_check(check)
    yield refused
    unregister_refresh_check(check)


def _user(username):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create(
        username=username, email=f"{username}@example.com"
    )


def _refresh_for(provider, user):
    from stapel_core.django.jwt.utils import serialize_user_to_jwt_data

    _, refresh = provider.create_tokens_from_data(serialize_user_to_jwt_data(user))
    return refresh


def _expired_access_request(refresh):
    from stapel_core.django.jwt.utils import jwt_cookie_names

    access_name, refresh_name = jwt_cookie_names()
    req = RequestFactory().get("/auth/api/v1/me/")
    # No access cookie: the request that follows an hour of uploading.
    req.COOKIES = {refresh_name: refresh}
    from django.contrib.sessions.backends.cache import SessionStore

    req.session = SessionStore()
    return req


@pytest.fixture(autouse=True)
def _refresh_allowed(settings):
    settings.JWT_REFRESH_ALLOWED = True


@pytest.mark.django_db
class TestMiddlewareHonoursTheSessionAuthority:
    def test_a_refused_refresh_token_mints_nothing(self, provider, refused_jtis):
        user = _user("zombie")
        refresh = _refresh_for(provider, user)
        refused_jtis.add(provider.handler.decode_token(refresh)["jti"])

        req = _expired_access_request(refresh)
        JWTAuthMiddleware(lambda r: HttpResponse()).process_request(req)

        assert not getattr(req, "_jwt_refreshed", False), (
            "the middleware minted an access token from a refresh token its "
            "session authority refused"
        )
        assert provider.refresh_access_token(refresh) is None

    def test_an_honoured_refresh_token_still_mints(self, provider, refused_jtis):
        user = _user("alive")
        refresh = _refresh_for(provider, user)

        req = _expired_access_request(refresh)
        JWTAuthMiddleware(lambda r: HttpResponse()).process_request(req)

        assert getattr(req, "_jwt_refreshed", False) is True
        assert provider.validate_token(req._new_access_token)["user_id"] == str(user.pk)

    def test_the_refresh_log_names_the_user_id_not_the_email(self, provider, caplog):
        user = _user("logged")
        refresh = _refresh_for(provider, user)

        req = _expired_access_request(refresh)
        with caplog.at_level(logging.INFO, logger="stapel_core.django.jwt.middleware"):
            JWTAuthMiddleware(lambda r: HttpResponse()).process_request(req)

        text = caplog.text
        assert "Token refreshed for user" in text
        assert str(user.pk) in text
        assert "@example.com" not in text


def test_registration_is_idempotent():
    from stapel_core.django.jwt import provider as mod

    def check(payload):
        return True

    register_refresh_check(check)
    register_refresh_check(check)
    try:
        assert mod._refresh_checks.count(check) == 1
    finally:
        unregister_refresh_check(check)


@pytest.fixture
def minted():
    from stapel_core.django.jwt.provider import (
        register_access_observer,
        unregister_access_observer,
    )

    seen: list = []

    def observer(refresh_payload, access_token):
        seen.append((refresh_payload.get("jti"), access_token))

    register_access_observer(observer)
    yield seen
    unregister_access_observer(observer)


@pytest.mark.django_db
class TestEveryMintFromRefreshIsObserved:
    """The session authority sees each access token a refresh token mints, on
    the middleware path as on a direct call, so it can revoke it later."""

    def test_the_middleware_mint_reaches_the_observer(self, provider, minted):
        user = _user("observed")
        refresh = _refresh_for(provider, user)

        req = _expired_access_request(refresh)
        JWTAuthMiddleware(lambda r: HttpResponse()).process_request(req)

        assert req._jwt_refreshed is True
        assert minted == [
            (provider.handler.decode_token(refresh)["jti"], req._new_access_token)
        ]

    def test_a_direct_refresh_reaches_the_observer(self, provider, minted):
        refresh = _refresh_for(provider, _user("direct"))
        access = provider.refresh_access_token(refresh)
        assert access and minted == [(provider.handler.decode_token(refresh)["jti"], access)]

    def test_a_refused_refresh_is_not_observed(self, provider, minted, refused_jtis):
        refresh = _refresh_for(provider, _user("refused"))
        refused_jtis.add(provider.handler.decode_token(refresh)["jti"])
        assert provider.refresh_access_token(refresh) is None
        assert minted == []

    def test_an_observer_that_fails_withholds_the_token(self, provider):
        from stapel_core.django.jwt.provider import (
            register_access_observer,
            unregister_access_observer,
        )

        def broken(refresh_payload, access_token):
            raise RuntimeError("session store down")

        register_access_observer(broken)
        try:
            refresh = _refresh_for(provider, _user("withheld"))
            assert provider.refresh_access_token(refresh) is None
        finally:
            unregister_access_observer(broken)
