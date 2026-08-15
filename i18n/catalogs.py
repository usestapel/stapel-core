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
from typing import Callable, Iterable

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


def catalog_search_dirs() -> list[Path]:
    """Every root the loader looks under: app packages + ``EXTRA_CATALOG_DIRS``.

    The single source of truth for "where a catalog can live" — both
    :func:`load_app_catalogs` (read side) and :func:`resolve_catalog_dir`
    (write side) go through it, so the two can never disagree about where a
    catalog is visible from.
    """
    return _installed_app_dirs() + _extra_catalog_dirs()


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
        app_dirs = catalog_search_dirs()
    for d in app_dirs:
        merged.update(load_catalog_file(Path(d) / catalog_relpath(domain, language)))
    return merged


def _owner_app_dirs() -> list[tuple[str, Path]]:
    """``(owning package, app package dir)`` for every installed app.

    The owner is the app's *top-level* package — the unit a key's owner is
    named by in the error registry (``stapel_core``, ``stapel_profiles``), and
    the unit that gets released. An app deeper in a distribution
    (``stapel_core.django``) still belongs to its distribution's package.
    """
    from django.apps import apps

    return [(ac.name.split(".")[0], Path(ac.path)) for ac in apps.get_app_configs()]


def owner_of_dir(path: Path | str) -> str | None:
    """Which package owns the catalogs in *path* (a ``translations`` dir).

    Resolved against INSTALLED_APPS: a ``translations`` directory belongs to
    the app package that contains it. ``None`` when *path* is not an installed
    app's catalog directory (a tmp_path unit test, an ``EXTRA_CATALOG_DIRS``
    root) — callers treat that as "ownership unknown" and fall back to the
    unscoped behaviour rather than guessing.
    """
    target = Path(path).resolve()
    if target.name == CATALOG_DIRNAME:
        target = target.parent
    try:
        pairs = _owner_app_dirs()
    except Exception:  # apps not ready — no ownership to resolve
        return None
    for owner, app_dir in pairs:
        if app_dir.resolve() == target:
            return owner
    return None


def owner_languages(owner: str, domain: str) -> set[str]:
    """The languages *owner* ships *domain* catalogs in, over INSTALLED_APPS.

    "Does the owner translate at all, and into what" — the fact the pairing
    gate needs before it can demand a code from the owner's catalog: a package
    that ships a language answers for every key it owns in that language,
    while a package that ships nothing has made no translation claim to break.
    """
    langs: set[str] = set()
    try:
        pairs = _owner_app_dirs()
    except Exception:
        return langs
    prefix = f"{domain}."
    for pkg, app_dir in pairs:
        if pkg != owner:
            continue
        catalog_dir = app_dir / CATALOG_DIRNAME
        if not catalog_dir.is_dir():
            continue
        for path in catalog_dir.glob(f"{prefix}*.json"):
            lang = path.name[len(prefix):-len(".json")]
            if lang:
                langs.add(lang)
    return langs


def owner_catalog(owner: str, domain: str, language: str) -> dict[str, str]:
    """Merge every catalog *owner* ships for *domain* / *language*.

    "What the owner actually publishes in this language" — the fact the gate
    needs before calling a module's entry a shadow: covering a key its owner
    does not translate is gap-filling, not shadowing.
    """
    merged: dict[str, str] = {}
    try:
        pairs = _owner_app_dirs()
    except Exception:
        return merged
    for pkg, app_dir in pairs:
        if pkg == owner:
            merged.update(load_catalog_file(app_dir / catalog_relpath(domain, language)))
    return merged


