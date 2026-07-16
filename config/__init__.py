"""stapel_core.config — one config seam over the secret seam.

``get_config(key)`` is the single entry point a project's ``settings`` module
uses to read a configuration value, whatever its *source*. It is a thin
generalization of :func:`stapel_core.secrets.get_secret` (the vault|env
provider seam), not a second store:

- a key whose source is ``env`` is read from ``os.environ`` — it lives in the
  environment by design and must stay there even when the project points its
  secret provider at Vault (a broker URL, a log level, an allowed-hosts list);
- a key whose source is ``vault`` is delegated to ``get_secret`` — the
  provider seam decides whether that is really OpenBao/Vault or (the default
  ``EnvSecretProvider``) the environment. A bare project therefore behaves
  exactly as before: both sources end up reading ``os.environ``.

The routing table is a **CONFIG.MD manifest** — a markdown registry checked in
at the project root (and one shipped by every stapel lib, aggregated at
scaffold time; see ``stapel_tools.config_lint`` / ``assemble_scaffold``). Each
row declares one key:

    | Key | Source | Purpose | Required | Default |
    |-----|--------|---------|----------|---------|
    | SECRET_KEY | vault | Django secret key | yes | |
    | LOG_LEVEL  | env   | Root log level    | no  | INFO |

``## <owner>`` headings above a table group its rows by the lib that owns them
(``## stapel-core`` for core's keys, the project's own section for the rest) —
the aggregator and the linter use the owner to tell library-provided keys
(read inside the lib) from project-owned ones (which must be read in the
project).

Fail-closed, exactly like ``get_secret``: a **required** key that resolves to
nothing and has no default is a hard :class:`ConfigUnavailable`, never a
silent ``None`` that boots a half-configured service. A ``vault`` key resolved
through a real (fail-closed) provider raises inside ``get_secret`` already;
this module extends the same discipline to ``env`` keys.

The manifest itself is discovered once (``STAPEL_CONFIG_MANIFEST`` env var → a
CONFIG.MD found by walking up from the cwd) and cached; tests pass an explicit
``manifest=`` and never touch global state. Pointing ``get_config`` at a
manifest it has never parsed is the whole configuration act — there is no
runtime that can flip a key's source out from under the settings module.
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from stapel_core.secrets import SecretUnavailable, get_secret

#: Sentinel distinguishing "no default supplied" from ``default=None``.
_UNSET = object()

#: Env var naming an explicit CONFIG.MD path — the intentional override, honored
#: before the cwd-walk discovery. Mirrors ``STAPEL_SECRETS_PROVIDER``.
MANIFEST_ENV = "STAPEL_CONFIG_MANIFEST"

#: Canonical registry filename.
CONFIG_MD = "CONFIG.MD"

SOURCE_ENV = "env"
SOURCE_VAULT = "vault"
_SOURCES = (SOURCE_ENV, SOURCE_VAULT)

_TRUTHY = {"yes", "y", "true", "1", "да", "required", "req"}
_FALSY = {"no", "n", "false", "0", "нет", "optional", "opt", ""}
#: default-cell placeholders that mean "no default"
_NO_DEFAULT = {"", "-", "—", "–", "none", "n/a", "нет"}


class ConfigError(Exception):
    """Base class for config-manifest problems."""


class ConfigUnavailable(ConfigError):
    """A required config key resolved to nothing and no default was supplied."""

    def __init__(self, key: str, source: str | None = None) -> None:
        self.key = key
        self.source = source
        detail = f" (source {source})" if source else ""
        super().__init__(
            f"required config {key!r}{detail} is unset and has no default. A "
            f"required key must fail closed, never boot a half-configured "
            f"service on a silent None."
        )


class ConfigKeyUnknown(ConfigError):
    """A key asked for is not declared in the CONFIG.MD manifest."""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(
            f"config {key!r} is not declared in any CONFIG.MD manifest — it "
            f"cannot be routed to env or vault. Add a row for it (the "
            f"config-lint CFG002 gate enforces this) or pass an explicit "
            f"default to get_config()."
        )


class ConfigManifestError(ConfigError):
    """A CONFIG.MD row is malformed (bad source, missing key, …)."""


class ConfigNotDeclared(ConfigError):
    """A ``required=True`` call site has no manifest row and no default.

    Distinct from :class:`ConfigKeyUnknown` (that one fires for *any* undeclared
    key, required or not, when no default is given either): this fires only
    when the caller explicitly marked the key required at the call site — a
    bootstrap escape hatch for a key that has not made it into CONFIG.MD yet,
    still fail-closed rather than silently unknown.
    """


@dataclass(frozen=True)
class ConfigEntry:
    """One declared config key."""

    key: str
    source: str          # "env" | "vault"
    purpose: str = ""
    required: bool = False
    default: Optional[str] = None
    owner: Optional[str] = None   # nearest "## <owner>" heading, or None
    line: int = 0                 # 1-based line in the CONFIG.MD it came from


# --- CONFIG.MD parsing ------------------------------------------------------


_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")


def _cell(value: str) -> str:
    return value.replace("\\|", "|").strip().strip("`").strip()


def _parse_bool(value: str) -> bool:
    token = _cell(value).lower()
    if token in _TRUTHY:
        return True
    if token in _FALSY:
        return False
    # Unknown token — treat presence of "yes"/"да" substring conservatively,
    # else default to not-required (the safe, non-fail-closed reading is a
    # deliberate choice: a typo must not silently make a key mandatory).
    return False


def _parse_default(value: str) -> Optional[str]:
    token = _cell(value)
    if token.lower() in _NO_DEFAULT:
        return None
    return token


def parse_config_md(source: str | Path, *, path_label: str | None = None) -> dict[str, ConfigEntry]:
    """Parse a CONFIG.MD (path or text) into ``{key: ConfigEntry}``.

    Recognizes any GitHub-flavored markdown table whose header contains a
    ``key`` and a ``source`` column (case-insensitive); ``purpose`` /
    ``required`` / ``default`` columns are optional. ``## <owner>`` headings
    tag the rows beneath them. A row with an unknown source is a
    :class:`ConfigManifestError` — a value that can be routed to neither env
    nor vault is not lintable and must fail loudly at parse time.
    """
    if isinstance(source, Path) or (isinstance(source, str) and "\n" not in source and source.endswith(".MD")):
        p = Path(source)
        text = p.read_text(encoding="utf-8")
        label = path_label or str(p)
    else:
        text = str(source)
        label = path_label or "<config.md>"

    entries: dict[str, ConfigEntry] = {}
    owner: Optional[str] = None
    header: Optional[list[str]] = None
    col: dict[str, int] = {}

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            heading = line.lstrip("#").strip()
            # A level-2 heading names the owning lib; a level-1 title clears
            # it; deeper (### …) headings are subsections that keep the owner.
            if level == 2:
                owner = heading or None
            elif level == 1:
                owner = None
            header = None
            continue
        if not line.startswith("|"):
            header = None  # a table ends at the first non-row line
            continue

        cells = [c for c in _split_row(line)]
        if _is_separator_row(cells):
            continue
        lowered = [c.strip().lower() for c in cells]
        if header is None:
            # This is a header row iff it names key + source columns.
            if "key" in lowered and "source" in lowered:
                header = lowered
                col = {name: i for i, name in enumerate(header)}
            continue

        # data row
        def _get(name: str) -> str:
            i = col.get(name)
            return cells[i] if i is not None and i < len(cells) else ""

        key = _cell(_get("key"))
        if not key:
            continue
        source_val = _cell(_get("source")).lower()
        if source_val not in _SOURCES:
            raise ConfigManifestError(
                f"{label}:{lineno}: config key {key!r} has source "
                f"{source_val!r}; expected one of {', '.join(_SOURCES)}."
            )
        entries[key] = ConfigEntry(
            key=key,
            source=source_val,
            purpose=_cell(_get("purpose")),
            required=_parse_bool(_get("required")),
            default=_parse_default(_get("default")),
            owner=owner,
            line=lineno,
        )
    return entries


def _split_row(line: str) -> list[str]:
    parts = _UNESCAPED_PIPE.split(line.strip())
    if parts and parts[0] == "":
        parts = parts[1:]
    if parts and parts[-1] == "":
        parts = parts[:-1]
    return parts


def _is_separator_row(cells: list[str]) -> bool:
    stripped = [c.strip() for c in cells]
    return bool(stripped) and all(
        set(c) <= {"-", ":"} and "-" in c for c in stripped if c
    ) and any(c for c in stripped)


# --- manifest discovery + cache ---------------------------------------------

_cache_lock = threading.Lock()
_manifest_cache: dict[str, dict[str, ConfigEntry]] = {}


def discover_manifest_path(start: Path | None = None) -> Optional[Path]:
    """Locate the active CONFIG.MD: ``STAPEL_CONFIG_MANIFEST`` if set, else the
    first ``CONFIG.MD`` found walking up from *start* (cwd)."""
    explicit = os.environ.get(MANIFEST_ENV)
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None
    here = (start or Path.cwd()).resolve()
    for base in (here, *here.parents):
        cand = base / CONFIG_MD
        if cand.is_file():
            return cand
    return None


def load_manifest(path: Path | None = None) -> dict[str, ConfigEntry]:
    """Parse and cache the CONFIG.MD manifest.

    Without *path*, discovers it (see :func:`discover_manifest_path`). A
    missing manifest resolves to an empty mapping — ``get_config`` then falls
    back to caller defaults and treats every undeclared key as unknown.
    """
    resolved = path or discover_manifest_path()
    if resolved is None:
        return {}
    key = str(Path(resolved).resolve())
    with _cache_lock:
        cached = _manifest_cache.get(key)
        if cached is not None:
            return cached
    parsed = parse_config_md(Path(resolved))
    with _cache_lock:
        _manifest_cache[key] = parsed
    return parsed


def reset_manifest_cache() -> None:
    """Drop the parsed-manifest cache (tests / after editing CONFIG.MD)."""
    with _cache_lock:
        _manifest_cache.clear()


# --- call-site declarations (a code-first source for the regenerator) ------
#
# CONFIG.MD is hand-maintained today: purpose/required/default live only in
# the markdown table, so they silently drift from the code that actually
# reads the key (a default changed in code, forgotten in the table — exactly
# what happened to STAPEL_BUS_BACKEND's row when its in-code default moved
# from kafka to memory, 0.11.0). ``declare_config`` (or the ``purpose=``/
# ``required=`` kwargs on :func:`get_config`, which call it as a backstop —
# the same "declare explicitly, or record lazily on first use" shape as
# ``stapel_core.django.swappable.declare_swap``) gives a future CONFIG.MD
# regenerator a code-sourced registry to cross-check or rebuild the table
# from, instead of trusting hand-written cells.

_declared: dict[str, ConfigEntry] = {}
_declared_lock = threading.Lock()


def declare_config(
    key: str,
    *,
    source: str = SOURCE_ENV,
    purpose: str = "",
    required: bool = False,
    default: str | None = None,
) -> None:
    """Register *key*'s metadata from code (first declaration wins).

    Call once at import time, next to the settings.py line that reads the
    key — independent of whether :func:`get_config` is ever called for it.
    Never overwrites an existing declaration (re-import safe) and never
    touches the CONFIG.MD-parsed manifest (that stays the authoritative
    source when both exist; see :func:`declared_config_entries`).
    """
    if source not in _SOURCES:
        raise ConfigManifestError(
            f"declare_config({key!r}): unknown source {source!r}, expected "
            f"one of {', '.join(_SOURCES)}"
        )
    with _declared_lock:
        _declared.setdefault(
            key,
            ConfigEntry(key=key, source=source, purpose=purpose, required=required, default=default),
        )


def declared_config_entries() -> dict[str, ConfigEntry]:
    """Snapshot of call-site declarations — the regenerator's raw material."""
    with _declared_lock:
        return dict(_declared)


