"""``check_translation_catalogs`` — the per-locale drift/params/provenance gate.

i18n-shipping.md §5. Given a domain's canonical ``{key: source_en}`` and the
catalog directory, verify each shipped locale:

* **E** missing key — the locale does not cover a canonical key;
* **E** stale — the source (en) text changed after the translation (the
  ``.state.json`` hash no longer matches ``h(source)``);
* **E** params mismatch — the translation/override dropped or invented a
  ``{param}`` slot relative to the canon (a client override MUST preserve the
  canon placeholders — §3);
* **E** not byte-stable — the catalog file is not in ``dump_catalog`` form;
* **W** unreviewed — a value whose provenance is machine (``origin: llm``) or
  unknown (no sidecar entry); a *counter*, not a release blocker (§5, open
  question #3).

Pure over its inputs (``source_texts`` + a directory) so a module's pytest can
call it directly, exactly like ``check_flows``.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .catalogs import (
    ORIGIN_LLM,
    STATE_FILENAME,
    StateSidecar,
    catalog_filename,
    content_hash,
    dump_catalog,
    load_catalog_file,
)
from .domains import params_of


@dataclass
class CatalogIssue:
    level: str  # "error" | "warning"
    code: str   # "missing" | "stale" | "params" | "unstable" | "unreviewed" | "orphan"
    language: str
    message: str


def check_translation_catalogs(
    domain: str,
    out_dir: Path | str,
    *,
    source_texts: dict[str, str],
    languages: list[str],
    source_language: str = "en",
) -> list[CatalogIssue]:
    """Gate the *domain* catalogs in the *out_dir* ``translations`` dir.

    *out_dir* is the directory holding ``<domain>.<lang>.json`` + ``.state.json``
    (a module's ``translations/``). *source_language* has no catalog (its texts
    are the canon — the registry for errors, the literals for flows) and is
    skipped.
    """
    out = Path(out_dir)
    issues: list[CatalogIssue] = []
    state = StateSidecar(out / STATE_FILENAME)

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

        for key, source_en in source_texts.items():
            value = catalog.get(key)
            if value is None:
                issues.append(CatalogIssue(
                    "error", "missing", lang,
                    f"{lang}: missing translation for {key!r}",
                ))
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
            if st is None or st.get("origin") == ORIGIN_LLM:
                issues.append(CatalogIssue(
                    "warning", "unreviewed", lang,
                    f"{lang}: {key!r} is unreviewed "
                    f"(origin={(st or {}).get('origin', 'unknown')})",
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


__all__ = ["CatalogIssue", "check_translation_catalogs", "summarize"]
