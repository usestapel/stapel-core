"""Human-readable error reference — ``docs/errors.<lang>.md`` (i18n-shipping.md §4).

A byte-stable markdown table (``code | status | params | remediation | text``)
per language, joining the error registry (``build_error_registry`` — code /
status / params / remediation / canonical en) with the language catalog
(``translations/errors.<lang>.json``). The en table uses the registry text; a
localized table uses the catalog value, falling back to en (marked) for keys
the locale does not cover — so the reference is never empty and shows coverage
honestly. Gated by the same regenerate-and-diff pattern as the flow docs.
"""
from __future__ import annotations

from pathlib import Path

from .catalogs import load_app_catalogs, load_catalog_file

LANGUAGE_NAMES = {"en": "English", "ru": "Русский"}

_HEADER_EN = ("Code", "Status", "Params", "Remediation", "Text")
_HEADER = {
    "en": _HEADER_EN,
    "ru": ("Код", "Статус", "Параметры", "Действие", "Текст"),
}


def _cell(text: str) -> str:
    """Escape a value for a single markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ").strip()


def render_error_docs(
    entries: list[dict],
    language: str,
    catalog: dict[str, str] | None = None,
    source_language: str = "en",
) -> str:
    """Render the reference table for *language* from registry *entries*.

    *entries* is ``build_error_registry()`` output. *catalog* is the
    ``{code: text}`` for *language* (ignored when ``language == source_language``).
    """
    header = _HEADER.get(language, _HEADER_EN)
    title = LANGUAGE_NAMES.get(language, language)
    lines = [
        f"# Errors — {title}",
        "",
        f"`{len(entries)}` error keys. Canonical texts live in the code "
        f"(`register_service_errors`); localized texts in "
        f"`translations/errors.{language}.json`.",
        "",
        "| " + " | ".join(header) + " |",
        "|" + "---|" * len(header),
    ]
    catalog = catalog or {}
    is_source = language == source_language
    for e in sorted(entries, key=lambda x: x["code"]):
        code = e["code"]
        if is_source:
            text = _cell(e["en"])
        else:
            localized = catalog.get(code)
            text = _cell(localized) if localized else f"{_cell(e['en'])} _(en)_"
        params = ", ".join(f"`{p}`" for p in e["params"]) or "—"
        lines.append(
            f"| `{code}` | {e['status']} | {params} | `{e['remediation']}` | {text} |"
        )
    return "\n".join(lines) + "\n"


def error_docs_filename(language: str) -> str:
    return f"errors.{language}.md"


def build_error_docs(
    language: str,
    *,
    catalog_dirs=None,
    translations_dir: Path | str | None = None,
    source_language: str = "en",
) -> str:
    """Render the reference for *language* from the live registry + catalog.

    The catalog is loaded from *translations_dir* if given (single module),
    else discovered over INSTALLED_APPS via *catalog_dirs*.
    """
    from stapel_core.django.api.errors import build_error_registry

    entries = build_error_registry()
    if language == source_language:
        catalog = {}
    elif translations_dir is not None:
        catalog = load_catalog_file(Path(translations_dir) / f"errors.{language}.json")
    else:
        catalog = load_app_catalogs("errors", language, dirs=catalog_dirs)
    return render_error_docs(entries, language, catalog, source_language)


__all__ = [
    "LANGUAGE_NAMES",
    "build_error_docs",
    "error_docs_filename",
    "render_error_docs",
]
