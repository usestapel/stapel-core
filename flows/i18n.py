"""Flow i18n — keys, per-app catalogs and the translator seam.

flow-system.md §2: flow texts are i18n keys, not literals. Every flow/step
derives an implicit key (``flow.<id>.title`` / ``flow.<id>.description`` /
``flow.<id>.step.<order>.note``) unless an explicit ``*_key`` was passed;
the in-code literal remains the canonical source text and the fallback, so
literal-only flows keep working unchanged.

The catalog mechanics (per-app ``translations/<domain>.<lang>.json`` discovery
+ later-wins merge, the ``DOC_TRANSLATOR`` seam, the content-hash cache) are
the domain-agnostic :mod:`stapel_core.i18n` contour; this module is the
``"flows"`` domain over it.

``resolve_flow_texts(flows, language)`` builds the key → text mapping the doc
renderers consume. Resolution chain for language X:

1. **Committed per-app catalogs** — ``<app>/translations/flows.<X>.json``
   discovered over INSTALLED_APPS (en/ru ship with the module, reviewed as
   code). Catalogs are authoritative for the languages they cover.
2. **``translate.resolve`` comm Function** (best-effort) — host-project
   values from the translate module fill keys the catalogs do not cover.
3. **DOC_TRANSLATOR seam** (opt-in, ``llm=True``) — on-demand machine
   translation for the remaining keys, guarded by a content-hash cache:
   re-generation without source changes performs **zero** LLM calls and
   produces **zero** diff (same byte-stable discipline as
   ``dump_translations``).
4. **The source literal** — nothing is ever rendered empty.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

from stapel_core.i18n import DocTranslationCache
from stapel_core.i18n import load_app_catalogs as _load_app_catalogs

from .registry import Flow

logger = logging.getLogger(__name__)

TRANSLATE_RESOLVE_FUNCTION = "translate.resolve"


def flow_source_texts(flows: Iterable[Flow]) -> dict[str, str]:
    """key → canonical source literal for every text of *flows*."""
    texts: dict[str, str] = {}
    for f in flows:
        texts[f.title_key] = f.title
        texts[f.description_key] = f.description
        for s in f.sorted_steps():
            texts[s.note_key] = s.note
    return texts


def load_app_catalogs(
    language: str, dirs: Iterable[Path | str] | None = None
) -> dict[str, str]:
    """Merge ``translations/flows.<language>.json`` catalogs (later-wins).

    Thin wrapper over :func:`stapel_core.i18n.load_app_catalogs` for the
    ``"flows"`` domain.
    """
    return _load_app_catalogs("flows", language, dirs=dirs)


def resolve_flow_texts(
    flows: Iterable[Flow],
    language: str | None,
    *,
    use_translate_function: bool = True,
    llm: bool = False,
    cache_path: Path | str | None = None,
    translator=None,
    catalog_dirs: Iterable[Path | str] | None = None,
) -> dict[str, str]:
    """Resolve every i18n key of *flows* to a text in *language*.

    Returns a complete ``{key: text}`` mapping — keys nothing could resolve
    map to their source literal (render never breaks). ``language=None``/""
    short-circuits to the literals.

    ``llm=True`` enables the DOC_TRANSLATOR seam for keys missing from the
    catalogs and the translate module; pass ``cache_path`` to make it
    byte-stable across regenerations (content-hash cache, committed).
    *translator* overrides the ``STAPEL_FLOWS["DOC_TRANSLATOR"]`` seam
    instance (tests / programmatic use).
    """
    flows = list(flows)
    source = flow_source_texts(flows)
    texts = dict(source)
    if not language:
        return texts

    resolved = load_app_catalogs(language, dirs=catalog_dirs)
    resolved = {k: v for k, v in resolved.items() if k in source}

    missing = [k for k in source if k not in resolved]
    if missing and use_translate_function:
        try:
            from stapel_core.comm import call

            result = call(TRANSLATE_RESOLVE_FUNCTION,
                          {"keys": missing, "language": language})
            values = (result or {}).get("values") or {}
            wanted = set(missing)
            resolved.update({
                k: v for k, v in values.items()
                if k in wanted and isinstance(v, str) and v
            })
        except Exception:
            logger.debug("%s unavailable — skipping DB-backed flow texts for %r",
                         TRANSLATE_RESOLVE_FUNCTION, language, exc_info=True)

    missing = [k for k in source if k not in resolved]
    if missing and llm:
        cache = DocTranslationCache(cache_path) if cache_path else None
        to_translate: dict[str, str] = {}
        for k in missing:
            cached = cache.get(k, source[k]) if cache else None
            if cached is not None:
                resolved[k] = cached
            else:
                to_translate[k] = source[k]
        if to_translate:
            if translator is None:
                from .conf import flows_settings

                translator = flows_settings.DOC_TRANSLATOR()
            out = translator.translate(
                to_translate, _source_language(), language,
            ) or {}
            for k, v in out.items():
                if k in to_translate and isinstance(v, str) and v:
                    resolved[k] = v
                    if cache:
                        cache.put(k, source[k], v)
        if cache:
            cache.save()

    texts.update(resolved)
    return texts


def _source_language() -> str:
    from .conf import flows_settings

    return flows_settings.DOC_SOURCE_LANGUAGE


__all__ = [
    "flow_source_texts",
    "load_app_catalogs",
    "resolve_flow_texts",
]
