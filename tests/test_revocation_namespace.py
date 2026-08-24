"""Sharing a Redis is not sharing a namespace.

Both blacklists wrote through ``django.core.cache.cache``, and Django builds
the real key as ``f"{KEY_PREFIX}:{VERSION}:{key}"`` from the *deployment's*
CACHES. Every service in a split deployment sets its own ``KEY_PREFIX``
(``auth``, ``stapel_profiles``, ...) exactly so its caches do not collide
with its peers'. Revocation is the one thing that must collide.

Reproduced live on a consumer's stand: a token blacklisted in the auth
service still returned 200 from the profiles service. Both pointed at
``redis://redis:6379/0``; auth wrote ``auth:1:jwt_blacklist:<jti>`` and
profiles looked for ``stapel_profiles:1:jwt_blacklist:<jti>``. "Log out
everywhere", RevokeSuspiciousView and password-change revocation were all
per-service illusions.

The tests below are written the way the defect was reproduced: two cache
connections that differ ONLY in ``KEY_PREFIX``, standing in for two services
sharing one store. On 0.38.0 the revoking side and the verifying side do not
see each other; from 0.39.0 they do.
"""
import uuid
from datetime import timedelta

import pytest
from django.core.cache import caches
from django.test import override_settings

from stapel_core.core.revocation_store import (
    DEFAULT_NAMESPACE,
    NAMESPACE_VERSION,
    revocation_cache,
    revocation_namespace,
    reset_revocation_cache,
)
from stapel_core.core.token_blacklist import TokenBlacklist
from stapel_core.django.jwt.authentication import (
    blacklist_user,
    is_user_blacklisted,
    unblacklist_user,
)

LOCMEM = "django.core.cache.backends.locmem.LocMemCache"

#: One store, two services. LocMemCache keyed on LOCATION shares its backing
#: dict between instances, which is exactly the "one Redis" of the defect.
SHARED_LOCATION = "revocation-shared-store"


def _service(key_prefix):
    """CACHES as one service in the split deployment would configure it."""
    return {
        "default": {
            "BACKEND": LOCMEM,
            "LOCATION": SHARED_LOCATION,
            "KEY_PREFIX": key_prefix,
        }
    }


AUTH_SERVICE = _service("auth")
PROFILES_SERVICE = _service("stapel_profiles")


@pytest.fixture(autouse=True)
def _clean_shared_store():
    reset_revocation_cache()
    with override_settings(CACHES=AUTH_SERVICE):
        revocation_cache().clear()
    reset_revocation_cache()
    yield
    reset_revocation_cache()


# ---------------------------------------------------------------------------
# The reproduction, both halves of revocation.
# ---------------------------------------------------------------------------

class TestRevocationCrossesServices:
    def test_token_revoked_in_auth_is_rejected_by_profiles(self):
        jti = str(uuid.uuid4())

        with override_settings(CACHES=AUTH_SERVICE):
            assert TokenBlacklist().blacklist_token(jti, timedelta(hours=1)) is True

        with override_settings(CACHES=PROFILES_SERVICE):
            assert TokenBlacklist().is_blacklisted(jti) is True, (
                "a token revoked in one service is still valid in the next"
            )

    def test_user_banned_in_auth_is_banned_in_profiles(self):
        uid = str(uuid.uuid4())

        with override_settings(CACHES=AUTH_SERVICE):
            assert blacklist_user(uid, ttl=3600) is True

        with override_settings(CACHES=PROFILES_SERVICE):
            assert is_user_blacklisted(uid) is True

    def test_unban_also_crosses(self):
        uid = str(uuid.uuid4())
        with override_settings(CACHES=AUTH_SERVICE):
            blacklist_user(uid, ttl=3600)
        with override_settings(CACHES=PROFILES_SERVICE):
            assert unblacklist_user(uid) is True
        with override_settings(CACHES=AUTH_SERVICE):
            assert is_user_blacklisted(uid) is False

    def test_a_token_nobody_revoked_still_passes(self):
        """The gate has to be a gate, not a wall."""
        with override_settings(CACHES=PROFILES_SERVICE):
            assert TokenBlacklist().is_blacklisted(str(uuid.uuid4())) is False

    def test_provider_seam_inherits_it(self):
        """Asserted through the provider, not the blacklist class."""
        from stapel_core.django.jwt.provider import jwt_provider

        jwt_provider.reset()
        try:
            with override_settings(CACHES=AUTH_SERVICE):
                access, _ = jwt_provider.create_tokens_from_data(
                    {"user_id": "cross-1", "email": "cross@example.com"}
                )
                assert jwt_provider.blacklist_token(access) is True
            with override_settings(CACHES=PROFILES_SERVICE):
                assert jwt_provider.validate_token(access) is None
        finally:
            jwt_provider.reset()


