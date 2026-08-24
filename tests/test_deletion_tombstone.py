"""Deletion is the one lifecycle event a bearer token must never undo.

A consumer-mode verifier (``JWT_CREATE_USERS_FROM_TOKEN=True``) materialises
a local row for a user it has never seen — that is the point of the mode. It
cannot tell that case from "I deleted this uid", because both surface as
``User.DoesNotExist``. So a deleted user's still-valid token walked into a
consumer service and was re-created from its own claims; the profiles service
then served the profile of an account that existed nowhere else.

0.39.0 gave the fleet a shared revocation namespace. 0.40.0 puts a deletion
tombstone in it, written by the deletion itself, and makes consumer-mode
verifiers consult it before trusting a claim.

Every test in the first three classes fails on 0.39.0.
"""
import uuid
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

from stapel_core.core.revocation_store import reset_revocation_cache
from stapel_core.django.jwt.tombstone import (
    TOMBSTONE_PREFIX,
    connect_deletion_tombstone,
    is_user_tombstoned,
    lift_tombstone,
    refresh_token_lifetime,
    tombstone_ttl,
    tombstone_user,
)
from stapel_core.django.jwt.utils import get_or_create_user_from_jwt

LOCMEM = "django.core.cache.backends.locmem.LocMemCache"
SHARED = "tombstone-shared-store"

#: Two services, one store, different cache prefixes — the 0.39.0 shape.
ISSUER = {"default": {"BACKEND": LOCMEM, "LOCATION": SHARED, "KEY_PREFIX": "auth"}}
CONSUMER = {
    "default": {"BACKEND": LOCMEM, "LOCATION": SHARED, "KEY_PREFIX": "stapel_profiles"}
}

TOMBSTONE_STORE = "stapel_core.core.revocation_store.revocation_cache"


@pytest.fixture(autouse=True)
def _clean_store():
    reset_revocation_cache()
    with override_settings(CACHES=ISSUER):
        from stapel_core.core.revocation_store import revocation_cache

        revocation_cache().clear()
    reset_revocation_cache()
    yield
    reset_revocation_cache()


def _claims(user):
    return {
        "user_id": str(user.pk),
        "email": user.email,
        "username": user.username,
        "is_staff": False,
        "is_superuser": False,
        "is_active": True,
    }


# ---------------------------------------------------------------------------
# The deletion writes its own tombstone — no caller has to remember.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestDeletionPublishesTheTombstone:
    def test_deleting_a_user_tombstones_them(self):
        connect_deletion_tombstone()
        user = get_user_model().objects.create(username="tomb-1")
        uid = user.pk

        assert is_user_tombstoned(uid) is False
        user.delete()
        assert is_user_tombstoned(uid) is True

    def test_a_queryset_delete_is_tombstoned_too(self):
        """post_delete fires per row, so bulk deletion is covered."""
        connect_deletion_tombstone()
        User = get_user_model()
        a = User.objects.create(username="tomb-bulk-a")
        b = User.objects.create(username="tomb-bulk-b")

        User.objects.filter(pk__in=[a.pk, b.pk]).delete()

        assert is_user_tombstoned(a.pk) is True
        assert is_user_tombstoned(b.pk) is True

    def test_connecting_twice_writes_one_tombstone(self):
        connect_deletion_tombstone()
        connect_deletion_tombstone()
        user = get_user_model().objects.create(username="tomb-idem")
        with patch(
            "stapel_core.django.jwt.tombstone.tombstone_user"
        ) as write:
            user.delete()
        assert write.call_count == 1

    def test_a_surviving_user_is_not_tombstoned(self):
        connect_deletion_tombstone()
        user = get_user_model().objects.create(username="tomb-alive")
        assert is_user_tombstoned(user.pk) is False

    def test_the_app_config_is_what_connects_it(self):
        """The wiring, not a caller, is what makes this non-optional."""
        import inspect

        from stapel_core.django.apps import CommonDjangoConfig

        assert "connect_deletion_tombstone" in inspect.getsource(
            CommonDjangoConfig.ready
        )


# ---------------------------------------------------------------------------
# The consumer-mode verifier refuses. This is the reported defect.
# ---------------------------------------------------------------------------

