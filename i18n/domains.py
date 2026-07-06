"""Domain sources — how each catalog domain names its canonical (en) texts.

A *domain resolver* returns the canonical ``{key: source_text}`` mapping the
catalogs of that domain translate. ``translate_catalogs`` and
``check_translation_catalogs`` are domain-agnostic; they call the resolver
registered for ``--domain``. New content domains register here.

Two resolvers ship:

- ``errors`` — the error-key registry (``build_error_registry``): the same
  ``{code: en}`` the ``errors.json`` artifact and the runtime ``/error-keys/``
  view expose. Every error module is force-imported first (as
  ``generate_error_keys`` does) so the registry is complete.
- ``flows`` — the flow source literals (``flow_source_texts``) over every
  autodiscovered flow.
"""
from __future__ import annotations

import re
from typing import Callable

#: ``{name}`` interpolation slots in a template, de-duped, first-seen order.
_PARAM_RE = re.compile(r"\{(\w+)\}")


def params_of(text: str) -> list[str]:
    seen: list[str] = []
    for m in _PARAM_RE.finditer(text):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def _errors_source() -> dict[str, str]:
    from importlib import import_module

    from django.conf import settings
    from django.utils.module_loading import autodiscover_modules

    from stapel_core.django.api.errors import build_error_registry

    autodiscover_modules("errors")
    for mod in (
        "stapel_core.verification.errors",
        "stapel_core.django.captcha",
        *getattr(settings, "STAPEL_ERROR_MODULES", []),
    ):
        try:
            import_module(mod)
        except ImportError:
            continue
    return {e["code"]: e["en"] for e in build_error_registry()}


def _flows_source() -> dict[str, str]:
    from stapel_core.flows import autodiscover_flows, flow_registry, flow_source_texts

    autodiscover_flows()
    return flow_source_texts(flow_registry.all())


#: domain → callable returning the canonical ``{key: source_text}`` map.
DOMAIN_SOURCES: dict[str, Callable[[], dict[str, str]]] = {
    "errors": _errors_source,
    "flows": _flows_source,
}


def source_texts(domain: str) -> dict[str, str]:
    try:
        resolver = DOMAIN_SOURCES[domain]
    except KeyError:
        raise ValueError(
            f"unknown i18n domain {domain!r} — known: {sorted(DOMAIN_SOURCES)}"
        )
    return resolver()


__all__ = ["DOMAIN_SOURCES", "params_of", "source_texts"]
