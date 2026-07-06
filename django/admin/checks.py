"""System checks for admin visibility (tag ``stapel_admin``) — AS-3.

Same policy as the ``stapel_access`` checks: malformed config that would
silently mean something else than written is E-level (deploy blocker);
cross-service tolerance and deliberate-but-notable overrides are W-level.
"""
from __future__ import annotations

from typing import Mapping

from django.core import checks

E001_BAD_MODEL_ENTRY = "stapel_core.admin.E001"
E002_BAD_ADMIN_CLASS = "stapel_core.admin.E002"
W001_UNKNOWN_MODEL_LABEL = "stapel_core.admin.W001"
W002_SECRET_DOWNGRADED = "stapel_core.admin.W002"


@checks.register("stapel_admin")
def check_admin_models(app_configs=None, **kwargs):
    """E001/E002/W001 — the ``STAPEL_ADMIN["MODELS"]`` registry must parse."""
    from django.apps import apps
    from django.utils.module_loading import import_string

    from stapel_core.access.declaration import STANDARD, declared_access
    from stapel_core.access.exceptions import AccessConfigError

    from .conf import ADMIN_ONLY_MODEL_KEYS, admin_settings

    findings = []
    for label, entry in (admin_settings.MODELS or {}).items():
        source = f"STAPEL_ADMIN['MODELS'][{label!r}]"
        try:
            model = apps.get_model(label)
        except (LookupError, ValueError):
            model = None
            findings.append(checks.Warning(
                f"{source} matches no installed model (fine if it targets "
                "another service of this deployment; check for a typo "
                "otherwise).",
                id=W001_UNKNOWN_MODEL_LABEL,
            ))
        if entry is None:
            continue  # unregister — always legal
        if not isinstance(entry, Mapping):
            findings.append(checks.Error(
                f"{source} must be a dict or None, got {type(entry).__name__}.",
                hint="None unregisters the model; a dict may carry category/"
                     "view/add/change/delete and/or admin_class.",
                id=E001_BAD_MODEL_ENTRY,
            ))
            continue
        access_entry = {
            key: value for key, value in entry.items()
            if key not in ADMIN_ONLY_MODEL_KEYS
        }
        if access_entry:
            base = declared_access(model) if model is not None else STANDARD
            try:
                base.patched(access_entry, source=source)
            except AccessConfigError as exc:
                findings.append(checks.Error(
                    str(exc),
                    hint="Allowed keys: category, view, add, change, delete, "
                         "admin_class; levels: low/mid/high/superuser/forbidden.",
                    id=E001_BAD_MODEL_ENTRY,
                ))
        admin_class = entry.get("admin_class")
        if isinstance(admin_class, str) and admin_class:
            try:
                import_string(admin_class)
            except ImportError:
                findings.append(checks.Error(
                    f"{source}['admin_class'] {admin_class!r} cannot be imported.",
                    id=E002_BAD_ADMIN_CLASS,
                ))
    return findings


@checks.register("stapel_admin")
def check_secret_downgrades(app_configs=None, **kwargs):
    """W002 — a settings overlay re-categorizing a declared secret (§1.4).

    The override is honored (mechanism vs policy — the host decides), but
    never silently: superuser-only enforcement and pattern-based masking are
    gone for that model.
    """
    from django.apps import apps

    from stapel_core.access.declaration import declared_access, effective_access
    from stapel_core.access.exceptions import AccessConfigError

    findings = []
    for model in apps.get_models():
        if declared_access(model).category != "secret":
            continue
        try:
            effective = effective_access(model)
        except AccessConfigError:
            continue  # malformed entry — reported by the registry checks
        if effective.category != "secret":
            findings.append(checks.Warning(
                f"{model._meta.label} is declared secret but a settings "
                f"overlay re-categorizes it as {effective.category!r} — "
                "superuser-only enforcement and secret-field masking no "
                "longer apply.",
                hint="If this is a deliberate host decision, silence via "
                     "SILENCED_SYSTEM_CHECKS.",
                id=W002_SECRET_DOWNGRADED,
            ))
    return findings


__all__ = [
    "E001_BAD_MODEL_ENTRY",
    "E002_BAD_ADMIN_CLASS",
    "W001_UNKNOWN_MODEL_LABEL",
    "W002_SECRET_DOWNGRADED",
    "check_admin_models",
    "check_secret_downgrades",
]
