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

E002 is the ``settings``-source counterpart: a required row declared as a
Django setting must resolve in the owner's ``STAPEL_<MOD>`` dict, a flat
setting, or its default column. Settings rows never consult env or vault.
"""
from __future__ import annotations

from django.core import checks

E001_REQUIRED_CONFIG_MISSING = "stapel_core.config.E001"
E002_REQUIRED_SETTING_MISSING = "stapel_core.config.E002"


def _required_entries():
    """Manifest rows ∪ call-site declarations marked required.

    The CONFIG.MD manifest wins on a key present in both — it is the
    authoritative, human-reviewed source; a call-site declaration only fills
    in keys the table does not know about yet.
    """
    from . import declared_config_entries, load_manifest

    merged = dict(declared_config_entries())
    merged.update(load_manifest())  # manifest overrides declared on overlap
    return {key: entry for key, entry in merged.items() if entry.required}


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
    for key, entry in sorted(_required_entries().items()):
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
        try:
            get_config(key, **kwargs)
        except (ConfigUnavailable, ConfigNotDeclared):
            purpose = f" — needed for: {entry.purpose}" if entry.purpose else ""
            errors.append(
                checks.Error(
                    f"config {key!r} is required{purpose}, but is not set and "
                    f"has no default — see CONFIG.MD.",
                    hint=(
                        "Set the environment variable"
                        + (" or vault secret" if entry.source == "vault" else "")
                        + " before starting the service, or add a safe "
                          "default to its CONFIG.MD row."
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
