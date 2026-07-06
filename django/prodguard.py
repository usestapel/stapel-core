"""Production configuration guard.

Generated Stapel projects (stapel-tools scaffolds) call these from their
``core/settings/prod.py`` tier (or the ``DJANGO_ENV=prod`` branch of the
minimal preset's single settings module) as the last line of defense before
booting with a value copied straight out of ``.env.example`` — the gap
tracked as security-programme.md B2/B6: the previous guard only rejected an
empty ``SECRET_KEY`` or one starting with ``django-insecure-``, so a shipped
placeholder like ``change_me_to_a_long_random_string`` or the default
``POSTGRES_PASSWORD=stapel``/``change_me`` sailed straight through into a
live deployment.

``stapel-create-project`` (SEC-6) writes fresh random values into ``.env``
at generation time specifically so these guards never fire for a project
that was actually configured — they exist for the "deployed as downloaded"
operator mistake, not as a routine speed bump.

Usage (prod settings tier)::

    from stapel_core.django.prodguard import guard_db_password, guard_secret

    guard_secret("SECRET_KEY", SECRET_KEY)
    guard_secret("JWT_SECRET_KEY", JWT_SECRET_KEY)
    guard_db_password(DATABASES["default"].get("PASSWORD"))

Both functions raise ``django.core.exceptions.ImproperlyConfigured``, which
Django surfaces as a hard startup failure (fail-closed, matching the
project's other prod-only checks).
"""
from __future__ import annotations

MIN_SECRET_LENGTH = 50

# Prefixes that mark a value as a known template placeholder rather than a
# real secret. Matched case-insensitively against the *start* of the value so
# both shipped placeholders (`change_me_to_a_long_random_string`,
# `change_me_to_another_long_random_string`) and the legacy dev-only fallback
# (`django-insecure-*`) are caught, along with any future `change_me*`
# variant a template adds without needing a guard update.
_PLACEHOLDER_PREFIXES = (
    "django-insecure-",
    "change_me",
    "changeme",
)

# Exact-match placeholder/default values for credentials that aren't
# generated `SECRET_KEY`-shaped strings (B6): the library's dev-only
# Postgres fallback and the pre-SEC-6 `.env.example` placeholder.
_PLACEHOLDER_DB_PASSWORDS = frozenset({"stapel", "change_me", "changeme", ""})


def _is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return any(lowered.startswith(prefix) for prefix in _PLACEHOLDER_PREFIXES)


def guard_secret(name: str, value: str | None, *, min_length: int = MIN_SECRET_LENGTH) -> None:
    """Refuse to boot on a missing, placeholder, or too-short secret.

    Args:
        name: setting name, used only in the error message (e.g. "SECRET_KEY").
        value: the resolved value (from env or settings) to check.
        min_length: minimum acceptable length once the placeholder check
            passes — the shipped generators write 64-character random
            secrets (SEC-6); 50 leaves headroom for a hand-picked value
            while still ruling out short/guessable strings.

    Raises:
        django.core.exceptions.ImproperlyConfigured: if *value* is empty,
            matches a known placeholder, or is shorter than *min_length*.
    """
    from django.core.exceptions import ImproperlyConfigured

    value = value or ""
    if not value or _is_placeholder(value):
        raise ImproperlyConfigured(
            f"{name} is empty or a known placeholder value. Set a real, "
            f"randomly generated secret in the environment before starting "
            f"in production (stapel-create-project writes one into .env "
            f"automatically — see security-programme.md SEC-6)."
        )
    if len(value) < min_length:
        raise ImproperlyConfigured(
            f"{name} is only {len(value)} characters long. Production "
            f"secrets must be at least {min_length} characters."
        )


def guard_db_password(password: str | None) -> None:
    """Refuse to boot on the shipped default/placeholder Postgres password.

    The dev-only library default (``stapel``) and the pre-SEC-6
    ``.env.example`` placeholder (``change_me``) are both fine for local
    Docker Compose (no network exposure); neither is acceptable once
    ``DJANGO_ENV=prod``.

    Raises:
        django.core.exceptions.ImproperlyConfigured: if *password* is one of
            the known defaults/placeholders (or missing).
    """
    from django.core.exceptions import ImproperlyConfigured

    value = (password or "").strip().lower()
    if value in _PLACEHOLDER_DB_PASSWORDS:
        raise ImproperlyConfigured(
            "POSTGRES_PASSWORD is a default or placeholder value. Set a "
            "real, randomly generated password in the environment before "
            "starting in production (stapel-create-project writes one into "
            ".env automatically — see security-programme.md SEC-6)."
        )


__all__ = ["MIN_SECRET_LENGTH", "guard_secret", "guard_db_password"]
