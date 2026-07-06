"""Domain-agnostic i18n catalogs, the translator seam and the provenance sidecar.

This is the generalization of the flow-i18n contour (``flows/i18n.py``) to
arbitrary content *domains* (i18n-shipping.md §1). A domain ``D`` (``"flows"``,
``"errors"``, …) ships per-app catalogs ``<app>/translations/D.<lang>.json`` —
flat ``{key: text}`` — discovered over INSTALLED_APPS and merged **later-wins**
(the host app, last in INSTALLED_APPS, overrides module texts without a fork).
The same merge-over-builtins semantics as every other stapel registry.

Byte-stable file format everywhere (sorted keys, 2-space indent,
``ensure_ascii=False``, single trailing newline) — the ``dump_translations``
discipline — so a no-op regeneration is a no-op diff and drift gates are
meaningful.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

#: Directory (inside an app package) holding catalogs, one file per domain and
#: language: ``translations/<domain>.<lang>.json`` mapping key → text.
CATALOG_DIRNAME = "translations"

#: The provenance sidecar next to the catalogs (i18n-shipping.md §5). Read only
#: by tooling (``translate_catalogs`` / ``check_translation_catalogs``); the
#: catalogs themselves stay flat ``{key: text}`` for runtime + gen-errors + humans.
STATE_FILENAME = ".state.json"

LLM_TRANSLATE_FUNCTION = "llm.translate"


def content_hash(text: str) -> str:
    """Stable 16-hex content hash of a source text (invalidates one entry)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def catalog_filename(domain: str, language: str) -> str:
    """``<domain>.<language>.json`` — the file inside a ``translations/`` dir."""
    return f"{domain}.{language}.json"


def catalog_relpath(domain: str, language: str) -> str:
    """``translations/<domain>.<language>.json`` — app-package-relative path."""
    return f"{CATALOG_DIRNAME}/{catalog_filename(domain, language)}"


def dump_catalog(mapping: dict[str, str]) -> str:
    """Byte-stable JSON string for a flat catalog (sorted keys, trailing NL)."""
    return json.dumps(
        {k: mapping[k] for k in sorted(mapping)},
        ensure_ascii=False, indent=2, sort_keys=True,
    ) + "\n"


def load_catalog_file(path: Path | str) -> dict[str, str]:
    """Read one catalog file → ``{key: text}`` (empty/broken → ``{}``)."""
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("unreadable catalog %s — skipped", path, exc_info=True)
        return {}
    if not isinstance(data, dict):
        logger.warning("catalog %s is not a JSON object — skipped", path)
        return {}
    return {
        k: v for k, v in data.items()
        if isinstance(k, str) and isinstance(v, str) and v
    }


def _installed_app_dirs() -> list[Path]:
    from django.apps import apps

    return [Path(ac.path) for ac in apps.get_app_configs()]


def _extra_catalog_dirs() -> list[Path]:
    try:
        from .conf import i18n_settings

        return [Path(d) for d in (i18n_settings.EXTRA_CATALOG_DIRS or [])]
    except Exception:  # settings not ready / namespace unusable
        return []


def load_app_catalogs(
    domain: str,
    language: str,
    dirs: Iterable[Path | str] | None = None,
) -> dict[str, str]:
    """Merge ``translations/<domain>.<language>.json`` catalogs, later-wins.

    *dirs* defaults to every installed app's package directory plus
    ``STAPEL_I18N["EXTRA_CATALOG_DIRS"]``. On key collision the later dir wins
    (INSTALLED_APPS order — the host app, last, overrides module texts). Empty
    / non-string values are dropped so a stub entry never shadows a real one.
    """
    merged: dict[str, str] = {}
    if dirs is not None:
        app_dirs = [Path(d) for d in dirs]
    else:
        app_dirs = _installed_app_dirs() + _extra_catalog_dirs()
    for d in app_dirs:
        merged.update(load_catalog_file(Path(d) / catalog_relpath(domain, language)))
    return merged


