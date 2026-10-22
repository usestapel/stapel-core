"""System check for the config manifest (tag ``stapel_config``).

E-level — a key declared ``required`` (CONFIG.MD row, or a call-site
``declare_config``/``get_config(required=True)`` declaration) must resolve to
a value or a default *before* the service is allowed to start. Without this,
"required" was only enforced the first time some code path happened to call
``get_config(key)`` — which might be deep in a request handler, minutes or
days after boot, and might never be called at all for a key nothing reads
through the config seam yet. ``manage.py check`` / boot-smoke is where a
half-configured service should die, not the first request that needs the
missing value.

An ``env``/``vault`` row whose value is absent from the environment and the
secrets provider still passes when the Django settings module defines the key
with a non-empty value: the settings the process actually runs on resolved it
(a ``SECRET_KEY = get_secret('SECRET_KEY', fallback)`` line, a value written in
``dev.py``). Whether that value is a placeholder is ``stapel_prodguard``'s
question, not this check's.

E002 is the ``settings``-source counterpart: a required row declared as a
Django setting must resolve in the owner's ``STAPEL_<MOD>`` dict, a flat
setting, or its default column. Settings rows never consult env or vault.
"""
from __future__ import annotations

from django.core import checks

E001_REQUIRED_CONFIG_MISSING = "stapel_core.config.E001"
E002_REQUIRED_SETTING_MISSING = "stapel_core.config.E002"


def _defined_in_settings(key: str) -> bool:
    """True when ``settings.<key>`` exists and is not ``None`` or empty."""
    from django.conf import settings

    try:
        value = getattr(settings, key, None)
    except Exception:  # settings not configured: nothing resolved there
        return False
    if value is None:
        return False
    if isinstance(value, (str, bytes)) and not value.strip():
        return False
    return True


def _required_entries():
    """Required manifest rows ∪ call-site declarations, keyed by (owner, key).

    Every row counts: two owners may each declare a required knob of one
    name, and each resolves in its own ``STAPEL_<MOD>`` namespace. The
    CONFIG.MD manifest wins on a key present in both — it is the
    authoritative, human-reviewed source; a call-site declaration only fills
    in keys the table does not know about yet.
    """
    from . import declared_config_entries, load_manifest_rows

    rows = load_manifest_rows()
    in_table = {key for _owner, key in rows}
    merged = {
        (None, key): entry
        for key, entry in declared_config_entries().items()
        if key not in in_table
    }
    merged.update(rows)
    return {row: entry for row, entry in merged.items() if entry.required}


@checks.register("stapel_config")
def check_required_config(app_configs=None, **kwargs):
    """E001 — every required config key must resolve at boot time."""
    from . import (
        SOURCE_SETTINGS,
        ConfigNotDeclared,
        ConfigUnavailable,
        discover_manifest_path,
        get_config,
        resolve_settings_value,
        settings_namespace,
    )

    errors = []
    reported_env: set[str] = set()
    entries = sorted(_required_entries().items(), key=lambda item: (item[0][1], item[0][0] or ""))
    for (_owner, key), entry in entries:
        if entry.source == SOURCE_SETTINGS:
            found, _ = resolve_settings_value(key, entry.owner)
            if found or entry.default is not None:
                continue
            namespace = settings_namespace(entry.owner)
            where = f"{namespace}[{key!r}] or settings.{key}" if namespace else f"settings.{key}"
            if entry.line:
                manifest = discover_manifest_path()
                row = f"{manifest or 'CONFIG.MD'}:{entry.line}"
                if entry.owner:
                    row += f" (## {entry.owner})"
            else:
                row = "a call-site declare_config()"
            purpose = f" — needed for: {entry.purpose}" if entry.purpose else ""
            errors.append(
                checks.Error(
                    f"config {key!r} is a required Django setting{purpose}, "
                    f"but it is not defined and its row has no default.",
                    hint=f"Define {where} in settings.py, or add a default to "
                    f"the row declared at {row}.",
                    id=E002_REQUIRED_SETTING_MISSING,
                )
            )
            continue
        # Explicit required=True (and default=, when the declared/manifest
        # entry carries one) so a key known only via declare_config — not
        # yet in the CONFIG.MD table get_config() itself consults — is still
        # resolved and gated, not misread as an unknown key.
        kwargs: dict = {"required": True}
        if entry.default is not None:
            kwargs["default"] = entry.default
        if entry.line:  # a manifest row, not a call-site declaration
            kwargs["owner"] = entry.owner
        try:
            get_config(key, **kwargs)
        except (ConfigUnavailable, ConfigNotDeclared):
            if _defined_in_settings(key) or key in reported_env:
                continue
            # One environment variable, however many owners declare it.
            reported_env.add(key)
            purpose = f" — needed for: {entry.purpose}" if entry.purpose else ""
            errors.append(
                checks.Error(
                    f"config {key!r} is required{purpose}, but is not set and "
                    f"has no default — see CONFIG.MD.",
                    hint=(
                        "Set the environment variable"
                        + (" or vault secret" if entry.source == "vault" else "")
                        + f" before starting the service, define settings.{key} "
                          "in the settings module, or add a safe default to "
                          "its CONFIG.MD row."
                    ),
                    id=E001_REQUIRED_CONFIG_MISSING,
                )
            )
    return errors


__all__ = [
    "E001_REQUIRED_CONFIG_MISSING",
    "E002_REQUIRED_SETTING_MISSING",
    "check_required_config",
]
