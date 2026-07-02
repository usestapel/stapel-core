"""Coverage tests for stapel_core.django.jwt authentication, provider, backends and session."""
import time
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import jwt as pyjwt
import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory

from stapel_core.django.jwt.authentication import (
    JWTCookieAuthentication,
    blacklist_user,
    is_user_blacklisted,
    unblacklist_user,
)
from stapel_core.django.jwt.backends import JWTAuthBackend
from stapel_core.django.jwt.provider import JWTProvider, jwt_provider
from stapel_core.django.jwt.session import EmailAuthBackend

factory = RequestFactory()


class FakeUser:
    """Plain object so hasattr() checks in serialize_user_to_jwt_data behave."""

    def __init__(self):
        self.pk = "fake-user-pk"
        self.email = "fake@example.com"
        self.username = "fakeuser"
        self.is_staff = False
        self.is_superuser = False
        self.is_active = True
        self.is_anonymous = False
        self.auth_type = "email"
        self.phone = None


# ---------------------------------------------------------------------------
# User-level redis blacklist helpers
# ---------------------------------------------------------------------------

class _RedisCache:
    """Fake django-redis style cache exposing .client.get_client()."""

    def __init__(self, redis):
        self.client = SimpleNamespace(get_client=lambda: redis)


class _ExplodingCache:
    def __getattr__(self, name):
        raise RuntimeError("cache backend exploded")


class TestUserBlacklistHelpers:
    def test_blacklist_user_uses_raw_redis(self):
        redis = MagicMock()
        with patch("django.core.cache.cache", _RedisCache(redis)):
            blacklist_user("u1", ttl=60)
        redis.setex.assert_called_once_with("user_blacklisted:u1", 60, "1")

    def test_blacklist_user_without_redis_logs_error(self):
        # LocMemCache (conftest) has no .client attribute -> no redis client.
        blacklist_user("u1")  # must not raise

    def test_unblacklist_user_with_redis(self):
        redis = MagicMock()
        with patch("django.core.cache.cache", _RedisCache(redis)):
            unblacklist_user("u1")
        redis.delete.assert_called_once_with("user_blacklisted:u1")

    def test_unblacklist_user_without_redis_noop(self):
        unblacklist_user("u1")  # must not raise

    def test_is_user_blacklisted_true(self):
        redis = MagicMock()
        redis.exists.return_value = 1
        with patch("django.core.cache.cache", _RedisCache(redis)):
            assert is_user_blacklisted("u1") is True

    def test_is_user_blacklisted_false(self):
        redis = MagicMock()
        redis.exists.return_value = 0
        with patch("django.core.cache.cache", _RedisCache(redis)):
            assert is_user_blacklisted("u1") is False

    def test_is_user_blacklisted_without_redis_false(self):
        assert is_user_blacklisted("u1") is False

    def test_get_redis_client_error_swallowed(self):
        with patch("django.core.cache.cache", _ExplodingCache()):
            assert is_user_blacklisted("u1") is False


# ---------------------------------------------------------------------------
# JWTCookieAuthentication
# ---------------------------------------------------------------------------

AUTH_PROVIDER = "stapel_core.django.jwt.provider.jwt_provider"
GET_OR_CREATE = "stapel_core.django.jwt.utils.get_or_create_user_from_jwt"
IS_USER_BL = "stapel_core.django.jwt.authentication.is_user_blacklisted"


def _auth_request(cookies=None, **extra):
    req = factory.get("/api/data/", **extra)
    req.COOKIES = cookies or {}
    return req


