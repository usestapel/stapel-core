"""Tests for stapel_core.secrets — provider seam, cache+TTL, fail-closed, env default.

Covers arch-stapel-vault Part 1: the SecretProvider seam in core (env default),
its per-process TTL cache, fail-closed semantics, prodguard compatibility over
the resolved value, and the provider-seam system checks.
"""
import os
from unittest import mock

import pytest
from django.test import override_settings

import stapel_core.secrets as secrets
from stapel_core.secrets import (
    BOOTSTRAP_PROVIDER_ENV,
    EnvSecretProvider,
    SecretProvider,
    SecretUnavailable,
    get_secret,
    invalidate_secret,
)
from stapel_core.secrets.checks import (
    W001_UNIMPORTABLE,
    W002_NOT_A_PROVIDER,
    check_secrets_provider,
)


@pytest.fixture(autouse=True)
def _reset():
    secrets._reset_state()
    yield
    secrets._reset_state()


# --- provider dotted-path resolution ---------------------------------------

class _CountingProvider:
    """Fail-closed provider that records how often it is consulted."""

    calls = 0
    fail_closed = True
    _values = {"API_KEY": "s3cr3t-from-provider"}

    def get(self, name):
        type(self).calls += 1
        return self._values.get(name)


class _DictProvider:
    fail_closed = True

    def __init__(self):
        self.values = {"DJANGO_SECRET_KEY": "vault-value"}

    def get(self, name):
        return self.values.get(name)


def setup_function(_):
    _CountingProvider.calls = 0


def test_default_provider_is_env():
    # Nothing configured -> EnvSecretProvider, reading os.environ.
    assert isinstance(secrets._resolve_provider(), EnvSecretProvider)


def test_env_default_transparent(monkeypatch):
    monkeypatch.setenv("SOME_TOKEN", "env-token-value")
    assert get_secret("SOME_TOKEN") == "env-token-value"


def test_env_missing_with_default_returns_default(monkeypatch):
    monkeypatch.delenv("DEFINITELY_ABSENT", raising=False)
    assert get_secret("DEFINITELY_ABSENT", "fallback") == "fallback"


def test_env_missing_without_default_is_none_not_raise(monkeypatch):
    # The env provider is fail_closed = False: os.environ.get semantics.
    monkeypatch.delenv("DEFINITELY_ABSENT", raising=False)
    assert get_secret("DEFINITELY_ABSENT") is None


def test_provider_resolved_by_dotted_path():
    with override_settings(
        STAPEL_SECRETS={"PROVIDER": "tests.test_secrets._DictProvider"}
    ):
        assert get_secret("DJANGO_SECRET_KEY") == "vault-value"


def test_provider_from_class_instance_and_shape():
    # A provider is anything with a callable get(name); duck-typed, no import.
    assert isinstance(EnvSecretProvider(), SecretProvider)


# --- fail-closed semantics --------------------------------------------------

def test_fail_closed_missing_secret_raises():
    with override_settings(
        STAPEL_SECRETS={"PROVIDER": "tests.test_secrets._CountingProvider"}
    ):
        with pytest.raises(SecretUnavailable) as exc:
            get_secret("NOT_IN_VAULT")
        assert "NOT_IN_VAULT" in str(exc.value)
        assert exc.value.provider == "_CountingProvider"


def test_fail_closed_missing_secret_with_default_returns_default():
    with override_settings(
        STAPEL_SECRETS={"PROVIDER": "tests.test_secrets._CountingProvider"}
    ):
        assert get_secret("NOT_IN_VAULT", "d") == "d"


def test_fail_closed_present_secret_returned():
    with override_settings(
        STAPEL_SECRETS={"PROVIDER": "tests.test_secrets._CountingProvider"}
    ):
        assert get_secret("API_KEY") == "s3cr3t-from-provider"


# --- per-process cache + TTL ------------------------------------------------

def test_cache_hits_within_ttl():
    with override_settings(
        STAPEL_SECRETS={
            "PROVIDER": "tests.test_secrets._CountingProvider",
            "CACHE_TTL": 300,
        }
    ):
        assert get_secret("API_KEY") == "s3cr3t-from-provider"
        assert get_secret("API_KEY") == "s3cr3t-from-provider"
        assert _CountingProvider.calls == 1  # second read served from cache


