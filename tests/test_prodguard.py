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

from stapel_core.django.prodguard import guard_db_password, guard_secret

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