class TestJWTCookieAuthentication:
    auth = JWTCookieAuthentication()

    def test_no_token_returns_none(self):
        assert self.auth.authenticate(_auth_request()) is None

    def test_blacklisted_token_returns_none(self):
        with patch(AUTH_PROVIDER) as provider:
            provider.is_blacklisted.return_value = True
            req = _auth_request(cookies={"stapel_jwt": "black.listed.token"})
            assert self.auth.authenticate(req) is None

    def test_invalid_token_returns_none(self):
        with patch(AUTH_PROVIDER) as provider:
            provider.is_blacklisted.return_value = False
            provider.validate_token.return_value = None
            # short token exercises the short_token suffix branch
            req = _auth_request(cookies={"stapel_jwt": "short"})
            assert self.auth.authenticate(req) is None

    def test_user_level_blacklist_returns_none(self):
        with (
            patch(AUTH_PROVIDER) as provider,
            patch(IS_USER_BL, return_value=True),
        ):
            provider.is_blacklisted.return_value = False
            provider.validate_token.return_value = {"user_id": "banned-user"}
            req = _auth_request(cookies={"stapel_jwt": "valid.jwt.token"})
            assert self.auth.authenticate(req) is None

    def test_user_creation_failure_returns_none(self):
        with (
            patch(AUTH_PROVIDER) as provider,
            patch(IS_USER_BL, return_value=False),
            patch(GET_OR_CREATE, return_value=None),
        ):
            provider.is_blacklisted.return_value = False
            provider.validate_token.return_value = {"user_id": "u1"}
            req = _auth_request(cookies={"stapel_jwt": "valid.jwt.token"})
            assert self.auth.authenticate(req) is None

    def test_successful_authentication(self):
        user = MagicMock()
        with (
            patch(AUTH_PROVIDER) as provider,
            patch(IS_USER_BL, return_value=False),
            patch(GET_OR_CREATE, return_value=user),
        ):
            provider.is_blacklisted.return_value = False
            provider.validate_token.return_value = {"user_id": "u1"}
            req = _auth_request(cookies={"stapel_jwt": "valid.jwt.token"})
            result = self.auth.authenticate(req)
        assert result == (user, None)

    def test_exception_returns_none(self):
        with patch(AUTH_PROVIDER) as provider:
            provider.is_blacklisted.side_effect = RuntimeError("redis gone")
            req = _auth_request(
                cookies={"stapel_jwt": "valid.jwt.token"},
                HTTP_USER_AGENT="pytest-agent",
            )
            assert self.auth.authenticate(req) is None

    def test_get_client_ip_forwarded_for(self):
        req = _auth_request(HTTP_X_FORWARDED_FOR="1.2.3.4, 5.6.7.8")
        assert self.auth._get_client_ip(req) == "1.2.3.4"

    def test_get_client_ip_remote_addr(self):
        req = _auth_request()
        assert self.auth._get_client_ip(req) == "127.0.0.1"

    def test_authenticate_header(self):
        assert self.auth.authenticate_header(_auth_request()) == "Bearer"


# ---------------------------------------------------------------------------
# JWTProvider
# ---------------------------------------------------------------------------

@pytest.fixture
def provider():
    jwt_provider.reset()
    yield jwt_provider
    jwt_provider.reset()


class TestJWTProvider:
    def test_singleton(self, provider):
        assert JWTProvider() is provider

    def test_lazy_initialization_and_properties(self, provider):
        assert provider.config.algorithm == "HS256"
        assert provider.handler is not None
        assert provider.manager is not None
        # Second access does not re-initialize
        handler = provider.handler
        assert provider.handler is handler

    def test_create_tokens_from_user_roundtrip(self, provider):
        access, refresh = provider.create_tokens(FakeUser())
        payload = provider.validate_token(access)
        assert payload["user_id"] == "fake-user-pk"
        assert payload["email"] == "fake@example.com"
        assert provider.validate_token(refresh) is None  # refresh is not an access token

    def test_create_tokens_from_data(self, provider):
        access, refresh = provider.create_tokens_from_data(
            {"user_id": "d1", "email": "d@example.com"}
        )
        assert provider.validate_token(access)["user_id"] == "d1"

    def test_refresh_access_token(self, provider):
        _, refresh = provider.create_tokens_from_data(
            {"user_id": "r1", "email": "r@example.com"}
        )
        new_access = provider.refresh_access_token(refresh)
        assert provider.validate_token(new_access)["user_id"] == "r1"

    def test_blacklist_token_lifecycle(self, provider):
        access, _ = provider.create_tokens_from_data({"user_id": "b1"})
        assert provider.is_blacklisted(access) is False
        assert provider.blacklist_token(access) is True
        assert provider.is_blacklisted(access) is True

    def test_blacklist_garbage_token_returns_false(self, provider):
        assert provider.blacklist_token("garbage.token") is False
        assert provider.is_blacklisted("garbage.token") is False

    def test_blacklist_expired_token_returns_false(self, provider):
        expired = pyjwt.encode(
            {"jti": "expired-jti", "exp": int(time.time()) - 100},
            "any-key",
            algorithm="HS256",
        )
        assert provider.blacklist_token(expired) is False

    def test_blacklist_token_without_jti_returns_false(self, provider):
        no_jti = pyjwt.encode(
            {"exp": int(time.time()) + 3600}, "any-key", algorithm="HS256"
        )
        assert provider.blacklist_token(no_jti) is False

    def test_get_jwks_none_for_hs256(self, provider):
        assert provider.get_jwks() is None

    def test_double_checked_locking_second_check(self, provider):
        # Simulate another thread finishing initialization while this one
        # was waiting on the lock: the inner check must return early.
        class TrickLock:
            def __init__(self, target):
                self.target = target

            def __enter__(self):
                self.target._initialized = True

            def __exit__(self, *args):
                return False

        provider._ensure_initialized()  # populate handler/config/manager
        provider._initialized = False
        provider._init_lock = TrickLock(provider)
        try:
            provider._ensure_initialized()
            assert provider._initialized is True
        finally:
            del provider._init_lock  # restore class-level lock

    def test_init_blacklist_falls_back_on_error(self, provider):
        fallback = MagicMock()
        with patch(
            "stapel_core.core.token_blacklist.TokenBlacklist",
            side_effect=[RuntimeError("boom"), fallback],
        ):
            provider._ensure_initialized()
        assert provider._blacklist is fallback