def test_cache_expires_after_ttl():
    with override_settings(
        STAPEL_SECRETS={
            "PROVIDER": "tests.test_secrets._CountingProvider",
            "CACHE_TTL": 10,
        }
    ):
        with mock.patch.object(secrets.time, "monotonic", return_value=1000.0):
            assert get_secret("API_KEY") == "s3cr3t-from-provider"
        # Advance past the TTL -> provider is consulted again (rotation re-read).
        with mock.patch.object(secrets.time, "monotonic", return_value=1011.0):
            assert get_secret("API_KEY") == "s3cr3t-from-provider"
        assert _CountingProvider.calls == 2


def test_invalidate_secret_forces_reread():
    with override_settings(
        STAPEL_SECRETS={
            "PROVIDER": "tests.test_secrets._CountingProvider",
            "CACHE_TTL": 300,
        }
    ):
        get_secret("API_KEY")
        invalidate_secret("API_KEY")
        get_secret("API_KEY")
        assert _CountingProvider.calls == 2


def test_cache_ttl_zero_disables_cache():
    with override_settings(
        STAPEL_SECRETS={
            "PROVIDER": "tests.test_secrets._CountingProvider",
            "CACHE_TTL": 0,
        }
    ):
        get_secret("API_KEY")
        get_secret("API_KEY")
        assert _CountingProvider.calls == 2


def test_missing_secret_is_not_negative_cached():
    with override_settings(
        STAPEL_SECRETS={
            "PROVIDER": "tests.test_secrets._CountingProvider",
            "CACHE_TTL": 300,
        }
    ):
        assert get_secret("LATER", "d") == "d"
        # Now the secret "appears" — a miss must not have been cached.
        _CountingProvider._values["LATER"] = "now-present"
        try:
            assert get_secret("LATER") == "now-present"
        finally:
            _CountingProvider._values.pop("LATER", None)


# --- bootstrap provider override (settings not yet configured) -------------

def test_bootstrap_env_provider_override(monkeypatch):
    # Simulate the pre-django.setup() path: _provider_dotted_path falls back to
    # the STAPEL_SECRETS_PROVIDER env var when the namespace read raises.
    monkeypatch.setenv(BOOTSTRAP_PROVIDER_ENV, "tests.test_secrets._DictProvider")
    with mock.patch.object(
        secrets, "_provider_spec",
        new=lambda: os.environ.get(BOOTSTRAP_PROVIDER_ENV),
    ):
        assert isinstance(secrets._resolve_provider(), _DictProvider)


# --- prodguard operates over the resolved value ----------------------------

def test_prodguard_compat_over_resolved_secret():
    from django.core.exceptions import ImproperlyConfigured

    from stapel_core.django.prodguard import guard_secret

    class _PlaceholderVault:
        fail_closed = True

        def get(self, name):
            return "change_me_to_a_long_random_string"

    with override_settings(STAPEL_SECRETS={"PROVIDER": _PlaceholderVault()}):
        with pytest.raises(ImproperlyConfigured):
            guard_secret("SECRET_KEY", get_secret("SECRET_KEY"))

    class _GoodVault:
        fail_closed = True

        def get(self, name):
            return "x" * 64

    invalidate_secret()
    with override_settings(STAPEL_SECRETS={"PROVIDER": _GoodVault()}):
        guard_secret("SECRET_KEY", get_secret("SECRET_KEY"))  # no raise


# --- system checks ----------------------------------------------------------

def test_check_ok_for_env_default():
    assert check_secrets_provider() == []


def test_check_warns_unimportable_provider():
    with override_settings(STAPEL_SECRETS={"PROVIDER": "no.such.module.Provider"}):
        results = check_secrets_provider()
    assert [w.id for w in results] == [W001_UNIMPORTABLE]


def test_check_warns_non_provider():
    with override_settings(STAPEL_SECRETS={"PROVIDER": "os.getcwd"}):
        results = check_secrets_provider()
    assert [w.id for w in results] == [W002_NOT_A_PROVIDER]


def test_invalid_provider_shape_raises_typeerror():
    with override_settings(STAPEL_SECRETS={"PROVIDER": "os.getcwd"}):
        with pytest.raises(TypeError):
            get_secret("ANYTHING")