def clear_declared_config() -> None:
    """Drop all call-site declarations (test isolation)."""
    with _declared_lock:
        _declared.clear()


# --- the entry point --------------------------------------------------------


def get_config(
    key: str,
    default: object = _UNSET,
    *,
    purpose: str = "",
    required: bool | None = None,
    manifest: dict[str, ConfigEntry] | None = None,
) -> str | None:
    """Resolve config *key* through the CONFIG.MD manifest.

    Routing: ``env`` source reads ``os.environ``; ``vault`` source delegates to
    :func:`stapel_core.secrets.get_secret` (provider seam). The effective
    default is the caller's *default* if given, else the manifest row's
    default. A required key that resolves to nothing with no effective default
    raises :class:`ConfigUnavailable`.

    A key absent from the manifest raises :class:`ConfigKeyUnknown` unless the
    caller supplied a *default* — with one exception: passing
    ``required=True`` explicitly (a call site ahead of its CONFIG.MD row, or a
    project-local key an aggregator would not otherwise see) still fails
    closed on a missing value (:class:`ConfigNotDeclared`) instead of raising
    "unknown key" — required is required, declared in the table or not.

    ``purpose``/``required`` passed here (whether or not the key is already
    in the manifest) are also recorded via :func:`declare_config` — a
    backstop declaration for a future CONFIG.MD regenerator, the same
    "declare explicitly, or record lazily on first use" shape as
    ``declare_swap``. They never change resolution when a manifest row
    already exists (the table stays authoritative); passing them is free
    metadata, not a second source of truth to reconcile by hand.
    """
    if purpose or required is not None:
        declare_config(key, purpose=purpose, required=bool(required), default=None)

    table = load_manifest() if manifest is None else manifest
    entry = table.get(key)

    if entry is None:
        if default is not _UNSET:
            return default  # type: ignore[return-value]
        if required:
            value = os.environ.get(key)
            if value is not None:
                return value
            raise ConfigNotDeclared(
                f"required config {key!r} is unset, has no default, and has "
                f"no CONFIG.MD row yet — set the environment variable, or "
                f"pass a default, or add the row."
            )
        raise ConfigKeyUnknown(key)

    if default is not _UNSET:
        eff_default: object = default
    elif entry.default is not None:
        eff_default = entry.default
    else:
        eff_default = _UNSET

    if entry.source == SOURCE_VAULT:
        if eff_default is not _UNSET:
            # get_secret honors the default for any provider (fail-open).
            return get_secret(key, eff_default)
        try:
            value = get_secret(key)  # fail-closed for real providers
        except SecretUnavailable as exc:
            raise ConfigUnavailable(key, SOURCE_VAULT) from exc
    else:  # env
        value = os.environ.get(key)

    if value is not None:
        return value
    if eff_default is not _UNSET:
        return eff_default  # type: ignore[return-value]
    if entry.required:
        raise ConfigUnavailable(key, entry.source)
    return None


__all__ = [
    "CONFIG_MD",
    "MANIFEST_ENV",
    "SOURCE_ENV",
    "SOURCE_VAULT",
    "ConfigEntry",
    "ConfigError",
    "ConfigKeyUnknown",
    "ConfigManifestError",
    "ConfigNotDeclared",
    "ConfigUnavailable",
    "discover_manifest_path",
    "get_config",
    "load_manifest",
    "parse_config_md",
    "reset_manifest_cache",
    "declare_config",
    "declared_config_entries",
    "clear_declared_config",
]