@pytest.fixture
def consumer_mode():
    with override_settings(JWT_CREATE_USERS_FROM_TOKEN=True):
        yield


@pytest.fixture
def issuer_mode():
    with override_settings(JWT_CREATE_USERS_FROM_TOKEN=False):
        yield


@pytest.mark.django_db
@pytest.mark.usefixtures("consumer_mode")
class TestConsumerModeRefusesADeletedUser:
    def test_a_deleted_users_token_creates_nobody(self):
        connect_deletion_tombstone()
        User = get_user_model()
        user = User.objects.create(username="ghost", email="ghost@example.com")
        claims = _claims(user)
        user.delete()

        assert get_or_create_user_from_jwt(claims) is None
        assert User.objects.filter(pk=claims["user_id"]).exists() is False

    def test_the_tombstone_crosses_the_service_boundary(self):
        """Deleted in the issuer, refused in the consumer — different prefixes."""
        connect_deletion_tombstone()
        User = get_user_model()
        user = User.objects.create(username="ghost-x", email="gx@example.com")
        claims = _claims(user)

        with override_settings(CACHES=ISSUER):
            user.delete()

        reset_revocation_cache()
        with override_settings(CACHES=CONSUMER):
            assert get_or_create_user_from_jwt(claims) is None

    def test_a_stale_shadow_copy_is_refused_too(self):
        """The row still exists locally; the issuer says the account is gone."""
        User = get_user_model()
        user = User.objects.create(username="shadow", email="shadow@example.com")
        tombstone_user(user.pk)

        assert get_or_create_user_from_jwt(_claims(user)) is None

    def test_first_contact_with_a_live_user_still_works(self):
        """Consumer mode must keep doing the thing it exists for."""
        uid = str(uuid.uuid4())
        created = get_or_create_user_from_jwt({
            "user_id": uid,
            "email": "newcomer@example.com",
            "username": "newcomer",
        })
        assert created is not None
        assert str(created.pk) == uid

    def test_the_tombstone_outlives_a_refresh_cycle(self):
        """The whole point of sizing the TTL off the refresh lifetime."""
        connect_deletion_tombstone()
        from stapel_core.django.jwt.provider import jwt_provider

        User = get_user_model()
        user = User.objects.create(username="refresher", email="r@example.com")
        uid = user.pk  # delete() nulls instance.pk once the collector is done
        jwt_provider.reset()
        try:
            _, refresh = jwt_provider.create_tokens_from_data(_claims(user))
            user.delete()
            # The refresh token is intact and unexpired; the account is not.
            assert jwt_provider.refresh_access_token(refresh) is None
            assert is_user_tombstoned(uid) is True
        finally:
            jwt_provider.reset()


@pytest.mark.django_db
@pytest.mark.usefixtures("issuer_mode")
class TestIssuerModeIsUnaffected:
    def test_a_deleted_user_is_already_refused_without_the_tombstone(self):
        User = get_user_model()
        user = User.objects.create(username="issuer-ghost")
        claims = _claims(user)
        user.delete()
        assert get_or_create_user_from_jwt(claims) is None

    def test_issuer_mode_does_not_pay_a_cache_read(self):
        user = get_user_model().objects.create(username="issuer-live")
        with patch(
            "stapel_core.django.jwt.utils._tombstoned"
        ) as probe:
            assert get_or_create_user_from_jwt(_claims(user)) is not None
        probe.assert_not_called()


# ---------------------------------------------------------------------------
# TTL: derived, never silently shorter than the credential it must outlive.
# ---------------------------------------------------------------------------

