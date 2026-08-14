"""Per-app settings namespaces — the DRF api_settings pattern, generalized.

Every Stapel package exposes one AppSettings instance instead of scattering
``getattr(settings, ...)`` calls:

    # stapel_billing/conf.py
    from stapel_core.conf import AppSettings

    billing_settings = AppSettings(
        "STAPEL_BILLING",
        defaults={
            "PAYMENT_PROVIDER": "stapel_billing.providers.stripe.StripeProvider",
            "CURRENCY": "usd",
        },
        import_strings=("PAYMENT_PROVIDER",),
    )

Resolution order per key: ``settings.<NAMESPACE>`` dict → flat Django
setting of the same name (legacy) → environment variable → default.
Values listed in *import_strings* are resolved with import_string — the
dotted-path escape hatch that makes behavior swappable without forking.
Caches are invalidated on Django's setting_changed (tests).

**A name in *import_strings* is never read from the environment.** Such a
name does not carry data, it names the CLASS the process imports and runs;
letting a same-named env var pick it means anything that can set an env var
in the pod (a leaked value, a sibling container's config, a stray export in
an entrypoint) chooses the implementation of a provider, backend or policy.
The project's own settings module is trusted and still wins — the
environment is not. A deployment that genuinely must select an
implementation from the environment says so once, by name, with
*env_overridable*.
"""
from __future__ import annotations

import os
from typing import Any, Iterable

_EMPTY = object()


class AppSettings:
    def __init__(
        self,
        namespace: str,
        defaults: dict[str, Any],
        import_strings: Iterable[str] = (),
        no_env: Iterable[str] = (),
        env_overridable: Iterable[str] = (),
    ) -> None:
        self.namespace = namespace
        self.defaults = dict(defaults)
        self.import_strings = frozenset(import_strings)
        # Keys that must never fall back to an environment variable: their
        # names are generic enough (PROVIDER, TRUSTED_PROXY_HEADER, …) that a
        # stray same-named env var could silently change a trust/security
        # decision. They still resolve via the namespace dict, a flat Django
        # setting, or the default — an explicit env var is simply ignored.
        self.no_env = frozenset(no_env)
        # The deliberate, greppable opt-OUT of the implicit no_env that every
        # import_strings key gets. Naming a key here restores the environment
        # step for it — for the deployment that really does pick an
        # implementation per environment. It is opt-out on purpose: forgetting
        # a flag must leave the process safe, never open.
        self.env_overridable = frozenset(env_overridable)
        contradictory = self.no_env & self.env_overridable
        if contradictory:
            # Silently picking a winner would hide an authoring mistake in the
            # one declaration that decides what code the process loads.
            raise ValueError(
                f"{namespace}: {sorted(contradictory)} declared both no_env and "
                "env_overridable — say which one you mean"
            )
        self._cache: dict[str, Any] = {}
        self._connect_reload()

    def _env_allowed(self, key: str) -> bool:
        """May *key* be read from ``os.environ``?

        import_strings names an implementation, not a value, so it is
        implicitly no_env; ``env_overridable`` is the explicit way back out.
        """
        if key in self.no_env:
            return False
        if key in self.import_strings:
            return key in self.env_overridable
        return True

    def _connect_reload(self) -> None:
        try:
            from django.test.signals import setting_changed

            def _reload(*, setting, **kwargs):
                if setting == self.namespace or setting in self.defaults:
                    self.reload()

            setting_changed.connect(_reload, weak=False)
        except Exception:  # Django not ready — tests will call reload()
            pass

    def reload(self) -> None:
        self._cache.clear()

    def _raw(self, key: str) -> Any:
        from django.conf import settings

        overrides = getattr(settings, self.namespace, None) or {}
        if key in overrides:
            return overrides[key]
        flat = getattr(settings, key, _EMPTY)
        if flat is not _EMPTY:
            return flat
        if self._env_allowed(key):
            env = os.environ.get(key)
            if env is not None:
                return env
        if key in self.defaults:
            return self.defaults[key]
        raise AttributeError(f"{self.namespace} has no setting {key!r}")

    def __getattr__(self, key: str) -> Any:
        if key.startswith("_"):
            raise AttributeError(key)
        if key in self._cache:
            return self._cache[key]
        value = self._raw(key)
        if key in self.import_strings and isinstance(value, str) and value:
            from django.utils.module_loading import import_string

            value = import_string(value)
        self._cache[key] = value
        return value


__all__ = ["AppSettings"]
