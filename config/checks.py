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
"""
from __future__ import annotations

from django.core import checks

E001_REQUIRED_CONFIG_MISSING = "stapel_core.config.E001"


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
    from . import ConfigNotDeclared, ConfigUnavailable, get_config

    errors = []
    for key, entry in sorted(_required_entries().items()):
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
            purpose = f" — нужен для: {entry.purpose}" if entry.purpose else ""
            errors.append(
                checks.Error(
                    f"config {key!r} обязателен{purpose}, но не задан и не "
                    f"имеет дефолта — см. CONFIG.MD.",
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
    "check_required_config",
]