def module_catalog(
    domain: str,
    language: str,
    translations_dir: Path | str,
    *,
    keys: Iterable[str] | None = None,
    owner: str | None = None,
    owners: dict[str, str] | None = None,
    owner_catalogs: Callable[[str, str, str], dict[str, str]] | None = None,
) -> dict[str, str]:
    """One module's catalog as a READER resolves it — its texts, then the owner's.

    The write side stopped duplicating: since ownership scoping a module ships
    only the keys it owns, and the runtime is unaffected because
    :func:`load_app_catalogs` merges every installed app's catalog, the owner's
    included. Anything reading a *single* module's ``translations/`` directory
    on its own, however, sees exactly what pruning removed — a reference doc
    built that way silently drops the owner's keys back to their English
    fallback, which is the same duplication defect wearing a documentation
    costume: the reader was never taught what the writer now assumes.

    So resolution follows ownership rather than the directory listing:

    * a key the module ships wins (that is what a declared override IS — the
      runtime merge puts the module after its dependency too);
    * a key it does not own is read from the catalog of the package that does
      (:func:`owner_catalog`, over INSTALLED_APPS);
    * a key nobody owns, or whose owner ships no text in *language*, is absent
      here exactly as before — the caller renders its own honest fallback.

    A key the module *owns* is never back-filled from elsewhere: an owner's own
    gap is the coverage error :func:`check_translation_catalogs` reports, and
    filling it from a same-named package installed elsewhere would hide it.

    *keys* limits resolution to the canon being rendered (default: every key
    with a known owner). *owner* (defaulted from the directory, like the gate),
    *owners* and *owner_catalogs* are injectable, so a caller that already knows
    the answers does not pay for INSTALLED_APPS lookups twice.
    """
    directory = Path(translations_dir)
    own = load_catalog_file(directory / catalog_filename(domain, language))
    if owners is None:
        from .domains import source_owners

        owners = source_owners(domain)
    if not owners:
        return own
    if owner_catalogs is None:
        owner_catalogs = owner_catalog

    this_owner = owner_of_dir(directory) if owner is None else owner
    resolved = dict(own)
    upstream: dict[str, dict[str, str]] = {}
    for key in (owners if keys is None else keys):
        if key in resolved:
            continue
        key_owner = owners.get(key)
        if not key_owner or key_owner == this_owner:
            continue
        if key_owner not in upstream:
            upstream[key_owner] = owner_catalogs(key_owner, domain, language)
        text = upstream[key_owner].get(key)
        if text:
            resolved[key] = text
    return resolved


class CatalogDirError(ValueError):
    """The requested catalog directory is not one the loader would ever read."""


def _app_package_dir(app: str) -> Path:
    """The package directory of the installed app *app* (label or dotted name)."""
    from django.apps import apps

    for ac in apps.get_app_configs():
        if app in (ac.label, ac.name):
            return Path(ac.path)
    known = ", ".join(sorted(ac.label for ac in apps.get_app_configs()))
    raise CatalogDirError(f"{app!r} is not an installed app — known labels: {known}")


def _where_the_loader_looks(roots: list[Path]) -> str:
    shown = [str(r / CATALOG_DIRNAME) for r in roots[:6]]
    more = "" if len(roots) <= 6 else f" (+{len(roots) - 6} more)"
    return "\n".join(f"  - {s}" for s in shown) + more


def resolve_catalog_dir(
    out: Path | str | None = None,
    *,
    app: str | None = None,
    roots: Iterable[Path | str] | None = None,
    cwd: Path | str | None = None,
) -> Path:
    """The ``translations`` directory to WRITE, checked against the read side.

    Catalogs are found by walking the *package* directories of INSTALLED_APPS
    (:func:`load_app_catalogs`), never the working directory. A relative
    ``--out translations`` resolved against a service root therefore produced a
    directory the loader would never open: the command reported success and the
    catalog was invisible forever after.

    So the write target is derived, not assumed:

    * *app* given → that app package's ``translations/``;
    * *out* given → accepted only when it IS ``<root>/translations`` for one of
      the loader's roots; otherwise :class:`CatalogDirError`, loudly, naming
      the places the loader does look;
    * neither → the app package the command is run from (the working directory,
      or the nearest app package above it). Outside any app package there is no
      defensible default, so it raises rather than inventing one.
    """
    if roots is None:
        search = catalog_search_dirs()
    else:
        search = [Path(r) for r in roots]
    resolved_roots = [Path(r).resolve() for r in search]

    if app is not None:
        return _app_package_dir(app) / CATALOG_DIRNAME

    here = Path(cwd).resolve() if cwd is not None else Path.cwd().resolve()

    if out is None:
        # The nearest enclosing app package (running from a subpackage is fine).
        enclosing = [
            r for r in resolved_roots if r == here or r in here.parents
        ]
        if enclosing:
            return max(enclosing, key=lambda r: len(r.parts)) / CATALOG_DIRNAME
        raise CatalogDirError(
            f"cannot default the catalog directory: {here} is not an installed "
            f"app package, and a catalog outside one is never loaded. Pass "
            f"--app <label> (or --out <app package>/{CATALOG_DIRNAME}). The "
            f"loader reads:\n{_where_the_loader_looks(resolved_roots)}"
        )

    target = Path(out)
    if not target.is_absolute():
        target = here / target
    target = target.resolve()
    if target.name == CATALOG_DIRNAME and target.parent in resolved_roots:
        return target
    raise CatalogDirError(
        f"{target} is not a catalog directory the loader reads — a catalog "
        f"written there would never be found. Pass --app <label>, or point "
        f"--out at one of:\n{_where_the_loader_looks(resolved_roots)}"
    )


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