class CommDocTranslator:
    """Default translator seam: ``llm.translate`` called by comm name.

    Core never imports the agent package (L0 stays clean) — if no provider for
    ``llm.translate`` is registered/routable, translation is silently skipped
    and the caller falls back down the resolution chain / leaves the key unset.
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
                "%s unavailable — catalog translation falls back for %r",
                LLM_TRANSLATE_FUNCTION, target_language, exc_info=True,
            )
            return {}
        if not isinstance(result, dict) or result.get("status") != "ok":
            reason = (result or {}).get("reason") if isinstance(result, dict) else result
            logger.warning("%s failed: %r", LLM_TRANSLATE_FUNCTION, reason)
            return {}
        out = result.get("result") or {}
        return {k: v for k, v in out.items() if isinstance(v, str) and v}


class DocTranslationCache:
    """Content-hash cache for translator output — a committed artifact.

    File format (sorted keys, 2-space indent, trailing newline — byte-stable
    like dump_translations): ``{key: {"hash": h(source_text), "text": t}}``. A
    cached value is reused only while the source text's hash matches, so
    editing a source literal invalidates exactly that entry.
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
        if entry and entry["hash"] == content_hash(source_text):
            return entry["text"]
        return None

    def put(self, key: str, source_text: str, text: str) -> None:
        entry = {"hash": content_hash(source_text), "text": text}
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


# ---------------------------------------------------------------------------
# Provenance sidecar (.state.json) — i18n-shipping.md §5
# ---------------------------------------------------------------------------

#: Provenance origins. ``llm`` = machine-translated, NOT human-reviewed (the
#: W-counter of ``check_translation_catalogs``); ``seed:<label>`` = lifted from
#: a curated corpus (e.g. stapel-translate builtin fixtures); ``human`` =
#: reviewed / hand-written (``translate_catalogs --approve``).
ORIGIN_LLM = "llm"
ORIGIN_HUMAN = "human"


def is_reviewed(origin: str | None) -> bool:
    """A value is reviewed unless it was machine-translated and untouched."""
    return bool(origin) and origin != ORIGIN_LLM


class StateSidecar:
    """The ``translations/.state.json`` provenance file, keyed ``<domain>.<lang>``.

    ``{"<domain>.<lang>": {"<key>": {"hash": h(source_en), "origin": "…"}}}``.
    ``hash`` is the content hash of the *source* (en) text at translation time:
    editing the canon automatically staleness-marks exactly that one key. Only
    tooling reads this — the catalog stays a flat ``{key: text}``.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._data: dict[str, dict[str, dict[str, str]]] = {}
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._data = data
            except (OSError, ValueError):
                logger.warning("unreadable state sidecar %s — starting empty",
                               self.path, exc_info=True)

    @staticmethod
    def _section(domain: str, language: str) -> str:
        return f"{domain}.{language}"

    def entries(self, domain: str, language: str) -> dict[str, dict[str, str]]:
        return dict(self._data.get(self._section(domain, language), {}))

    def get(self, domain: str, language: str, key: str) -> dict[str, str] | None:
        return self._data.get(self._section(domain, language), {}).get(key)

    def set(self, domain: str, language: str, key: str,
            *, source_hash: str, origin: str) -> None:
        section = self._data.setdefault(self._section(domain, language), {})
        section[key] = {"hash": source_hash, "origin": origin}

    def prune(self, domain: str, language: str, keep: Iterable[str]) -> None:
        """Drop provenance for keys no longer in the catalog / source."""
        keep = set(keep)
        section = self._data.get(self._section(domain, language))
        if section is None:
            return
        for gone in [k for k in section if k not in keep]:
            del section[gone]

    def render(self) -> str:
        """Byte-stable JSON (nested keys sorted, trailing newline)."""
        ordered = {
            sec: {k: self._data[sec][k] for k in sorted(self._data[sec])}
            for sec in sorted(self._data)
            if self._data[sec]
        }
        return json.dumps(ordered, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.render(), encoding="utf-8")


__all__ = [
    "CATALOG_DIRNAME",
    "STATE_FILENAME",
    "ORIGIN_LLM",
    "ORIGIN_HUMAN",
    "CommDocTranslator",
    "DocTranslationCache",
    "StateSidecar",
    "catalog_filename",
    "catalog_relpath",
    "content_hash",
    "dump_catalog",
    "is_reviewed",
    "load_app_catalogs",
    "load_catalog_file",
]
