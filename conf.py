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
    ) -> None:
        self.namespace = namespace
        self.defaults = dict(defaults)
        self.import_strings = frozenset(import_strings)
        self._cache: dict[str, Any] = {}
        self._connect_reload()

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
