"""Coverage tests for stapel_core.django.jwt authentication, provider, backends and session."""
import time
import uuid
from unittest.mock import MagicMock, patch

import jwt as pyjwt
import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory

from stapel_core.core.drop import DropOutcome
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
# User-level blacklist helpers
#
# These used to reach for `cache.client.get_client()` — a raw django_redis
# handle — to bypass Django's per-service cache KEY_PREFIX, and fell back to
# the prefix-scoped (i.e. per-service, i.e. broken) path on every other
# backend. 0.39.0 replaced the workaround with the shared revocation
# namespace, so there is one seam to patch and no special backend.
# ---------------------------------------------------------------------------

#: The shared revocation namespace, as imported into the module under test.
USER_BLACKLIST_STORE = "stapel_core.django.jwt.authentication.revocation_cache"


class _ExplodingCache:
    def __getattr__(self, name):
        raise RuntimeError("cache backend exploded")


class TestUserBlacklistHelpers:
    def test_blacklist_user_writes_to_the_shared_namespace(self):
        store = MagicMock()
        with patch(USER_BLACKLIST_STORE, return_value=store):
            assert blacklist_user("u1", ttl=60) is True
        store.set.assert_called_once_with("user_blacklisted:u1", "1", 60)

    def test_ban_round_trips_on_the_real_store(self):
        # It used to only log an error on a non-django_redis backend, which
        # made the ban a permanent no-op with nothing saying so.
        assert blacklist_user("locmem-user") is True
        assert is_user_blacklisted("locmem-user") is True
        assert unblacklist_user("locmem-user")
        assert is_user_blacklisted("locmem-user") is False

    def test_blacklist_user_reports_failure(self):
        with patch(USER_BLACKLIST_STORE, return_value=_ExplodingCache()):
            assert blacklist_user("u1") is False
            report = unblacklist_user("u1")
        assert report.outcome is DropOutcome.UNAVAILABLE
        assert not report

    def test_unblacklist_user_deletes_from_the_shared_namespace(self):
        store = MagicMock()
        with patch(USER_BLACKLIST_STORE, return_value=store):
            unblacklist_user("u1")
        store.delete.assert_called_once_with("user_blacklisted:u1")

    def test_unblacklist_an_unbanned_user_says_it_removed_nothing(self):
        """Was ``is True`` — "the call did not raise" — until 0.47.0."""
        report = unblacklist_user("u1")
        assert report.outcome is DropOutcome.NOT_FOUND
        assert not report

    def test_is_user_blacklisted_true(self):
        store = MagicMock()
        store.get.return_value = "1"
        with patch(USER_BLACKLIST_STORE, return_value=store):
            assert is_user_blacklisted("u1") is True
        store.get.assert_called_once_with("user_blacklisted:u1")

    def test_is_user_blacklisted_false(self):
        store = MagicMock()
        store.get.return_value = None
        with patch(USER_BLACKLIST_STORE, return_value=store):
            assert is_user_blacklisted("u1") is False

    def test_an_unbanned_user_is_simply_absent(self):
        assert is_user_blacklisted("never-banned") is False

    def test_is_user_blacklisted_fails_closed_when_store_is_down(self):
        # Was pinned as False ("error swallowed"), i.e. an unreachable store
        # silently unbanned everyone. Revocation must outlive the store.
        with patch(USER_BLACKLIST_STORE, return_value=_ExplodingCache()):
            assert is_user_blacklisted("u1") is True

    def test_is_user_blacklisted_fail_open_hatch(self):
        from django.test import override_settings
        with patch(USER_BLACKLIST_STORE, return_value=_ExplodingCache()):
            with override_settings(STAPEL_BLACKLIST_FAIL_OPEN=True):
                assert is_user_blacklisted("u1") is False

    def test_is_user_blacklisted_fails_closed_on_store_error(self):
        store = MagicMock()
        store.get.side_effect = RuntimeError("connection refused")
        with patch(USER_BLACKLIST_STORE, return_value=store):
            assert is_user_blacklisted("u1") is True


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

    def test_refresh_reads_the_database_by_default(self, provider):
        """0.39.0: the loader is the default, so an id with no row mints nothing."""
        _, refresh = provider.create_tokens_from_data(
            {"user_id": "r1", "email": "r@example.com"}
        )
        assert provider.refresh_access_token(refresh) is None

    def test_refresh_from_claims_when_the_caller_says_so(self, provider):
        _, refresh = provider.create_tokens_from_data(
            {"user_id": "r1", "email": "r@example.com"}
        )
        new_access = provider.refresh_access_token(refresh, None)
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
    """The backend changes the *lookup* key to email; nothing else.

    Everything below the first two cases is the password check: this backend
    sits in AUTHENTICATION_BACKENDS, so django.contrib.auth.authenticate()
    hands it every login attempt in the process. Before this suite existed it
    returned the user found by email regardless of the password, which made
    "any nonempty password" a valid credential for any known address.
    """

    backend = EmailAuthBackend()

    def test_authenticate_by_username_as_email(self):
        User = get_user_model()
        user = User.objects.create_user(
            username="em1", email="em1@example.com", password="correct-horse"
        )
        assert (
            self.backend.authenticate(
                None, username="em1@example.com", password="correct-horse"
            )
            == user
        )

    def test_authenticate_by_email_kwarg(self):
        User = get_user_model()
        user = User.objects.create_user(
            username="em2", email="em2@example.com", password="correct-horse"
        )
        assert (
            self.backend.authenticate(
                None, email="em2@example.com", password="correct-horse"
            )
            == user
        )

    def test_wrong_password_denies(self):
        User = get_user_model()
        User.objects.create_user(
            username="em-wrong", email="wrong@example.com", password="correct-horse"
        )
        assert (
            self.backend.authenticate(
                None, email="wrong@example.com", password="anything-at-all"
            )
            is None
        )

    def test_missing_password_denies(self):
        """No secret presented = no principal returned, whatever the email."""
        User = get_user_model()
        User.objects.create_user(
            username="em-nopw", email="nopw@example.com", password="correct-horse"
        )
        assert self.backend.authenticate(None, email="nopw@example.com") is None
        assert (
            self.backend.authenticate(None, email="nopw@example.com", password="")
            is None
        )

    def test_inactive_user_denies_even_with_correct_password(self):
        User = get_user_model()
        User.objects.create_user(
            username="em-off",
            email="off@example.com",
            password="correct-horse",
            is_active=False,
        )
        assert (
            self.backend.authenticate(
                None, email="off@example.com", password="correct-horse"
            )
            is None
        )

    def test_ambiguous_email_denies(self):
        """Two rows share the address: no single principal owns the secret."""
        User = get_user_model()
        User.objects.create_user(
            username="dup-a", email="dup@example.com", password="correct-horse"
        )
        User.objects.create_user(
            username="dup-b", email="dup@example.com", password="correct-horse"
        )
        assert (
            self.backend.authenticate(
                None, email="dup@example.com", password="correct-horse"
            )
            is None
        )

    def test_authenticate_without_email_returns_none(self):
        assert self.backend.authenticate(None) is None

    def test_authenticate_unknown_email_returns_none(self):
        assert (
            self.backend.authenticate(
                None, email="nobody@example.com", password="correct-horse"
            )
            is None
        )

    def test_declares_that_it_verifies_credentials(self):
        """The declaration the stapel_auth_backends boot check reads."""
        assert EmailAuthBackend.verifies_credentials is True

    def test_get_user_found(self):
        User = get_user_model()
        user = User.objects.create_user(username="em3", email="em3@example.com")
        assert self.backend.get_user(user.pk) == user

    def test_get_user_missing_returns_none(self):
        assert self.backend.get_user(uuid.uuid4()) is None