# ---------------------------------------------------------------------------
# The namespace is a deliberate, documented, fleet-wide value.
# ---------------------------------------------------------------------------

class TestNamespaceIsDeploymentIndependent:
    def test_key_does_not_carry_the_service_prefix(self):
        with override_settings(CACHES=AUTH_SERVICE):
            store = revocation_cache()
            key = store.make_key("jwt_blacklist:abc")
        assert key == f"{DEFAULT_NAMESPACE}:{NAMESPACE_VERSION}:jwt_blacklist:abc"
        assert "auth" not in key

    def test_two_services_compute_the_same_key(self):
        with override_settings(CACHES=AUTH_SERVICE):
            first = revocation_cache().make_key("jwt_blacklist:abc")
        reset_revocation_cache()
        with override_settings(CACHES=PROFILES_SERVICE):
            second = revocation_cache().make_key("jwt_blacklist:abc")
        assert first == second

    def test_a_per_service_key_function_cannot_re_isolate_it(self):
        conf = {
            "default": {
                **AUTH_SERVICE["default"],
                "KEY_FUNCTION": "tests.test_revocation_namespace.isolating_key_func",
            }
        }
        with override_settings(CACHES=conf):
            key = revocation_cache().make_key("jwt_blacklist:abc")
        assert key == f"{DEFAULT_NAMESPACE}:{NAMESPACE_VERSION}:jwt_blacklist:abc"

    def test_an_explicit_fleet_namespace_is_honoured(self):
        with override_settings(
            CACHES=AUTH_SERVICE, STAPEL_JWT_REVOCATION_NAMESPACE="fleet-b"
        ):
            assert revocation_namespace() == "fleet-b"
            assert revocation_cache().make_key("x").startswith("fleet-b:")

    def test_two_fleets_on_one_store_do_not_see_each_other(self):
        """The one legitimate reason to change the namespace."""
        jti = str(uuid.uuid4())
        with override_settings(CACHES=AUTH_SERVICE):
            TokenBlacklist().blacklist_token(jti, timedelta(hours=1))
        reset_revocation_cache()
        with override_settings(
            CACHES=PROFILES_SERVICE, STAPEL_JWT_REVOCATION_NAMESPACE="fleet-b"
        ):
            assert TokenBlacklist().is_blacklisted(jti) is False

    def test_alias_setting_selects_the_connection(self):
        conf = {
            "default": {"BACKEND": LOCMEM, "LOCATION": "not-this-one"},
            "revocation": {
                "BACKEND": LOCMEM,
                "LOCATION": SHARED_LOCATION,
                "KEY_PREFIX": "whatever",
            },
        }
        with override_settings(
            CACHES=conf, STAPEL_JWT_REVOCATION_CACHE="revocation"
        ):
            store = revocation_cache()
            # Same backing store as the named alias, different key namespace.
            assert store._cache is caches["revocation"]._cache
            assert store._cache is not caches["default"]._cache


def isolating_key_func(key, key_prefix, version):
    """A per-service KEY_FUNCTION — dropped by the revocation store."""
    return f"per-service::{key_prefix}::{version}::{key}"


# ---------------------------------------------------------------------------
# The check that fires when a deployment cannot be reached by its peers.
# ---------------------------------------------------------------------------

