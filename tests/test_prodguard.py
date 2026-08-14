"""Tests for stapel_core.django.prodguard (security-programme.md SEC-4/B2/B6).

The previous ad-hoc prod-guard (inline in the stapel-tools prod.py template)
only rejected an empty SECRET_KEY or one starting with "django-insecure-" —
a shipped `.env.example` placeholder like `change_me_to_a_long_random_string`
sailed straight through. These tests pin the hardened behavior: known
placeholders, too-short secrets, and the default/placeholder DB password are
all rejected; a real generated secret (SEC-6: `secrets.token_urlsafe`-style,
64 chars) passes.
"""
import pytest
from django.core.exceptions import ImproperlyConfigured

from stapel_core.django.prodguard import (
    guard_cookie_security,
    guard_db_password,
    guard_secret,
)

# A stand-in for what stapel-create-project actually writes into .env
# (64 letters/digits — see stapel-tools create_project._random_secret).
REAL_SECRET = "aB3" * 20  # 60 chars, alnum only, no placeholder prefix


class TestGuardSecret:
    def test_rejects_empty(self):
        with pytest.raises(ImproperlyConfigured, match="placeholder"):
            guard_secret("SECRET_KEY", "")

    def test_rejects_none(self):
        with pytest.raises(ImproperlyConfigured, match="placeholder"):
            guard_secret("SECRET_KEY", None)

    def test_rejects_legacy_django_insecure_prefix(self):
        with pytest.raises(ImproperlyConfigured, match="placeholder"):
            guard_secret("SECRET_KEY", "django-insecure-whatever")

    def test_rejects_shipped_change_me_placeholder(self):
        with pytest.raises(ImproperlyConfigured, match="placeholder"):
            guard_secret("SECRET_KEY", "change_me_to_a_long_random_string")

    def test_rejects_shipped_change_me_placeholder_jwt_variant(self):
        with pytest.raises(ImproperlyConfigured, match="placeholder"):
            guard_secret("JWT_SECRET_KEY", "change_me_to_another_long_random_string")

    def test_rejects_changeme_no_underscore(self):
        with pytest.raises(ImproperlyConfigured, match="placeholder"):
            guard_secret("SECRET_KEY", "ChangeMe123")

    def test_rejects_too_short_real_looking_value(self):
        # Not a placeholder, but well under the 50-char floor.
        with pytest.raises(ImproperlyConfigured, match="characters"):
            guard_secret("SECRET_KEY", "a-real-but-short-secret-value")

    def test_accepts_generated_secret(self):
        secret = REAL_SECRET + "cD4e"  # pad to >=50 chars
        assert len(secret) >= 50
        guard_secret("SECRET_KEY", secret)

    def test_custom_min_length_is_honored(self):
        guard_secret("SECRET_KEY", "short-but-allowed", min_length=5)


class TestGuardDbPassword:
    def test_rejects_library_dev_default(self):
        with pytest.raises(ImproperlyConfigured, match="POSTGRES_PASSWORD"):
            guard_db_password("stapel")

    def test_rejects_pre_sec6_placeholder(self):
        with pytest.raises(ImproperlyConfigured, match="POSTGRES_PASSWORD"):
            guard_db_password("change_me")

    def test_rejects_empty(self):
        with pytest.raises(ImproperlyConfigured, match="POSTGRES_PASSWORD"):
            guard_db_password("")

    def test_rejects_none(self):
        with pytest.raises(ImproperlyConfigured, match="POSTGRES_PASSWORD"):
            guard_db_password(None)

    def test_is_case_insensitive(self):
        with pytest.raises(ImproperlyConfigured, match="POSTGRES_PASSWORD"):
            guard_db_password("STAPEL")

    def test_accepts_generated_password(self):
        guard_db_password("kX9mQ2vN8pL4rT6wZ1yB")


# ---------------------------------------------------------------------------
# guard_cookie_security — the transport the guarded SECRET_KEY's cookie rides
# ---------------------------------------------------------------------------

def _hardened(**overrides) -> dict:
    """A prod settings namespace with every TLS flag closed."""
    namespace = {
        "SESSION_COOKIE_SECURE": True,
        "CSRF_COOKIE_SECURE": True,
        "JWT_COOKIE_SECURE": True,
        "SECURE_SSL_REDIRECT": True,
        "SECURE_HSTS_SECONDS": 31536000,
    }
    namespace.update(overrides)
    return namespace


class TestGuardCookieSecurity:
    def test_accepts_a_hardened_namespace(self):
        guard_cookie_security(_hardened())

    @pytest.mark.parametrize(
        "flag", ["SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE", "JWT_COOKIE_SECURE"]
    )
    def test_rejects_each_cleartext_cookie_flag(self, flag):
        with pytest.raises(ImproperlyConfigured, match=flag):
            guard_cookie_security(_hardened(**{flag: False}))

    def test_rejects_a_namespace_that_declares_nothing(self):
        """The "deployed as downloaded" shape this whole module exists for."""
        with pytest.raises(ImproperlyConfigured) as exc:
            guard_cookie_security({})
        message = str(exc.value)
        for flag in ("SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE", "JWT_COOKIE_SECURE"):
            assert flag in message

    def test_reports_every_problem_at_once(self):
        """A guard the operator has to run four times is one they stop running."""
        with pytest.raises(ImproperlyConfigured) as exc:
            guard_cookie_security(
                _hardened(SESSION_COOKIE_SECURE=False, JWT_COOKIE_SECURE=False)
            )
        assert "SESSION_COOKIE_SECURE" in str(exc.value)
        assert "JWT_COOKIE_SECURE" in str(exc.value)

    def test_requires_redirect_and_hsts(self):
        with pytest.raises(ImproperlyConfigured, match="SECURE_SSL_REDIRECT"):
            guard_cookie_security(_hardened(SECURE_SSL_REDIRECT=False))
        with pytest.raises(ImproperlyConfigured, match="SECURE_HSTS_SECONDS"):
            guard_cookie_security(_hardened(SECURE_HSTS_SECONDS=0))

    def test_upstream_termination_is_an_explicit_statement(self):
        """An edge that already redirects and sends HSTS is a real shape — but
        it has to be claimed, not assumed by omission."""
        guard_cookie_security(_hardened(
            SECURE_SSL_REDIRECT=False,
            SECURE_HSTS_SECONDS=0,
            STAPEL_TLS_TERMINATED_UPSTREAM=True,
        ))

    def test_rejects_untrusted_proxy_header(self):
        """Trusting a client-settable X-Forwarded-Proto makes every other flag
        here decorative — the caller decides what "secure" means."""
        with pytest.raises(ImproperlyConfigured, match="SECURE_PROXY_SSL_HEADER"):
            guard_cookie_security(_hardened(
                SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
            ))

    def test_accepts_proxy_header_when_the_deployment_vouches_for_it(self):
        guard_cookie_security(_hardened(
            SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
            STAPEL_TRUST_PROXY_SSL_HEADER=True,
        ))
