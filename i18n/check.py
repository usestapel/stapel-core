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
  what has not been READ, not what came from a poor source;
* **E** no registry export / unexported — the package ships catalogs for keys
  it owns but no ``docs/errors.json`` (or a stale one): its codes are
  invisible to every consumer that pairs registries with catalogs. Where that
  export lives follows what the package IS: a distributable carries it in its
  wheel, a project's own app has no wheel and is declared by the project's
  export (:func:`domains._errors_export_codes`). The other direction of the
  same contract is :func:`check_registry_catalog_pairing`, run at
  ``generate_error_keys`` emission time.

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
    owner_languages,
    owner_of_dir,
)
from .domains import DOMAIN_EXPORTS, params_of, source_owners


@dataclass
class CatalogIssue:
    level: str  # "error" | "warning"
    # "missing" | "stale" | "params" | "unstable" | "unreviewed" | "orphan"
    # | "foreign" | "vacuous_override" | "no_registry_export" | "unexported"
    # | "untranslated" | "unshipped"
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
    export_resolver: "Callable[[str], set[str] | None] | None" = None,
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

    # The registry export is the other half of this package's contract: a
    # catalog says how an owned key reads, the export (docs/errors.json —
    # generate_error_keys) says the key exists. A translated key with no
    # export entry is a string no consumer can ever render — the shape that
    # let gdpr's ten codes and core's forty-one keys ship invisible.
    if owner and owners and (export_resolver or DOMAIN_EXPORTS.get(domain)):
        resolver = export_resolver or DOMAIN_EXPORTS[domain]
        strictly_owned = {k for k in source_texts if owners.get(k) == owner}
        if strictly_owned:
            exported = resolver(owner)
            if exported is None:
                issues.append(CatalogIssue(
                    "error", "no_registry_export", "*",
                    f"{owner!r} ships {domain} catalogs and owns "
                    f"{len(strictly_owned)} key(s) but publishes no registry "
                    f"export (docs/errors.json) — its codes are invisible to "
                    f"every consumer; run `generate_error_keys` and ship the "
                    f"artifact: inside the package for a distributable, at "
                    f"<BASE_DIR>/docs/errors.json (STAPEL_I18N"
                    f"[\"REGISTRY_EXPORT\"]) for a project's own app",
                ))
            else:
                translated: set[str] = set()
                for lang in languages:
                    if lang == source_language:
                        continue
                    translated.update(
                        load_catalog_file(out / catalog_filename(domain, lang))
                    )
                unexported = sorted(
                    (translated & strictly_owned) - exported
                )
                if unexported:
                    issues.append(CatalogIssue(
                        "error", "unexported", "*",
                        f"{len(unexported)} translated key(s) missing from "
                        f"{owner!r}'s registry export (docs/errors.json is "
                        f"stale — regenerate it): {unexported[:8]}",
                    ))

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


def check_registry_catalog_pairing(
    entries: list[dict],
    *,
    domain: str = "errors",
    languages_of: Callable[[str, str], "set[str]"] | None = None,
    owner_catalogs: Callable[[str, str, str], dict[str, str]] | None = None,
) -> list[CatalogIssue]:
    """Every declared code must be translated wherever its owner translates.

    Run at ``generate_error_keys`` emission time, over the registry the
    instance is about to export. *entries* is ``build_error_registry()``
    output — each with its ``owner`` provenance. For every owner that ships
    the domain in some language (over INSTALLED_APPS), every code it owns must
    be present in that language's catalog:

    * **E** ``untranslated`` — the registry declares a code, its owner ships
      the language, and the owner's catalog does not carry it. This is the
      auth/gdpr incident shape made impossible: an ownership move that strips
      a translation goes red at the next emission of EVERY instance that
      mounts the key, not at a user's screen.
    * **W** ``unshipped`` — an owner with declared codes ships no catalog in
      any language while some other package in the instance does: its codes
      render as English fallbacks in an otherwise translated deployment. A
      counter, not a blocker — a package that never claimed a language has no
      translation contract to break, only coverage debt to see.

    Pure over injectable *languages_of* / *owner_catalogs* (defaulted from
    INSTALLED_APPS like the catalog gate), so a unit test needs no apps.
    """
    if languages_of is None:
        languages_of = owner_languages
    if owner_catalogs is None:
        owner_catalogs = owner_catalog

    by_owner: dict[str, list[str]] = {}
    for entry in entries:
        pkg = entry.get("owner")
        if isinstance(pkg, str) and pkg:
            by_owner.setdefault(pkg, []).append(entry["code"])

    shipped = {pkg: sorted(languages_of(pkg, domain)) for pkg in by_owner}
    anyone_ships = any(shipped.values())

    issues: list[CatalogIssue] = []
    for pkg in sorted(by_owner):
        langs = shipped[pkg]
        if not langs:
            if anyone_ships:
                issues.append(CatalogIssue(
                    "warning", "unshipped", "*",
                    f"{pkg!r} owns {len(by_owner[pkg])} declared code(s) but "
                    f"ships no {domain} catalog in any language — they will "
                    f"render as English fallbacks in a translated deployment",
                ))
            continue
        for lang in langs:
            catalog = owner_catalogs(pkg, domain, lang)
            for code in by_owner[pkg]:
                if code not in catalog:
                    issues.append(CatalogIssue(
                        "error", "untranslated", lang,
                        f"{code!r} is declared by this registry and owned by "
                        f"{pkg!r}, which ships {lang} — but its {lang} catalog "
                        f"does not carry the key; translate it (or the "
                        f"ownership move that stripped it lost a string)",
                    ))
    return issues


def summarize(issues: list[CatalogIssue]) -> tuple[int, int]:
    """(#errors, #warnings)."""
    errors = sum(1 for i in issues if i.level == "error")
    return errors, len(issues) - errors


__all__ = [
    "CatalogIssue",
    "check_registry_catalog_pairing",
    "check_translation_catalogs",
    "owned_keys",
    "summarize",
]
