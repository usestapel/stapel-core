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

Relationship to the secret-provider seam (``stapel_core.secrets``): these
guards operate on the **resolved** value, wherever it came from. Whether
``SECRET_KEY`` was read from the environment (default provider) or from Vault
(``stapel_vault.VaultSecretProvider``), the canonical prod call is the same::

    from stapel_core.secrets import get_secret

    guard_secret("SECRET_KEY", get_secret("SECRET_KEY"))

So a Vault that hands back a shipped placeholder, a too-short value, or
nothing is caught exactly like a bad env var — the guard needs no knowledge
of the provider. (A fail-closed provider raises ``SecretUnavailable`` for a
missing secret before the guard even runs; the guard still covers the
"present but placeholder/short" case for every provider.)
"""
from __future__ import annotations

from stapel_core.django.check_guard import declare_security_critical

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


#: Cookie flags that carry a bearer credential and must never travel in
#: cleartext. There is no legitimate production reason for any of these to be
#: off, so the guard offers no escape for them.
_TLS_ONLY_COOKIE_FLAGS = (
    "SESSION_COOKIE_SECURE",
    "CSRF_COOKIE_SECURE",
    "JWT_COOKIE_SECURE",
)


def guard_cookie_security(namespace: dict) -> None:
    """Refuse to boot a production tier that serves credentials over cleartext.

    Same genre as :func:`guard_secret` — the "deployed as downloaded" mistake,
    not a routine speed bump. `SECRET_KEY` was already guarded here; the
    transport the resulting session cookie travels on was not, which made the
    guarded half of the pair the less interesting one.

    Call it from the prod settings tier, after the settings it inspects::

        from stapel_core.django.prodguard import guard_cookie_security

        guard_cookie_security(globals())

    What it requires:

    - ``SESSION_COOKIE_SECURE`` / ``CSRF_COOKIE_SECURE`` / ``JWT_COOKIE_SECURE``
      on. These are bearer credentials; no escape hatch is offered.
    - ``SECURE_SSL_REDIRECT`` on and ``SECURE_HSTS_SECONDS`` non-zero, so a
      first plain-HTTP request is upgraded rather than answered. A deployment
      whose edge already does both states that with
      ``STAPEL_TLS_TERMINATED_UPSTREAM = True`` — the safe value is the
      default, and the opt-out is a sentence someone had to write.
    - ``SECURE_PROXY_SSL_HEADER`` set only where the deployment has stated it
      is behind a proxy that overwrites the header
      (``STAPEL_TRUST_PROXY_SSL_HEADER``). Trusting a client-settable header
      lets a caller claim HTTPS, which makes every other check here decorative.

    Raises:
        django.core.exceptions.ImproperlyConfigured: listing *every* problem
            found, not just the first — a boot guard the operator has to run
            four times is a guard they will stop running.
    """
    from django.core.exceptions import ImproperlyConfigured

    problems: list[str] = []

    for flag in _TLS_ONLY_COOKIE_FLAGS:
        if not namespace.get(flag, False):
            problems.append(
                f"{flag} is off — the cookie it controls is a bearer "
                f"credential and would be sent over plain HTTP."
            )

    if not namespace.get("STAPEL_TLS_TERMINATED_UPSTREAM", False):
        if not namespace.get("SECURE_SSL_REDIRECT", False):
            problems.append(
                "SECURE_SSL_REDIRECT is off, so a plain-HTTP request is "
                "answered instead of upgraded."
            )
        if not namespace.get("SECURE_HSTS_SECONDS", 0):
            problems.append(
                "SECURE_HSTS_SECONDS is 0, so a browser will try plain HTTP "
                "again on the next visit."
            )

    if namespace.get("SECURE_PROXY_SSL_HEADER") and not namespace.get(
        "STAPEL_TRUST_PROXY_SSL_HEADER", False
    ):
        problems.append(
            "SECURE_PROXY_SSL_HEADER is set but STAPEL_TRUST_PROXY_SSL_HEADER "
            "is not. X-Forwarded-Proto is a request header any client can "
            "send; trusting it without a proxy that overwrites it lets a "
            "caller declare its own connection secure."
        )

    if problems:
        raise ImproperlyConfigured(
            "Production TLS configuration is incomplete:\n  - "
            + "\n  - ".join(problems)
            + "\n\nSet the flags above. If TLS is terminated at an edge that "
            "already redirects and sends HSTS, set "
            "STAPEL_TLS_TERMINATED_UPSTREAM = True to say so explicitly. If "
            "that edge also overwrites X-Forwarded-Proto, set "
            "STAPEL_TRUST_PROXY_SSL_HEADER = True."
        )


# ---------------------------------------------------------------------------
# System checks (tag ``stapel_prodguard``) — the guards, run without being called
# ---------------------------------------------------------------------------
#
# Why the functions above were never adopted, precisely: they are *calls a
# settings module has to make*. ``stapel-tools`` writes them into the prod tier
# it generates (``_templates.py``, ``_minimal_templates.py``), so a project
# scaffolded by ``stapel-create-project`` gets them — and every project that
# was not, or that was scaffolded before the template grew them, gets nothing.
# Nothing anywhere detects the absence: no check reads them, no gate asks for
# them, and ``manage.py check`` cannot report an ImproperlyConfigured that a
# settings module never raised. So a six-character SECRET_KEY boots.
#
# The fix is to stop requiring the memory. These checks CALL the same two
# functions — one rule, not a second copy of it — from the check registry every
# project already inherits by having ``stapel_core.django`` in INSTALLED_APPS,
# and the tag rides the boot-gate roster so it reaches gunicorn, where the
# settings-module call would have run and didn't.

_PRODGUARD_SETTING = "STAPEL_PRODGUARD"

#: Extra setting names to hold to :func:`guard_secret`, beyond ``SECRET_KEY``.
#: A list of names, not values — the guard reads the resolved setting itself
#: and nothing here ever logs or reports one.
_PRODGUARD_SECRETS_SETTING = "STAPEL_PRODGUARD_SECRETS"

AUTO, ENFORCE, OFF = "auto", "enforce", "off"

#: Both ids ARE their security-critical declarations (``check_guard``): the
#: marking travels with the constant, so no blanket SILENCED_SYSTEM_CHECKS
#: line can mute them and no separate list can drift away from them.
E001_WEAK_SECRET = declare_security_critical(
    "stapel_core.prodguard.E001",
    "a placeholder or short SECRET_KEY forges every session cookie and signed "
    "token this service issues",
)
E002_WEAK_DB_PASSWORD = declare_security_critical(
    "stapel_core.prodguard.E002",
    "the shipped default database password is public knowledge",
)
W001_PRODGUARD_OFF = "stapel_core.prodguard.W001"


def _under_test_runner() -> bool:
    """Is this process a test run rather than a deployment?

    A package's own test suite configures a short, obviously-fake SECRET_KEY
    and no DEBUG — which is production-shaped to every signal Django exposes.
    Enforcing there would turn every library's CI red over a value that is
    correct for it, and a check that floods gets silenced wholesale on day one.
    A deployed gunicorn worker does not import pytest.
    """
    import sys

    return "pytest" in sys.modules or sys.argv[1:2] == ["test"]


def prodguard_mode() -> str:
    """``"enforce"`` | ``"off"``, after resolving :data:`_PRODGUARD_SETTING`.

    ``"auto"`` (the default) enforces in any process that is not running with
    ``DEBUG`` on and is not a test run — i.e. every real deployment, with no
    settings edit and nothing for a product to remember. An unreadable value
    means ``auto``: a typo in the switch must not silently disable the guard.
    """
    from django.conf import settings

    raw = str(getattr(settings, _PRODGUARD_SETTING, AUTO) or AUTO).strip().lower()
    if raw in (ENFORCE, OFF):
        return raw
    if getattr(settings, "DEBUG", False):
        return OFF
    if _under_test_runner():
        return OFF
    return ENFORCE


def _guarded_secret_names() -> list[str]:
    from django.conf import settings

    extra = getattr(settings, _PRODGUARD_SECRETS_SETTING, None) or ()
    names = ["SECRET_KEY"]
    if isinstance(extra, (list, tuple, set)):
        names.extend(str(name) for name in extra if str(name) != "SECRET_KEY")
    return names


#: Backends that hold no credential to be weak. SQLite authenticates with the
#: filesystem; Django's `dummy` backend cannot open a connection at all, which
#: is what a boot-smoke tier configures on purpose to prove that loading an
#: app needs no database. Asking either for a password is asking a question
#: that has no subject — a different thing from a deployment shipping the
#: public default, which is what E002 exists to stop.
_PASSWORDLESS_DB_ENGINES = ("sqlite", "dummy")


def _default_db_wants_a_password() -> bool:
    """Only engines that authenticate with one — see _PASSWORDLESS_DB_ENGINES."""
    from django.conf import settings

    default = (getattr(settings, "DATABASES", None) or {}).get("default") or {}
    engine = str(default.get("ENGINE", ""))
    return bool(engine) and not any(e in engine for e in _PASSWORDLESS_DB_ENGINES)


def check_production_secrets(app_configs=None, **kwargs):
    """E001/E002/W001 — run the prod guards from the check registry.

    Findings carry the guard's own message verbatim; none of them carries a
    value. ``guard_secret`` reports "empty or a known placeholder" or a length,
    which is everything an operator needs and nothing an attacker does.
    """
    from django.conf import settings
    from django.core import checks
    from django.core.exceptions import ImproperlyConfigured

    from stapel_core.django.check_guard import SecurityCriticalError

    mode = prodguard_mode()
    if mode == OFF:
        if str(getattr(settings, _PRODGUARD_SETTING, AUTO)).strip().lower() != OFF:
            return []
        return [checks.Warning(
            f"{_PRODGUARD_SETTING}='off': the production secret guards do not "
            f"run in this deployment. A placeholder or six-character "
            f"SECRET_KEY boots without complaint.",
            hint=f"Remove {_PRODGUARD_SETTING} (or set it to 'auto', the "
                 f"default) once the values are real. Turning a gate down is "
                 f"a legitimate stance; leaving it down silently is not, which "
                 f"is why this reports itself at every boot.",
            id=W001_PRODGUARD_OFF,
        )]

    findings = []
    for name in _guarded_secret_names():
        try:
            guard_secret(name, getattr(settings, name, None))
        except ImproperlyConfigured as exc:
            findings.append(SecurityCriticalError(
                str(exc),
                hint="Generate a real value into the environment. "
                     "stapel-create-project writes one; "
                     "`python -c \"import secrets; print(secrets.token_urlsafe(48))\"` "
                     "is the manual equivalent.",
                id=E001_WEAK_SECRET,
            ))

    if _default_db_wants_a_password():
        password = (
            (getattr(settings, "DATABASES", None) or {}).get("default") or {}
        ).get("PASSWORD")
        try:
            guard_db_password(password)
        except ImproperlyConfigured as exc:
            findings.append(SecurityCriticalError(
                str(exc),
                hint="Set POSTGRES_PASSWORD (or the DATABASES['default'] "
                     "PASSWORD this deployment builds from it) to a generated "
                     "value. The library default exists for local Docker "
                     "Compose, which is not reachable from a network.",
                id=E002_WEAK_DB_PASSWORD,
            ))
    return findings


def register_checks() -> None:
    """Register :func:`check_production_secrets` under ``stapel_prodguard``.

    A function, not an import-time decorator: ``stapel-tools`` imports this
    module to read ``_PLACEHOLDER_PREFIXES`` and a prod settings tier imports
    it to call the guards, and neither should acquire a registered check as a
    side effect. ``CommonDjangoConfig.ready()`` calls this.
    """
    from django.core import checks

    checks.register("stapel_prodguard")(check_production_secrets)


__all__ = [
    "AUTO",
    "ENFORCE",
    "E001_WEAK_SECRET",
    "E002_WEAK_DB_PASSWORD",
    "MIN_SECRET_LENGTH",
    "OFF",
    "W001_PRODGUARD_OFF",
    "check_production_secrets",
    "guard_secret",
    "guard_db_password",
    "guard_cookie_security",
    "prodguard_mode",
    "register_checks",
]