class TestTombstoneTtl:
    def test_defaults_to_the_refresh_lifetime(self):
        with override_settings(JWT_REFRESH_TOKEN_LIFETIME=1234):
            assert refresh_token_lifetime() == 1234
            assert tombstone_ttl() == 1234

    def test_a_longer_explicit_value_is_honoured(self):
        with override_settings(
            JWT_REFRESH_TOKEN_LIFETIME=1000, STAPEL_JWT_TOMBSTONE_TTL=5000
        ):
            assert tombstone_ttl() == 5000

    def test_a_shorter_explicit_value_is_clamped_not_obeyed(self):
        with override_settings(
            JWT_REFRESH_TOKEN_LIFETIME=1000, STAPEL_JWT_TOMBSTONE_TTL=10
        ):
            assert tombstone_ttl() == 1000

    def test_the_ttl_is_what_gets_written(self):
        store = MagicMock()
        with patch(TOMBSTONE_STORE, return_value=store):
            with override_settings(JWT_REFRESH_TOKEN_LIFETIME=4321):
                tombstone_user("u-ttl")
        store.set.assert_called_once_with(f"{TOMBSTONE_PREFIX}u-ttl", "1", 4321)


class TestTombstoneTtlCheck:
    def _run(self):
        from stapel_core.django.blacklist_checks import check_tombstone_ttl

        return check_tombstone_ttl()

    def test_silent_when_derived(self):
        assert self._run() == []

    def test_fires_when_shorter_than_the_refresh_lifetime(self):
        with override_settings(
            JWT_REFRESH_TOKEN_LIFETIME=604800, STAPEL_JWT_TOMBSTONE_TTL=3600
        ):
            findings = self._run()
        assert [f.id for f in findings] == ["stapel_core.revocation.E002"]
        assert findings[0].level >= 40

    def test_silent_when_longer(self):
        with override_settings(
            JWT_REFRESH_TOKEN_LIFETIME=600, STAPEL_JWT_TOMBSTONE_TTL=99999
        ):
            assert self._run() == []

    def test_a_non_numeric_value_is_an_error(self):
        with override_settings(STAPEL_JWT_TOMBSTONE_TTL="soon"):
            findings = self._run()
        assert [f.id for f in findings] == ["stapel_core.revocation.E002"]

    def test_it_cannot_be_silently_muted(self):
        from stapel_core.django.check_guard import is_security_critical

        assert is_security_critical("stapel_core.revocation.E002")


# ---------------------------------------------------------------------------
# Store down: a deleted principal is not admitted because the store is offline.
# ---------------------------------------------------------------------------

class TestTombstoneFailsClosed:
    def _down(self):
        store = MagicMock()
        store.get.side_effect = RuntimeError("redis down")
        store.set.side_effect = RuntimeError("redis down")
        return store

    def test_unreachable_store_answers_tombstoned(self):
        with patch(TOMBSTONE_STORE, return_value=self._down()):
            assert is_user_tombstoned("anyone") is True

    def test_the_one_documented_hatch_flips_it(self):
        with patch(TOMBSTONE_STORE, return_value=self._down()):
            with override_settings(STAPEL_BLACKLIST_FAIL_OPEN=True):
                assert is_user_tombstoned("anyone") is False

    def test_a_failed_write_is_reported_not_swallowed(self):
        with patch(TOMBSTONE_STORE, return_value=self._down()):
            assert tombstone_user("anyone") is False

    def test_lift_reports_failure_too(self):
        store = MagicMock()
        store.delete.side_effect = RuntimeError("down")
        with patch(TOMBSTONE_STORE, return_value=store):
            assert lift_tombstone("anyone") is False

    def test_lift_removes_a_tombstone(self):
        tombstone_user("liftable")
        assert is_user_tombstoned("liftable") is True
        assert lift_tombstone("liftable") is True
        assert is_user_tombstoned("liftable") is False


# ---------------------------------------------------------------------------
# Same family, both flagged in the 0.38.0 sweep.
# ---------------------------------------------------------------------------

