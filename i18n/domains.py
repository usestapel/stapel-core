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


def _errors_owners() -> dict[str, str]:
    from stapel_core.django.api.errors import error_owners

    _errors_source()  # force-import every error module so the registry is whole
    return error_owners()


def _errors_export_codes(owner: str) -> set[str] | None:
    """Codes *owner*'s shipped registry export declares, or None if it ships none.

    The export lives at ``<top-level package dir>/docs/errors.json`` — the
    ``generate_error_keys`` artifact, carried in the wheel by package-data.
    ``None`` means "no export at all" (the gdpr/core failure shape: catalogs on
    disk, registry nowhere, so no consumer can pair the two); an unreadable
    file counts as an empty export rather than as absence, so a corrupt
    artifact reads as "declares nothing" and every translated key goes red.
    """
    import json
    import sys
    from importlib.util import find_spec
    from pathlib import Path

    module = sys.modules.get(owner)
    roots = list(getattr(module, "__path__", []) or [])
    if not roots:
        try:
            spec = find_spec(owner)
        except (ImportError, ValueError):
            return None
        roots = list(spec.submodule_search_locations or []) if spec else []
    for root in roots:
        path = Path(root) / "docs" / "errors.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        if not isinstance(data, list):
            return set()
        return {
            e["code"] for e in data
            if isinstance(e, dict) and isinstance(e.get("code"), str)
        }
    return None


#: domain → callable returning the canonical ``{key: source_text}`` map.
DOMAIN_SOURCES: dict[str, Callable[[], dict[str, str]]] = {
    "errors": _errors_source,
    "flows": _flows_source,
}

#: domain → callable returning ``{key: owning package}``. A domain without a
#: resolver reports no ownership, and the gate then behaves exactly as it did
#: before ownership existed (every canonical key required, nothing foreign) —
#: so adding ownership to one domain cannot disturb another.
DOMAIN_OWNERS: dict[str, Callable[[], dict[str, str]]] = {
    "errors": _errors_owners,
}

#: domain → callable ``(owner) -> set of exported codes | None``. The registry
#: export is the OTHER half of a package's i18n contract: catalogs say how a
#: key reads, the export says the key exists. A domain without a resolver has
#: no export artifact and the catalog gate skips the pairing check for it.
DOMAIN_EXPORTS: dict[str, Callable[[str], "set[str] | None"]] = {
    "errors": _errors_export_codes,
}


def source_texts(domain: str) -> dict[str, str]:
    try:
        resolver = DOMAIN_SOURCES[domain]
    except KeyError:
        raise ValueError(
            f"unknown i18n domain {domain!r} — known: {sorted(DOMAIN_SOURCES)}"
        )
    return resolver()


def source_owners(domain: str) -> dict[str, str]:
    """``{key: owning package}`` for *domain* — empty when it tracks no owners."""
    resolver = DOMAIN_OWNERS.get(domain)
    return resolver() if resolver else {}


def export_codes(domain: str, owner: str) -> "set[str] | None":
    """Codes *owner*'s registry export declares for *domain*.

    ``None`` when the owner ships no export — or when the domain has no export
    artifact at all, which callers must treat as "nothing to pair against",
    not as a defect.
    """
    resolver = DOMAIN_EXPORTS.get(domain)
    return resolver(owner) if resolver else None


__all__ = [
    "DOMAIN_EXPORTS",
    "DOMAIN_OWNERS",
    "DOMAIN_SOURCES",
    "export_codes",
    "params_of",
    "source_owners",
    "source_texts",
]
