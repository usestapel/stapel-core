"""Revocation lives inside validation, so "valid" cannot mean "except revoked".

Before 0.25.0 the blacklist was a second step every caller had to remember.
Two whole populations did not: ``JWTAuthBackend`` (which ironmemo wires as a
Django auth backend) and every direct caller of ``jwt_provider.validate_token``.
A user who logged out kept authenticating on those paths until the token
expired on its own — up to the full access-token lifetime, and on the refresh
path up to a week.

These tests pin the seam, not the callers: the manager refuses a revoked jti,
the provider adds the user-level ban, and the backend is asserted fixed
WITHOUT being patched — it inherits through the provider. That last one is the
regression test proper. Patching the backend directly would have left the next
``validate_token`` caller to reopen the hole.
"""
import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.test import override_settings

from stapel_core.core.config import JWTConfig
from stapel_core.core.token_blacklist import TokenBlacklist
from stapel_core.core.token_manager import TokenManager

HS256 = JWTConfig(algorithm="HS256", secret_key="revocation-seam-secret")
USER = {"user_id": "u1", "email": "u1@example.com"}

LONG = timedelta(hours=1)
IS_USER_BLACKLISTED = "stapel_core.django.jwt.authentication.is_user_blacklisted"


# ---------------------------------------------------------------------------
# Layer 1: TokenManager — it always held the blacklist; now it runs it.
# ---------------------------------------------------------------------------

class TestManagerRefusesRevokedTokens:
    def setup_method(self):
        self.blacklist = TokenBlacklist(key_prefix="seam_test")
        self.manager = TokenManager(HS256, blacklist=self.blacklist)

    def _revoke(self, token):
        jti = self.manager.get_token_jti(token)
        assert jti, "test token must carry a jti or it pins nothing"
        self.blacklist.blacklist_token(jti, LONG)
        return jti

    def test_revoked_access_token_is_invalid(self):
        access, _ = self.manager.create_tokens(USER)
        assert self.manager.validate_access_token(access) is not None
        self._revoke(access)
        assert self.manager.validate_access_token(access) is None

    def test_revoked_refresh_token_is_invalid(self):
        _, refresh = self.manager.create_tokens(USER)
        assert self.manager.validate_refresh_token(refresh) is not None
        self._revoke(refresh)
        assert self.manager.validate_refresh_token(refresh) is None

    def test_revoked_refresh_token_mints_nothing(self):
        """The refresh path is the long-lived one: a week of resurrection."""
        _, refresh = self.manager.create_tokens(USER)
        self._revoke(refresh)
        assert self.manager.refresh_access_token(refresh) is None

    def test_revoking_one_token_does_not_revoke_the_other(self):
        """Per-jti, not per-user: the manager has no concept of a user ban."""
        access, refresh = self.manager.create_tokens(USER)
        self._revoke(access)
        assert self.manager.validate_access_token(access) is None
        assert self.manager.validate_refresh_token(refresh) is not None

    def test_valid_token_still_validates(self):
        """The gate must reject the revoked, not everything."""
        access, refresh = self.manager.create_tokens(USER)
        assert self.manager.validate_access_token(access)["user_id"] == "u1"
        assert self.manager.validate_refresh_token(refresh)["user_id"] == "u1"

    def test_store_down_fails_closed(self):
        access, _ = self.manager.create_tokens(USER)
        with patch("django.core.cache.cache.get", side_effect=RuntimeError("down")):
            assert self.manager.validate_access_token(access) is None

    @override_settings(STAPEL_BLACKLIST_FAIL_OPEN=True)
    def test_store_down_fail_open_hatch_admits(self):
        access, _ = self.manager.create_tokens(USER)
        with patch("django.core.cache.cache.get", side_effect=RuntimeError("down")):
            assert self.manager.validate_access_token(access) is not None


class TestManagerWithoutBlacklistIsUnchanged:
    """A manager built with no blacklist keeps its old behaviour exactly.

    stapel-core is a library: constructing ``TokenManager(config)`` with no
    store is legal and must not start touching the Django cache.
    """

    def setup_method(self):
        self.manager = TokenManager(HS256)

    def test_validates_without_consulting_any_store(self):
        access, refresh = self.manager.create_tokens(USER)
        with patch("django.core.cache.cache.get", side_effect=AssertionError(
            "no blacklist injected — the cache must not be touched"
        )):
            assert self.manager.validate_access_token(access) is not None
            assert self.manager.validate_refresh_token(refresh) is not None