class TestRevocationNamespaceCheck:
    def _run(self):
        from stapel_core.django.blacklist_checks import check_revocation_namespace

        return check_revocation_namespace()

    def test_silent_on_the_default(self):
        with override_settings(CACHES=AUTH_SERVICE):
            assert self._run() == []

    def test_custom_namespace_is_reported(self):
        with override_settings(
            CACHES=AUTH_SERVICE, STAPEL_JWT_REVOCATION_NAMESPACE="fleet-b"
        ):
            ids = [f.id for f in self._run()]
        assert "stapel_core.revocation.W003" in ids

    def test_unknown_alias_with_a_default_warns(self):
        with override_settings(
            CACHES=AUTH_SERVICE, STAPEL_JWT_REVOCATION_CACHE="nope"
        ):
            ids = [f.id for f in self._run()]
        assert "stapel_core.revocation.W004" in ids

    def test_unknown_alias_with_no_default_is_an_error(self):
        with override_settings(
            CACHES={"other": {"BACKEND": LOCMEM}},
            STAPEL_JWT_REVOCATION_CACHE="nope",
        ):
            findings = self._run()
        assert [f.id for f in findings] == ["stapel_core.revocation.E001"]
        assert findings[0].level >= 40  # ERROR

    def test_the_namespace_warning_cannot_be_silently_muted(self):
        """It is declared security-critical, like the fail-open hatch."""
        from stapel_core.django.check_guard import is_security_critical

        assert is_security_critical("stapel_core.revocation.W003")


# ---------------------------------------------------------------------------
# Defect 4 — the verifying/refresh seam trusted claims unless the caller
# remembered to ask it not to.
#
# 0.38.0 fixed core's own refresh view by passing `load_user_by_uid`. That
# leaves every CONSUMER call site — a product's own refresh endpoint, a
# management command, a token-exchange view — still defaulting to "re-mint
# from the token's own claims", which are as old as the refresh token (7 days
# by default). A safe behaviour each caller must remember is not a safe
# behaviour.
# ---------------------------------------------------------------------------

@pytest.fixture
def provider():
    from stapel_core.django.jwt.provider import jwt_provider

    jwt_provider.reset()
    yield jwt_provider
    jwt_provider.reset()


@pytest.mark.django_db
class TestRefreshLoaderIsTheDefault:
    def _staff(self, username):
        from django.contrib.auth import get_user_model

        return get_user_model().objects.create(
            username=username, email=f"{username}@example.com", is_staff=True
        )

    def _refresh_for(self, provider, user):
        from stapel_core.django.jwt.utils import serialize_user_to_jwt_data

        _, refresh = provider.create_tokens_from_data(serialize_user_to_jwt_data(user))
        return refresh

    def test_caller_that_passes_nothing_still_re_reads_the_database(self, provider):
        from django.contrib.auth import get_user_model

        user = self._staff("default-loader")
        refresh = self._refresh_for(provider, user)
        get_user_model().objects.filter(pk=user.pk).update(is_staff=False)

        # Exactly what a consumer's own refresh view looks like.
        new_access = provider.refresh_access_token(refresh)

        assert provider.validate_token(new_access)["is_staff"] is False

    def test_caller_that_passes_nothing_refuses_a_deactivated_user(self, provider):
        from django.contrib.auth import get_user_model

        user = self._staff("default-loader-off")
        refresh = self._refresh_for(provider, user)
        get_user_model().objects.filter(pk=user.pk).update(is_active=False)

        assert provider.refresh_access_token(refresh) is None

    def test_caller_that_passes_nothing_refuses_a_deleted_user(self, provider):
        from django.contrib.auth import get_user_model

        user = self._staff("default-loader-gone")
        refresh = self._refresh_for(provider, user)
        get_user_model().objects.filter(pk=user.pk).delete()

        assert provider.refresh_access_token(refresh) is None

    def test_explicit_none_is_still_the_documented_stale_claims_mode(self, provider):
        """`None` keeps its meaning; it just has to be typed out now."""
        from django.contrib.auth import get_user_model

        user = self._staff("explicit-none")
        refresh = self._refresh_for(provider, user)
        get_user_model().objects.filter(pk=user.pk).update(is_staff=False)

        new_access = provider.refresh_access_token(refresh, None)

        assert provider.validate_token(new_access)["is_staff"] is True

    def test_an_explicit_loader_still_wins(self, provider):
        user = self._staff("explicit-loader")
        refresh = self._refresh_for(provider, user)
        seen = []

        def loader(uid):
            seen.append(uid)
            return {"user_id": uid, "email": "x@example.com", "is_staff": False}

        provider.refresh_access_token(refresh, loader)
        assert seen == [str(user.pk)]
