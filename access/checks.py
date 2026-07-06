"""System checks for the staff mandate (tag ``stapel_access``).

E-level — malformed policy config is a deploy blocker (an access policy
that silently means something else than written is worse than a blocked
deploy). W-level — degradation/underconfiguration hints, never blocking
(library-standard §3.7).
"""
from __future__ import annotations

from django.core import checks

E001_BAD_ROLES = "stapel_core.access.E001"
E002_BAD_MODELS = "stapel_core.access.E002"
E003_STRICT_UNENFORCEABLE = "stapel_core.access.E003"
E004_BAD_STEP_UP = "stapel_core.access.E004"
W001_BACKEND_NOT_INSTALLED = "stapel_core.access.W001"
W002_UNAUDITED_DAC = "stapel_core.access.W002"
W003_UNKNOWN_MODEL_LABEL = "stapel_core.access.W003"
W004_RUNTIME_ROLES_RESERVED = "stapel_core.access.W004"
W005_STEP_UP_DEGRADED = "stapel_core.access.W005"

MANDATE_BACKEND = "stapel_core.access.backend.MandateBackend"
AUDITED_BACKEND = "stapel_core.access.backend.AuditedModelBackend"
PLAIN_MODEL_BACKEND = "django.contrib.auth.backends.ModelBackend"


def _access_configured() -> bool:
    from django.conf import settings

    return bool(getattr(settings, "STAPEL_ACCESS", None))


@checks.register("stapel_access")
def check_access_config(app_configs=None, **kwargs):
    """E001/E002 — the ROLES / MODELS merge-registries must parse."""
    from django.apps import apps

    from .conf import access_settings
    from .declaration import declared_access
    from .exceptions import AccessConfigError
    from .roles import effective_roles

    findings = []
    try:
        effective_roles()
    except AccessConfigError as exc:
        findings.append(checks.Error(
            f"STAPEL_ACCESS['ROLES'] is invalid: {exc}",
            hint="Entries are {'clearance': 'low|mid|high', 'apps': {app_label: level}} "
                 "or None to disable a builtin role.",
            id=E001_BAD_ROLES,
        ))

    overlay = access_settings.MODELS or {}
    for label, entry in overlay.items():
        try:
            model = apps.get_model(label)
        except (LookupError, ValueError):
            # Legal by design: one deploy config is shared across services,
            # so overrides may target models of apps not installed here.
            # Still worth a typo hint.
            findings.append(checks.Warning(
                f"STAPEL_ACCESS['MODELS'] key {label!r} matches no installed model "
                "(fine if it targets another service of this deployment; "
                "check for a typo otherwise).",
                id=W003_UNKNOWN_MODEL_LABEL,
            ))
            continue
        if entry is None:
            continue
        try:
            declared_access(model).patched(
                entry, source=f"STAPEL_ACCESS['MODELS'][{label!r}]"
            )
        except AccessConfigError as exc:
            findings.append(checks.Error(
                str(exc),
                hint="Allowed keys: category, view, add, change, delete; "
                     "levels: low/mid/high/superuser/forbidden.",
                id=E002_BAD_MODELS,
            ))
    return findings


@checks.register("stapel_access")
def check_access_backends(app_configs=None, **kwargs):
    """E003/W001/W002/W004 — backend chain consistent with the configured policy."""
    from django.conf import settings

    from .conf import access_settings

    findings = []
    backends = list(getattr(settings, "AUTHENTICATION_BACKENDS", [PLAIN_MODEL_BACKEND]))
    mandate_installed = MANDATE_BACKEND in backends
    audited_installed = AUDITED_BACKEND in backends

    if _access_configured() and not mandate_installed:
        findings.append(checks.Warning(
            "STAPEL_ACCESS is configured but MandateBackend is not in "
            "AUTHENTICATION_BACKENDS — role clearances have no effect.",
            hint=f"Add {MANDATE_BACKEND!r} to AUTHENTICATION_BACKENDS.",
            id=W001_BACKEND_NOT_INSTALLED,
        ))

    if access_settings.STRICT and not audited_installed and PLAIN_MODEL_BACKEND in backends:
        # STRICT is enforced by AuditedModelBackend; a plain ModelBackend
        # ORs its grants past the ceiling. Asking for a ceiling and not
        # getting one is a config error, not a degradation.
        findings.append(checks.Error(
            "STAPEL_ACCESS['STRICT'] is True but AUTHENTICATION_BACKENDS uses the "
            "plain django ModelBackend — DAC grants above the mandate are NOT denied.",
            hint=f"Replace {PLAIN_MODEL_BACKEND!r} with {AUDITED_BACKEND!r}.",
            id=E003_STRICT_UNENFORCEABLE,
        ))
    elif mandate_installed and not audited_installed and PLAIN_MODEL_BACKEND in backends:
        findings.append(checks.Warning(
            "MandateBackend is installed alongside the plain django ModelBackend — "
            "DAC grants above the mandate will work but will not be audited (A4).",
            hint=f"Replace {PLAIN_MODEL_BACKEND!r} with {AUDITED_BACKEND!r}.",
            id=W002_UNAUDITED_DAC,
        ))

    if access_settings.RUNTIME_ROLE_DEFINITIONS:
        findings.append(checks.Warning(
            "STAPEL_ACCESS['RUNTIME_ROLE_DEFINITIONS'] is reserved and not "
            "implemented in this version; role definitions come from settings only "
            "(see the mini-design in stapel_core.access.roles).",
            id=W004_RUNTIME_ROLES_RESERVED,
        ))
    return findings


@checks.register("stapel_access")
def check_step_up(app_configs=None, **kwargs):
    """E004 — STEP_UP must parse; W005 — enforced but degraded (no factor)."""
    from .exceptions import AccessConfigError
    from .stepup import step_up_capable, step_up_config, step_up_enforced

    findings = []
    try:
        step_up_config()
    except AccessConfigError as exc:
        findings.append(checks.Error(
            str(exc),
            hint="STEP_UP keys: ENFORCE (bool), LEVELS (['high']), "
                 "SCOPE (str), MAX_AGE (positive int).",
            id=E004_BAD_STEP_UP,
        ))
        return findings  # a malformed config can't answer the W-check below

    if step_up_enforced() and not step_up_capable():
        findings.append(checks.Warning(
            "STAPEL_ACCESS['STEP_UP'] is enforced but no verification factor is "
            "registered — step-up self-disables (a grant would be unobtainable), "
            "so HIGH operations fall back to the mandate alone.",
            hint="Install stapel-auth or register a factor (register_factor) to "
                 "activate step-up; set STEP_UP={'ENFORCE': False} to silence this.",
            id=W005_STEP_UP_DEGRADED,
        ))
    return findings


__all__ = [
    "E001_BAD_ROLES",
    "E002_BAD_MODELS",
    "E003_STRICT_UNENFORCEABLE",
    "E004_BAD_STEP_UP",
    "W001_BACKEND_NOT_INSTALLED",
    "W002_UNAUDITED_DAC",
    "W003_UNKNOWN_MODEL_LABEL",
    "W004_RUNTIME_ROLES_RESERVED",
    "W005_STEP_UP_DEGRADED",
    "check_access_backends",
    "check_access_config",
    "check_step_up",
]
