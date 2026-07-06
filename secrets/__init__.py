"""stapel_core.secrets — secret resolution as a core seam.

``get_secret(name, default=…)`` is the single entry point the framework and
generated projects use to read a secret (``SECRET_KEY``, ``JWT_SECRET_KEY``,
a database password, an LLM pool key, …). It resolves the value through a
**provider seam** — ``STAPEL_SECRETS["PROVIDER"]``, a dotted path (or class /
instance) of signature ``get(name) -> str | None`` — exactly like
``AUDIT_SINK`` / ``ROLE_SOURCES`` elsewhere in core.

Design rules (arch-stapel-vault skeleton, decision 2026-07-06 "env для
секретов в проде НЕПРИЕМЛЕМ"):

- **The mechanism lives in core; the backends do not.** The default provider
  is :class:`EnvSecretProvider` — ``os.environ.get``. A bare stapel-core
  project, the ``minimal`` preset and every local dev box therefore behave
  exactly as before: zero new dependencies, zero config, secrets from the
  environment. Pointing ``PROVIDER`` at ``stapel-vault``'s
  ``VaultSecretProvider`` (a separate OSS module) is what moves production
  secret storage off the environment and into OpenBao / HashiCorp Vault.

- **Bootstrap-tolerant.** Production settings modules resolve ``SECRET_KEY``
  *before* Django is configured, so provider selection cannot depend on
  ``django.conf.settings`` being ready. When settings are unavailable
  ``get_secret`` reads the provider from the dedicated bootstrap env var
  ``STAPEL_SECRETS_PROVIDER`` (falling back to the env provider). Once Django
  is configured, ``STAPEL_SECRETS["PROVIDER"]`` wins. The generic key name
  ``PROVIDER`` stays ``no_env`` (a stray ``PROVIDER`` env var must never flip
  which code reads your secrets); the explicit ``STAPEL_SECRETS_PROVIDER`` is
  the intentional bootstrap knob.

- **Per-process cache with TTL.** A resolved value is memoized for
  ``STAPEL_SECRETS["CACHE_TTL"]`` seconds so the hot path never re-hits a
  remote secret store on every request. The TTL is also the rotation
  re-read window: after it elapses ``get_secret`` re-reads the provider, so a
  rotated secret propagates without a restart (stapel-vault pairs this with
  ``invalidate_secret`` for eager rotation — see its MODULE.md).

- **Fail-closed for real backends.** If a provider returns ``None`` (secret
  absent) and no ``default`` was supplied, :class:`SecretUnavailable` is
  raised — a missing production secret must be a hard, loud failure, never a
  silent ``None`` that boots a half-configured service. The env provider is
  the deliberate exception: it is ``fail_closed = False`` so that
  ``get_secret("X")`` with a missing env var and no default returns ``None``,
  preserving the ``os.environ.get`` semantics existing settings modules rely
  on. Supplying a ``default`` short-circuits fail-closed for any provider.

Interaction with prodguard (SEC-4): the guards operate on the *resolved*
value. ``guard_secret("SECRET_KEY", get_secret("SECRET_KEY", ...))`` is the
canonical prod call — whether the value came from the environment or from
Vault, a placeholder/short/empty secret still fails the boot.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Protocol, runtime_checkable

#: Sentinel distinguishing "no default supplied" from ``default=None``.
_UNSET = object()

#: Bootstrap env var: dotted path to the provider, honored when Django
#: settings are not yet configured (prod settings modules resolving
#: ``SECRET_KEY`` before ``django.setup()``). Deliberately explicit — unlike
#: the generic ``PROVIDER`` key, this name cannot collide with an unrelated
#: env var.
BOOTSTRAP_PROVIDER_ENV = "STAPEL_SECRETS_PROVIDER"

#: Fallback provider dotted path when nothing is configured anywhere.
_DEFAULT_PROVIDER = "stapel_core.secrets.EnvSecretProvider"

#: Fallback cache TTL (seconds) when Django settings are not yet readable.
_DEFAULT_CACHE_TTL = 300


class SecretUnavailable(Exception):
    """A required secret was not found and no default was supplied.

    Raised only for fail-closed providers (everything except the env
    default): a production secret store that cannot produce ``name`` is a
    boot-stopping error, not a silent ``None``.
    """

    def __init__(self, name: str, provider: str | None = None) -> None:
        self.name = name
        self.provider = provider
        detail = f" (provider {provider})" if provider else ""
        super().__init__(
            f"secret {name!r} is unavailable{detail} and no default was "
            f"supplied. A fail-closed provider must not silently return None "
            f"for a missing secret."
        )


@runtime_checkable
class SecretProvider(Protocol):
    """The secret-provider duck type (dotted path in ``STAPEL_SECRETS``).

    ``get(name)`` returns the secret value or ``None`` when the provider has
    no value for that name. A provider MAY expose ``fail_closed`` (default
    ``True`` when the attribute is absent): when ``False``, a missing secret
    with no caller default resolves to ``None`` instead of raising
    :class:`SecretUnavailable` — this is how :class:`EnvSecretProvider`
    preserves ``os.environ.get`` semantics.

    A provider is free to namespace/map ``name`` internally (stapel-vault
    maps ``DJANGO_SECRET_KEY`` onto a KV mount/path/key); core passes the
    logical name through unchanged.
    """

    def get(self, name: str) -> str | None: ...


class EnvSecretProvider:
    """Default provider: read secrets from ``os.environ``.

    ``fail_closed = False`` — a missing env var with no caller default
    resolves to ``None`` (transparent drop-in for the ``os.getenv`` calls
    that pepper existing settings modules). Local dev, the ``minimal`` preset
    and any unconfigured project keep working with zero new dependencies.
    """

    fail_closed = False

    def get(self, name: str) -> str | None:
        return os.environ.get(name)


# --- provider resolution (memoized, bootstrap-tolerant) ---------------------

_provider_lock = threading.Lock()
_provider_instance: SecretProvider | None = None

# name -> (value, expires_at_monotonic). Positive-only cache (a miss is cheap
# and must never mask a just-added secret). Guarded by _cache_lock.
_cache_lock = threading.Lock()
_cache: dict[str, tuple[str, float]] = {}


def _reset_state(*, setting=None, **kwargs) -> None:
    """Drop the memoized provider and the value cache.

    Connected to Django's ``setting_changed`` (a config change starts fresh)
    and callable directly from tests / stapel-vault on rotation.
    """
    if setting is not None and setting != "STAPEL_SECRETS":
        # A STAPEL_SECRETS override, or a flat setting matching one of our
        # keys, should reset; ignore unrelated override_settings blocks.
        try:
            from .conf import secrets_settings

            if setting not in secrets_settings.defaults:
                return
        except Exception:
            return
    global _provider_instance
    with _provider_lock:
        _provider_instance = None
    with _cache_lock:
        _cache.clear()


try:  # keep the singleton honest across override_settings in tests
    from django.test.signals import setting_changed

    setting_changed.connect(_reset_state, weak=False)
except Exception:  # pragma: no cover - Django not importable at import time
    pass


def _provider_spec() -> object:
    """Provider spec (dotted path / class / instance), tolerant of an
    unconfigured Django.

    Prefers ``STAPEL_SECRETS["PROVIDER"]`` when settings are readable; during
    settings bootstrap (Django not configured yet) falls back to the
    ``STAPEL_SECRETS_PROVIDER`` env var (a dotted path) and finally the env
    provider.
    """
    try:
        from .conf import secrets_settings

        return secrets_settings.PROVIDER
    except Exception:
        # Django settings not configured yet — prod settings modules resolve
        # SECRET_KEY here, before django.setup(). Use the explicit bootstrap
        # env var (or the env provider).
        return os.environ.get(BOOTSTRAP_PROVIDER_ENV) or _DEFAULT_PROVIDER


def _cache_ttl() -> float:
    try:
        from .conf import secrets_settings

        return float(secrets_settings.CACHE_TTL)
    except Exception:
        return float(_DEFAULT_CACHE_TTL)


def _resolve_provider() -> SecretProvider:
    """The configured provider (dotted path / class / instance), memoized."""
    instance = _provider_instance
    if instance is not None:
        return instance
    with _provider_lock:
        if _provider_instance is not None:
            return _provider_instance
        value: object = _provider_spec()
        if isinstance(value, str):
            from django.utils.module_loading import import_string

            value = import_string(value)
        if isinstance(value, type):
            value = value()
        if not (hasattr(value, "get") and callable(value.get)):
            raise TypeError(
                f"STAPEL_SECRETS['PROVIDER'] resolved to {value!r}, which is "
                "not a SecretProvider (no callable get(name))."
            )
        globals()["_provider_instance"] = value
        return value  # type: ignore[return-value]


def get_secret(name: str, default: object = _UNSET) -> str | None:
    """Resolve secret *name* through the configured provider.

    Args:
        name: logical secret name (e.g. ``"SECRET_KEY"``). The provider is
            free to map it onto its own namespace.
        default: value returned when the provider has no value for *name*.
            Omit it to make a missing secret fail-closed (see below).

    Returns:
        The secret string, or *default* / ``None`` when absent (per the
        provider's ``fail_closed`` flag).

    Raises:
        SecretUnavailable: the provider returned ``None``, no *default* was
            supplied, and the provider is fail-closed (every provider except
            the env default).
    """
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(name)
        if entry is not None and entry[1] > now:
            return entry[0]

    provider = _resolve_provider()
    value = provider.get(name)
    if value is not None:
        ttl = _cache_ttl()
        if ttl > 0:
            with _cache_lock:
                _cache[name] = (value, now + ttl)
        return value

    if default is not _UNSET:
        return default  # type: ignore[return-value]

    if getattr(provider, "fail_closed", True):
        raise SecretUnavailable(name, type(provider).__name__)
    return None


def invalidate_secret(name: str | None = None) -> None:
    """Drop cached secret(s) so the next :func:`get_secret` re-reads them.

    ``invalidate_secret()`` clears the whole cache; ``invalidate_secret(name)``
    clears one entry. The rotation hook: after a secret store rotates a value,
    call this to force an immediate re-read instead of waiting out the TTL.
    stapel-vault calls it from its rotation handling.
    """
    with _cache_lock:
        if name is None:
            _cache.clear()
        else:
            _cache.pop(name, None)


__all__ = [
    "BOOTSTRAP_PROVIDER_ENV",
    "EnvSecretProvider",
    "SecretProvider",
    "SecretUnavailable",
    "get_secret",
    "invalidate_secret",
]