class TestOnlyOurOwnTokenCanBeRevoked:
    """Logout took its ``jti`` from an UNVERIFIED decode.

    Anyone who could observe a victim's token — any component that logs or
    forwards one — could mint an unsigned JWT carrying that ``jti`` and
    ``exp`` and POST it to the logout endpoint, which requires no
    authentication. The victim's live session died. A denial of service on
    another user's account, delivered through the revocation machinery.
    """

    @pytest.fixture(autouse=True)
    def _provider(self):
        from stapel_core.django.jwt.provider import jwt_provider

        jwt_provider.reset()
        self.provider = jwt_provider
        yield
        jwt_provider.reset()

    def _forged_with_victims_jti(self, victim_token):
        import jwt as pyjwt

        claims = pyjwt.decode(victim_token, options={"verify_signature": False})
        # Same jti and exp, signed with a key that is not ours.
        return pyjwt.encode(claims, "attacker-key", algorithm="HS256")

    def test_a_forged_token_cannot_revoke_a_victims_session(self):
        victim, _ = self.provider.create_tokens_from_data(
            {"user_id": "victim-1", "email": "v@example.com"}
        )
        forged = self._forged_with_victims_jti(victim)

        assert self.provider.blacklist_token(forged) is False
        assert self.provider.validate_token(victim) is not None, (
            "a forged token revoked someone else's live session"
        )

    def test_an_unsigned_token_cannot_revoke_either(self):
        import jwt as pyjwt

        victim, _ = self.provider.create_tokens_from_data(
            {"user_id": "victim-2", "email": "v2@example.com"}
        )
        claims = pyjwt.decode(victim, options={"verify_signature": False})
        unsigned = pyjwt.encode(claims, key="", algorithm="none")

        assert self.provider.blacklist_token(unsigned) is False
        assert self.provider.validate_token(victim) is not None

    def test_our_own_token_still_revokes(self):
        access, _ = self.provider.create_tokens_from_data({"user_id": "real-1"})
        assert self.provider.blacklist_token(access) is True
        assert self.provider.validate_token(access) is None

    def test_logging_out_late_still_revokes_the_refresh_token(self):
        """The access token has expired; the refresh token beside it has not.

        An expired access token has always returned False here (nothing left
        to revoke — see the ``expires_in > 0`` guard), and that is unchanged.
        What must keep working is the credential that is still live.
        """
        import time

        with override_settings(JWT_ACCESS_TOKEN_LIFETIME=1):
            self.provider.reset()
            access, refresh = self.provider.create_tokens_from_data(
                {"user_id": "late-1"}
            )
            time.sleep(1.2)

        assert self.provider.validate_token(access) is None  # expired on its own
        assert self.provider.blacklist_token(access) is False  # nothing to revoke
        assert self.provider.blacklist_token(refresh) is True  # this is the point


class TestSessionCookieIsNotBlanketCsrfExempt:
    """The docstring described the fix; the code only counted the JWT cookie.

    The JWT middleware calls ``login()``, so any browser that authenticated
    holds a Django session cookie, and DRF's ``SessionAuthentication`` accepts
    it alone. Such a request was blanket-exempted from CSRF on every mutating
    ``/api/`` endpoint.
    """

    def _exempt(self, cookies=None, headers=None):
        from django.test import RequestFactory

        from stapel_core.django.jwt.middleware import CsrfExemptAPIMiddleware

        request = RequestFactory().post("/api/thing/", **(headers or {}))
        for name, value in (cookies or {}).items():
            request.COOKIES[name] = value
        CsrfExemptAPIMiddleware(lambda r: None).process_request(request)
        return getattr(request, "_dont_enforce_csrf_checks", False)

    def test_session_cookie_only_request_keeps_csrf(self):
        assert self._exempt(cookies={"sessionid": "abc"}) is False

    def test_session_cookie_with_same_origin_proof_is_exempt(self):
        assert self._exempt(
            cookies={"sessionid": "abc"},
            headers={"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"},
        ) is True

    def test_session_cookie_plus_bearer_is_exempt(self):
        assert self._exempt(
            cookies={"sessionid": "abc"},
            headers={"HTTP_AUTHORIZATION": "Bearer x"},
        ) is True

    def test_jwt_cookie_only_still_keeps_csrf(self):
        assert self._exempt(cookies={"stapel_jwt": "tok"}) is False

    def test_a_request_with_no_cookie_at_all_is_still_exempt(self):
        """Nothing to forge: an anonymous /api/ call is not a CSRF target."""
        assert self._exempt() is True

    def test_the_configured_session_cookie_name_is_honoured(self):
        with override_settings(SESSION_COOKIE_NAME="my_session"):
            assert self._exempt(cookies={"my_session": "abc"}) is False