# Provenance vocabulary — two independent facts about a value, not one.
#
# WHO PRODUCED IT (the ``origin`` string in the sidecar):
#
# * ``llm``          — machine translation from the ``TRANSLATOR`` seam;
# * ``seed:<label>`` — lifted verbatim from a curated corpus (the
#   stapel-translate builtin fixtures). Curated and paid for, but still
#   machine-made: nobody read it on the way in;
# * ``imported``     — the value was already in the catalog file with no
#   sidecar row. Authorship unknown — a hand-written catalog and a machine
#   dump look identical on disk;
# * ``human``        — a person read the value and approved it
#   (``translate_catalogs --approve`` / ``--approve-all``).
#
# WHETHER A HUMAN SIGNED OFF (:func:`is_reviewed`) — true for ``human`` only.
# The two used to be conflated: everything that was not ``llm`` counted as
# reviewed, so routing a machine translation through ``--seed`` (the obvious
# path) drove the gate's unreviewed counter to zero for text no human had ever
# read. A counter that reads zero for unread text is worse than no counter.
#
# WHETHER IT MAY BE SILENTLY RE-DERIVED (:func:`is_curated`) — this is the
# other fact, and it is NOT the same one. A seeded value must not be quietly
# overwritten when the en source moves (the corpus was curated against the OLD
# English; re-seeding would hide the drift the gate exists to show), yet it is
# still unreviewed. ``translate_catalog`` asks ``is_curated``; the gate asks
# ``is_reviewed``.
ORIGIN_LLM = "llm"
ORIGIN_HUMAN = "human"
ORIGIN_IMPORTED = "imported"

#: ``seed:<label>`` — a value lifted from the curated corpus *<label>*.
ORIGIN_SEED_PREFIX = "seed:"


def seed_origin(label: str) -> str:
    """The provenance string for a value taken from corpus *label*."""
    return f"{ORIGIN_SEED_PREFIX}{label}"


def is_seeded(origin: str | None) -> bool:
    """True for ``seed:<label>`` — a curated corpus value (machine-made)."""
    return bool(origin) and origin.startswith(ORIGIN_SEED_PREFIX)


def is_reviewed(origin: str | None) -> bool:
    """True only when a human signed the value off (``origin: human``).

    Machine output — ``llm`` and every ``seed:<label>`` — is NOT reviewed, and
    neither is an ``imported`` value of unknown authorship. This is what the
    gate's ``unreviewed`` warning counts, so the count means "nobody has read
    these", never "these came from somewhere respectable".
    """
    return bool(origin) and (
        origin == ORIGIN_HUMAN or origin.startswith(f"{ORIGIN_HUMAN}:")
    )


def is_curated(origin: str | None) -> bool:
    """True when the value was placed deliberately — never re-derive it silently.

    Human approvals, curated-corpus seeds and imported hand-written catalogs
    all qualify: when the en source moves under such a value, the value stays
    put and the gate reports it stale. Only raw ``llm`` output (and a value
    with no provenance at all) may be regenerated without asking.
    """
    return is_reviewed(origin) or is_seeded(origin) or origin == ORIGIN_IMPORTED


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
        """Record hash + origin, preserving fields this call does not manage.

        A wholesale overwrite here would silently drop an
        :meth:`declare_override` declaration on the next retranslation — the
        gate would then flag a deliberate override as stale copy-paste, which
        is exactly the distinction the declaration exists to make.
        """
        section = self._data.setdefault(self._section(domain, language), {})
        entry = dict(section.get(key) or {})
        entry.update({"hash": source_hash, "origin": origin})
        section[key] = entry

    def declare_override(self, domain: str, language: str, key: str,
                         *, owner: str) -> None:
        """Mark *key* as a deliberate override of *owner*'s text (§3).

        The declaration lives here rather than in the catalog because the
        catalog must stay a flat ``{key: text}`` map — the runtime merge, the
        frontend ``gen-errors.mjs`` and human readers all depend on that. The
        owner is named in the value so the declaration is self-describing in a
        review diff and so the gate can spot one that outlived its owner.
        """
        section = self._data.setdefault(self._section(domain, language), {})
        entry = dict(section.get(key) or {})
        entry["override"] = owner
        section[key] = entry

    def overrides(self, domain: str, language: str) -> dict[str, str]:
        """``{key: declared owner}`` for the deliberate overrides in a locale."""
        section = self._data.get(self._section(domain, language), {})
        return {
            k: v["override"]
            for k, v in section.items()
            if isinstance(v, dict) and isinstance(v.get("override"), str)
        }

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
    "ORIGIN_IMPORTED",
    "ORIGIN_SEED_PREFIX",
    "CatalogDirError",
    "CommDocTranslator",
    "DocTranslationCache",
    "StateSidecar",
    "catalog_filename",
    "catalog_relpath",
    "catalog_search_dirs",
    "content_hash",
    "dump_catalog",
    "is_curated",
    "is_reviewed",
    "is_seeded",
    "load_app_catalogs",
    "load_catalog_file",
    "module_catalog",
    "owner_catalog",
    "owner_languages",
    "owner_of_dir",
    "resolve_catalog_dir",
    "seed_origin",
]
