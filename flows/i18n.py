"""Flow i18n — keys, per-app catalogs and the DOC_TRANSLATOR seam.

flow-system.md §2: flow texts are i18n keys, not literals. Every flow/step
derives an implicit key (``flow.<id>.title`` / ``flow.<id>.description`` /
``flow.<id>.step.<order>.note``) unless an explicit ``*_key`` was passed;
the in-code literal remains the canonical source text and the fallback, so
literal-only flows keep working unchanged.

``resolve_flow_texts(flows, language)`` builds the key → text mapping the
doc renderers consume. Resolution chain for language X:

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

import hashlib
import json
import logging
from pathlib import Path
from typing import Iterable

from .registry import Flow

logger = logging.getLogger(__name__)

#: Directory (inside an app package) holding flow catalogs, one file per
#: language: ``translations/flows.<lang>.json`` mapping key → text.
CATALOG_DIRNAME = "translations"

TRANSLATE_RESOLVE_FUNCTION = "translate.resolve"
LLM_TRANSLATE_FUNCTION = "llm.translate"


def flow_source_texts(flows: Iterable[Flow]) -> dict[str, str]:
    """key → canonical source literal for every text of *flows*."""
    texts: dict[str, str] = {}
    for f in flows:
        texts[f.title_key] = f.title
        texts[f.description_key] = f.description
        for s in f.sorted_steps():
            texts[s.note_key] = s.note
    return texts


def _installed_app_dirs() -> list[Path]:
    from django.apps import apps

    return [Path(ac.path) for ac in apps.get_app_configs()]


def load_app_catalogs(language: str, dirs: Iterable[Path | str] | None = None) -> dict[str, str]:
    """Merge ``translations/flows.<language>.json`` catalogs.

    *dirs* defaults to the package directories of all installed apps. Keys
    are flow-namespaced (``flow.<flow_id>.…``), so cross-app collisions are
    pathological; on collision the later app wins (INSTALLED_APPS order —
    the same merge-over-builtins semantics as other stapel registries).
    """
    merged: dict[str, str] = {}
    app_dirs = list(dirs) if dirs is not None else _installed_app_dirs()
    for d in app_dirs:
        path = Path(d) / CATALOG_DIRNAME / f"flows.{language}.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("unreadable flow catalog %s — skipped", path, exc_info=True)
            continue
        if not isinstance(data, dict):
            logger.warning("flow catalog %s is not a JSON object — skipped", path)
            continue
        merged.update({
            k: v for k, v in data.items()
            if isinstance(k, str) and isinstance(v, str) and v
        })
    return merged


class CommDocTranslator:
    """Default DOC_TRANSLATOR: ``llm.translate`` called by comm name.

    Core never imports the agent package (L0 stays clean) — if no provider
    for ``llm.translate`` is registered/routable, translation is silently
    skipped and the caller falls back down the resolution chain.
    """

    def translate(
        self,
        entries: dict[str, str],
        source_language: str,
        target_language: str,
    ) -> dict[str, str]:
        from stapel_core.comm import call

        try:
            result = call(LLM_TRANSLATE_FUNCTION, {
                "from_lang": source_language or "auto",
                "to": target_language,
                "entries": dict(entries),
            })
        except Exception:
            logger.warning(
                "%s unavailable — flow docs fall back to source literals for %r",
                LLM_TRANSLATE_FUNCTION, target_language, exc_info=True,
            )
            return {}
        if not isinstance(result, dict) or result.get("status") != "ok":
            reason = (result or {}).get("reason") if isinstance(result, dict) else result
            logger.warning("%s failed: %r", LLM_TRANSLATE_FUNCTION, reason)
            return {}
        out = result.get("result") or {}
        return {k: v for k, v in out.items() if isinstance(v, str) and v}


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class DocTranslationCache:
    """Content-hash cache for DOC_TRANSLATOR output — a committed artifact.

    File format (sorted keys, 2-space indent, trailing newline — byte-stable
    like dump_translations): ``{key: {"hash": h(source_text), "text": t}}``.
    A cached value is reused only while the source literal's hash matches,
    so editing a flow text invalidates exactly that entry.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._entries: dict[str, dict[str, str]] = {}
        self._dirty = False
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._entries = {
                        k: v for k, v in data.items()
                        if isinstance(v, dict)
                        and isinstance(v.get("hash"), str)
                        and isinstance(v.get("text"), str)
                    }
            except (OSError, ValueError):
                logger.warning("unreadable doc-translation cache %s — starting empty",
                               self.path, exc_info=True)

    def get(self, key: str, source_text: str) -> str | None:
        entry = self._entries.get(key)
        if entry and entry["hash"] == _content_hash(source_text):
            return entry["text"]
        return None

    def put(self, key: str, source_text: str, text: str) -> None:
        entry = {"hash": _content_hash(source_text), "text": text}
        if self._entries.get(key) != entry:
            self._entries[key] = entry
            self._dirty = True

    def save(self) -> bool:
        """Write the cache file iff something changed. Returns True on write."""
        if not self._dirty:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            dict(sorted(self._entries.items())),
            ensure_ascii=False, indent=2, sort_keys=True,
        )
        self.path.write_text(payload + "\n", encoding="utf-8")
        self._dirty = False
        return True


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
    "CATALOG_DIRNAME",
    "CommDocTranslator",
    "DocTranslationCache",
    "flow_source_texts",
    "load_app_catalogs",
    "resolve_flow_texts",
]