# ---------------------------------------------------------------------------
# JWTAuthBackend
# ---------------------------------------------------------------------------

BACKEND_PROVIDER = "stapel_core.django.jwt.backends.jwt_provider"
BACKEND_GET_OR_CREATE = "stapel_core.django.jwt.backends.get_or_create_user_from_jwt"


class TestJWTAuthBackend:
    backend = JWTAuthBackend()

    def test_no_token_returns_none(self):
        assert self.backend.authenticate(None) is None
        assert self.backend.authenticate(None, jwt_token=None) is None

    def test_invalid_token_returns_none(self):
        with patch(BACKEND_PROVIDER) as provider:
            provider.validate_token.return_value = None
            assert self.backend.authenticate(None, jwt_token="bad.token") is None

    def test_user_creation_failure_returns_none(self):
        with (
            patch(BACKEND_PROVIDER) as provider,
            patch(BACKEND_GET_OR_CREATE, return_value=None),
        ):
            provider.validate_token.return_value = {"user_id": "u1"}
            assert self.backend.authenticate(None, jwt_token="ok.token") is None

    def test_successful_authentication(self):
        user = MagicMock()
        with (
            patch(BACKEND_PROVIDER) as provider,
            patch(BACKEND_GET_OR_CREATE, return_value=user),
        ):
            provider.validate_token.return_value = {"user_id": "u1"}
            assert self.backend.authenticate(None, jwt_token="ok.token") is user

    def test_exception_returns_none(self):
        with patch(BACKEND_PROVIDER) as provider:
            provider.validate_token.side_effect = RuntimeError("boom")
            assert self.backend.authenticate(None, jwt_token="ok.token") is None

    @pytest.mark.django_db
    def test_get_user_found(self):
        User = get_user_model()
        user = User.objects.create_user(username="backenduser")
        assert self.backend.get_user(user.pk) == user

    @pytest.mark.django_db
    def test_get_user_missing_returns_none(self):
        assert self.backend.get_user(uuid.uuid4()) is None


# ---------------------------------------------------------------------------
# EmailAuthBackend
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestEmailAuthBackend:
    backend = EmailAuthBackend()

    def test_authenticate_by_username_as_email(self):
        User = get_user_model()
        user = User.objects.create_user(username="em1", email="em1@example.com")
        assert self.backend.authenticate(None, username="em1@example.com") == user

    def test_authenticate_by_email_kwarg(self):
        User = get_user_model()
        user = User.objects.create_user(username="em2", email="em2@example.com")
        assert self.backend.authenticate(None, email="em2@example.com") == user

    def test_authenticate_without_email_returns_none(self):
        assert self.backend.authenticate(None) is None

    def test_authenticate_unknown_email_returns_none(self):
        assert self.backend.authenticate(None, email="nobody@example.com") is None

    def test_get_user_found(self):
        User = get_user_model()
        user = User.objects.create_user(username="em3", email="em3@example.com")
        assert self.backend.get_user(user.pk) == user

    def test_get_user_missing_returns_none(self):
        assert self.backend.get_user(uuid.uuid4()) is None
