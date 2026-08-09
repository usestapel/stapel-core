"""``check_translation_catalogs`` — the per-locale drift/params/provenance gate.

i18n-shipping.md §5. Given a domain's canonical ``{key: source_en}`` and the
catalog directory, verify each shipped locale:

* **E** missing key — the locale does not cover a canonical key **this app
  owns** (see below);
* **E** foreign key — the locale re-translates a key another package owns and
  already ships in that language, without declaring the override;
* **E** stale — the source (en) text changed after the translation (the
  ``.state.json`` hash no longer matches ``h(source)``);
* **E** params mismatch — the translation/override dropped or invented a
  ``{param}`` slot relative to the canon (a client override MUST preserve the
  canon placeholders — §3);
* **E** not byte-stable — the catalog file is not in ``dump_catalog`` form;
* **W** unreviewed — a value no human has signed off: machine
  (``origin: llm``), curated corpus (``origin: seed:<label>`` — paid for, still
  machine-made), imported with unknown authorship, or with no sidecar entry at
  all. A *counter*, not a release blocker (§5, open question #3), and it counts
  what has not been READ, not what came from a poor source.

**Ownership scoping.** ``source_texts`` for the ``errors`` domain is the whole
in-process registry — core's cross-cutting keys included. Requiring every
canonical key from every module's own catalog is what produced the fleet's 410
duplicated entries: five libraries each re-translated the same 41 core keys,
byte-identically, because going green demanded it. So the canon is scoped by
*owner* (``domains.source_owners``): a module answers for the keys it owns,
core ships core's, and covering someone else's key is a shadow that has to be
declared (``translate_catalogs --declare-override``) or deleted.

Scoping engages only when ownership resolves — an explicit *owner*, or an
*out_dir* that is an installed app's catalog directory. Outside that (a
tmp_path unit test, a domain with no owner resolver) the gate behaves exactly
as it did before ownership existed.

Pure over its inputs (``source_texts`` + a directory) so a module's pytest can
call it directly, exactly like ``check_flows``.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .catalogs import (
    STATE_FILENAME,
    StateSidecar,
    catalog_filename,
    content_hash,
    dump_catalog,
    is_reviewed,
    load_catalog_file,
    owner_catalog,
    owner_of_dir,
)
from .domains import params_of, source_owners


@dataclass
class CatalogIssue:
    level: str  # "error" | "warning"
    # "missing" | "stale" | "params" | "unstable" | "unreviewed" | "orphan"
    # | "foreign" | "vacuous_override"
    code: str
    language: str
    message: str


def owned_keys(
    source_texts: dict[str, str],
    owners: dict[str, str],
    owner: str | None,
) -> dict[str, str]:
    """The slice of *source_texts* that *owner* answers for.

    A key with no known owner counts as owned: an un-attributed key must still
    be translated by somebody, and silently dropping it from the canon would
    turn a coverage gate into a coverage illusion.
    """
    if not owner or not owners:
        return dict(source_texts)
    return {
        k: v for k, v in source_texts.items()
        if owners.get(k, owner) == owner
    }


def check_translation_catalogs(
    domain: str,
    out_dir: Path | str,
    *,
    source_texts: dict[str, str],
    languages: list[str],
    source_language: str = "en",
    owner: str | None = None,
    owners: dict[str, str] | None = None,
    owner_catalogs: Callable[[str, str, str], dict[str, str]] | None = None,
    undeclared_overrides: str = "error",
) -> list[CatalogIssue]:
    """Gate the *domain* catalogs in the *out_dir* ``translations`` dir.

    *out_dir* is the directory holding ``<domain>.<lang>.json`` + ``.state.json``
    (a module's ``translations/``). *source_language* has no catalog (its texts
    are the canon — the registry for errors, the literals for flows) and is
    skipped.

    *owner* is the package whose catalogs these are (defaulted from *out_dir*
    against INSTALLED_APPS); *owners* is ``{key: owning package}`` (defaulted
    from the domain's resolver). Together they scope coverage to the keys this
    package answers for and turn an undeclared copy of somebody else's key into
    a ``foreign`` error. *owner_catalogs(owner, domain, language)`` answers
    "what does that owner actually publish here" (defaulted to the
    INSTALLED_APPS lookup); *undeclared_overrides* downgrades the error to a
    warning (``"warn"``) for a host migrating a legacy catalog.
    """
    out = Path(out_dir)
    issues: list[CatalogIssue] = []
    state = StateSidecar(out / STATE_FILENAME)

    if owner is None:
        owner = owner_of_dir(out)
    if owners is None:
        owners = source_owners(domain)
    if owner_catalogs is None:
        owner_catalogs = owner_catalog
    canon = owned_keys(source_texts, owners, owner)
    foreign_level = "warning" if undeclared_overrides == "warn" else "error"

    for lang in languages:
        if lang == source_language:
            continue
        path = out / catalog_filename(domain, lang)
        catalog = load_catalog_file(path)

        # byte-stability of the file on disk (only if present + parseable).
        if path.is_file():
            raw = path.read_text(encoding="utf-8")
            if catalog and raw != dump_catalog(catalog):
                issues.append(CatalogIssue(
                    "error", "unstable", lang,
                    f"{path.name} is not byte-stable — run "
                    f"`translate_catalogs --domain {domain} --lang {lang}` to normalise",
                ))

        declared = state.overrides(domain, lang)

        for key in canon:
            if key not in catalog:
                issues.append(CatalogIssue(
                    "error", "missing", lang,
                    f"{lang}: missing translation for {key!r}",
                ))

        # A foreign key is one this package does not own but translates anyway.
        # It is only a shadow where the owner ALSO ships that language for that
        # key: filling a gap the owner leaves (a host generating a language for
        # the whole fleet) is legitimate and must not demand a declaration per
        # key. The day the owner ships the language, this goes red and the
        # copy is deleted or declared.
        if owner and owners:
            for key in catalog:
                key_owner = owners.get(key)
                if not key_owner or key_owner == owner or key not in source_texts:
                    continue
                upstream = owner_catalogs(key_owner, domain, lang).get(key)
                if upstream is None:
                    continue
                if key not in declared:
                    issues.append(CatalogIssue(
                        foreign_level, "foreign", lang,
                        f"{lang}: {key!r} is owned by {key_owner!r}, which already "
                        f"ships it in {lang} — delete it (the loader merges the "
                        f"owner's catalog) or declare the reword with "
                        f"`translate_catalogs --domain {domain} --lang {lang} "
                        f"--declare-override {key}`",
                    ))
                elif catalog[key] == upstream:
                    issues.append(CatalogIssue(
                        "warning", "vacuous_override", lang,
                        f"{lang}: {key!r} is declared an override of "
                        f"{key_owner!r} but repeats its text verbatim — the "
                        f"declaration protects nothing; delete the entry",
                    ))

        for key, source_en in source_texts.items():
            value = catalog.get(key)
            if value is None:
                continue
            if set(params_of(value)) != set(params_of(source_en)):
                issues.append(CatalogIssue(
                    "error", "params", lang,
                    f"{lang}: {key!r} placeholders {sorted(params_of(value))} "
                    f"≠ canon {sorted(params_of(source_en))}",
                ))
            st = state.get(domain, lang, key)
            if st is not None and st.get("hash") != content_hash(source_en):
                issues.append(CatalogIssue(
                    "error", "stale", lang,
                    f"{lang}: {key!r} is stale — the en source changed since it "
                    f"was translated; re-run `translate_catalogs`",
                ))
            if not is_reviewed((st or {}).get("origin")):
                issues.append(CatalogIssue(
                    "warning", "unreviewed", lang,
                    f"{lang}: {key!r} is unreviewed "
                    f"(origin={(st or {}).get('origin', 'unknown')}) — no human "
                    f"has approved this text",
                ))

        # Orphans — catalog keys not in the canon. Allowed (a host app may
        # override another module's key), so warn rather than fail.
        for key in catalog:
            if key not in source_texts:
                issues.append(CatalogIssue(
                    "warning", "orphan", lang,
                    f"{lang}: {key!r} is not a canonical {domain} key here "
                    f"(cross-module override?)",
                ))

    return issues


def summarize(issues: list[CatalogIssue]) -> tuple[int, int]:
    """(#errors, #warnings)."""
    errors = sum(1 for i in issues if i.level == "error")
    return errors, len(issues) - errors


__all__ = [
    "CatalogIssue",
    "check_translation_catalogs",
    "owned_keys",
    "summarize",
]
