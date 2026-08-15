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
from pathlib import Path
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


#: Where a registry export sits inside the unit that publishes it: a package
#: directory for a distributable, the project root for a monolith.
EXPORT_RELPATH = ("docs", "errors.json")


def _read_export(path: Path, owner: str | None = None) -> set[str] | None:
    """Codes the export at *path* declares — for *owner* only, when given.

    ``None`` when there is no such file. An unreadable or mis-shaped file
    counts as an empty export rather than as absence, so a corrupt artifact
    reads as "declares nothing" and every translated key goes red. With
    *owner*, an entry attributed to a DIFFERENT package is skipped: an export
    vouches for a package's codes, never for its neighbour's. An entry that
    names nobody counts (a pre-``owner`` artifact), exactly as an un-attributed
    key counts as owned in :func:`owned_keys`.
    """
    import json

    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(data, list):
        return set()
    codes: set[str] = set()
    for entry in data:
        if not isinstance(entry, dict) or not isinstance(entry.get("code"), str):
            continue
        declared = entry.get("owner")
        if owner is not None and isinstance(declared, str) and declared:
            if declared != owner:
                continue
        codes.add(entry["code"])
    return codes


def _package_roots(owner: str) -> list[str]:
    """Directories the *owner* package occupies, or [] when it is unimportable."""
    import sys
    from importlib.util import find_spec

    module = sys.modules.get(owner)
    roots = list(getattr(module, "__path__", []) or [])
    if roots:
        return roots
    try:
        spec = find_spec(owner)
    except (ImportError, ValueError):
        return []
    return list(spec.submodule_search_locations or []) if spec else []


def _is_distributed(package: str) -> bool:
    """True when an installed distribution provides the top-level *package*.

    "Does this package ship to anybody" — the question that decides where its
    registry export has to live. A distribution travels as a wheel, so its
    export must travel inside it; a project's own app travels nowhere.
    """
    from importlib.metadata import packages_distributions

    try:
        return package in packages_distributions()
    except Exception:
        return False


def _project_export() -> tuple[Path, Path] | None:
    """``(project root, project export path)``, or None outside a project.

    ``STAPEL_I18N["REGISTRY_EXPORT"]`` wins; otherwise the convention that
    mirrors the package one — ``<BASE_DIR>/docs/errors.json``, the artifact
    ``generate_error_keys`` writes.
    """
    from django.conf import settings

    try:
        base = getattr(settings, "BASE_DIR", None)
    except Exception:  # settings not configured
        return None
    from .conf import i18n_settings

    try:
        configured = i18n_settings.REGISTRY_EXPORT
    except Exception:
        configured = None
    if configured:
        export = Path(configured)
        return (Path(base) if base else export.parent.parent), export
    if not base:
        return None
    return Path(base), Path(base).joinpath(*EXPORT_RELPATH)


def _within(path: Path, root: Path) -> bool:
    try:
        resolved, base = path.resolve(), root.resolve()
    except OSError:
        return False
    return resolved == base or base in resolved.parents


def _errors_export_codes(owner: str) -> set[str] | None:
    """Codes *owner*'s registry export declares, or None if it publishes none.

    A distributable package carries its own export at
    ``<top-level package dir>/docs/errors.json`` — the ``generate_error_keys``
    artifact, kept in the wheel by package-data. That is the only place a
    consumer who installed the package can look, so that is where the gate
    looks first. ``None`` means "no export at all": the gdpr/core failure shape
    (catalogs on disk, registry nowhere, so no consumer can pair the two).

    A monolith's local app is not a wheel and has no ``docs/`` of its own to
    put anything in. Its codes are declared by the export of the project it IS
    part of (:func:`_project_export`) — the artifact the project already
    generates and commits. Two conditions keep that from becoming a way out of
    the gate: the package must be *undistributed* (an installed distribution
    has a wheel of its own to carry its export, and no project export stands in
    for it — otherwise a library shipping catalogs with no registry export goes
    green inside any project that generates one), and it must live inside the
    project root that publishes the export. And the project export answers for
    a code only where it attributes it to that app, so it cannot vouch for keys
    another package owns.
    """
    roots = _package_roots(owner)
    for root in roots:
        codes = _read_export(Path(root).joinpath(*EXPORT_RELPATH))
        if codes is not None:
            return codes

    if not roots or _is_distributed(owner):
        return None
    project = _project_export()
    if project is None:
        return None
    root_dir, export = project
    if not any(_within(Path(r), root_dir) for r in roots):
        return None
    return _read_export(export, owner=owner)


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
