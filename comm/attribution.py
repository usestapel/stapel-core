"""Which installed app a registered handler belongs to.

A subscriber in the action registry is a plain callable. The only thing that
names the library behind it is where it was defined, and two mechanisms need
that answer:

- the lifecycle-pair check, which charges a missing companion handler to an
  app rather than to core;
- the GDPR provider bridge, which must not answer for a library that already
  answers for itself.

Handlers core subscribes on another package's behalf are closures of core, so
their ``__module__`` names core. They carry a ``stapel_handler_module`` stamp
naming the library that asked for them, and that stamp wins.
"""
from __future__ import annotations

import inspect


def handler_module(handler) -> str:
    """The module *handler* should be charged to, or ``""``."""
    stamped = getattr(handler, "stapel_handler_module", None)
    if stamped:
        return str(stamped)
    try:
        handler = inspect.unwrap(handler)
    except Exception:  # noqa: BLE001 — a broken __wrapped__ chain is not our error
        pass
    return str(getattr(handler, "__module__", "") or "")


def owning_app(module: str) -> str:
    """The longest installed ``AppConfig.name`` that owns *module*.

    Falls back to the top-level package, so a module outside every installed
    app is still named rather than dropped — and so the answer is stable
    before the app registry is populated.
    """
    from django.apps import apps
    from django.core.exceptions import AppRegistryNotReady

    best = ""
    try:
        configs = list(apps.get_app_configs())
    except AppRegistryNotReady:
        configs = []
    for config in configs:
        name = config.name
        if (module == name or module.startswith(f"{name}.")) and len(name) > len(best):
            best = name
    return best or module.split(".")[0]


__all__ = ["handler_module", "owning_app"]