def test_token_without_jti_is_not_treated_as_revoked():
    """No jti means "not individually revocable", not "reject everything"."""
    blacklist = TokenBlacklist(key_prefix="seam_nojti")
    manager = TokenManager(HS256, blacklist=blacklist)
    assert manager._revoked({"user_id": "u1"}) is False


def test_revocation_reads_the_verified_payload_not_an_unverified_decode():
    """The jti looked up comes from the signature-verified payload.

    ``get_token_jti`` decodes with ``verify=False``. Routing revocation
    through it would let a caller present a token whose unverified jti the
    store has never heard of while the verified one is revoked. Sabotaging
    that helper must therefore not blind the gate.
    """
    blacklist = TokenBlacklist(key_prefix="seam_verified")
    manager = TokenManager(HS256, blacklist=blacklist)
    access, _ = manager.create_tokens(USER)
    blacklist.blacklist_token(manager.get_token_jti(access), LONG)

    with patch.object(
        manager, "get_token_jti",
        side_effect=AssertionError("revocation must not use an unverified decode"),
    ):
        assert manager.validate_access_token(access) is None


# ---------------------------------------------------------------------------
# Layer 2: JWTProvider — adds the django-only, user-level ban.
# ---------------------------------------------------------------------------

@pytest.fixture
def provider():
    from stapel_core.django.jwt.provider import jwt_provider

    jwt_provider.reset()
    yield jwt_provider
    jwt_provider.reset()


class TestProviderRefusesRevokedAndBanned:
    def test_revoked_access_token_is_invalid(self, provider):
        access, _ = provider.create_tokens_from_data(USER)
        with patch(IS_USER_BLACKLISTED, return_value=False):
            assert provider.validate_token(access) is not None
            assert provider.blacklist_token(access) is True
            assert provider.validate_token(access) is None

    def test_banned_user_is_refused_even_with_a_pristine_token(self, provider):
        access, _ = provider.create_tokens_from_data(USER)
        with patch(IS_USER_BLACKLISTED, return_value=True):
            assert provider.validate_token(access) is None

    def test_revoked_refresh_token_mints_nothing(self, provider):
        _, refresh = provider.create_tokens_from_data(USER)
        with patch(IS_USER_BLACKLISTED, return_value=False):
            assert provider.refresh_access_token(refresh) is not None
            assert provider.blacklist_token(refresh) is True
            assert provider.refresh_access_token(refresh) is None

    def test_banned_user_gets_no_new_access_token(self, provider):
        """The mint is the operation that outlives the presented credential."""
        _, refresh = provider.create_tokens_from_data(USER)
        with patch(IS_USER_BLACKLISTED, return_value=True):
            assert provider.refresh_access_token(refresh) is None

    def test_clean_token_and_unbanned_user_still_pass(self, provider):
        access, refresh = provider.create_tokens_from_data(USER)
        with patch(IS_USER_BLACKLISTED, return_value=False):
            assert provider.validate_token(access)["user_id"] == "u1"
            assert provider.refresh_access_token(refresh) is not None


# ---------------------------------------------------------------------------
# Layer 3: the population that never checked — asserted through, not patched.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestAuthBackendInheritsTheFix:
    """``JWTAuthBackend`` is deliberately NOT patched; it inherits.

    This is the original defect end to end: ``authenticate(jwt_token=...)``
    with a revoked token used to return a logged-in User.
    """

    def _token_for_a_real_user(self, provider, email):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create(id=uuid.uuid4(), email=email)
        access, _ = provider.create_tokens_from_data(
            {"user_id": str(user.id), "email": email}
        )
        return access

    def test_revoked_token_authenticates_nobody(self, provider):
        from stapel_core.django.jwt.backends import JWTAuthBackend

        backend = JWTAuthBackend()
        access = self._token_for_a_real_user(provider, "backend@example.com")
        with patch(IS_USER_BLACKLISTED, return_value=False):
            assert backend.authenticate(None, jwt_token=access) is not None
            provider.blacklist_token(access)
            assert backend.authenticate(None, jwt_token=access) is None

    def test_banned_user_authenticates_nobody(self, provider):
        from stapel_core.django.jwt.backends import JWTAuthBackend

        backend = JWTAuthBackend()
        access = self._token_for_a_real_user(provider, "backend2@example.com")
        with patch(IS_USER_BLACKLISTED, return_value=True):
            assert backend.authenticate(None, jwt_token=access) is None
