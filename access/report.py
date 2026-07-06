"""``access_report`` data — the review surface of the mandate (admin-suite §3.8).

"What can an editor actually touch" is answered by one command instead of
archaeology across permission tables: role × model × operation matrix, every
DAC grant above the mandate (A4 — escalation is visible, never silent), and
the list of models running on the implicit standard declaration.
"""
from __future__ import annotations

from typing import Any

from .declaration import ACTIONS, effective_access, is_declared
from .roles import clearance_for, effective_roles


def _model_entry(model, roles) -> dict[str, Any]:
    declaration = effective_access(model)
    requirements = {action: declaration.required(action) for action in ACTIONS}
    app_label = model._meta.app_label
    matrix = {}
    for name, role in roles.items():
        clearance = role.clearance_for(app_label)
        matrix[name] = "".join(
            action[0] if clearance >= required else "-"
            for action, required in requirements.items()
        )
    return {
        "label": model._meta.label,
        "category": declaration.category,
        "declared": is_declared(model),
        "requirements": {a: r.name.lower() for a, r in requirements.items()},
        "roles": matrix,
    }


def _staff_dac_grants(roles) -> list[dict[str, Any]]:
    """Manual grants of staff users that exceed (or lack) a mandate."""
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Permission

    from .backend import resolve_perm
    from .sources import user_roles

    User = get_user_model()
    escalations = []
    staff = User.objects.filter(is_staff=True, is_superuser=False, is_active=True)
    for user in staff:
        perms = Permission.objects.filter(
            pk__in=user.user_permissions.values_list("pk", flat=True)
        ) | Permission.objects.filter(group__in=user.groups.all())
        names = user_roles(user)
        for permission in perms.select_related("content_type").distinct():
            full = f"{permission.content_type.app_label}.{permission.codename}"
            target = resolve_perm(full)
            if target is None:
                continue  # custom codename — pure DAC, not mandate-governed
            app_label, action, model = target
            required = effective_access(model).required(action)
            clearance = clearance_for(names, app_label)
            if clearance is not None and clearance >= required:
                continue  # within the mandate — redundant grant, not an escalation
            escalations.append({
                "user": str(user),
                "user_id": str(user.pk),
                "perm": full,
                "required": required.name.lower(),
                "clearance": clearance.name.lower() if clearance is not None else None,
                "roles": sorted(names),
            })
    return escalations


def _step_up_section() -> dict[str, Any]:
    """Step-up posture (AS-6, §3.8): which operations are gated + grant uptake.

    Aggregates only — model labels, gated actions, and *counts* of staff with
    a fresh grant; never the grant tokens themselves (no secret leak).
    """
    from django.apps import apps
    from django.contrib.auth import get_user_model

    from .stepup import (
        action_requires_step_up,
        has_fresh_step_up,
        step_up_active,
        step_up_capable,
        step_up_config,
        step_up_enforced,
    )

    cfg = step_up_config()
    gated = []
    for model in apps.get_models():
        actions = [a for a in ACTIONS if action_requires_step_up(model, a)]
        if actions:
            gated.append({"label": model._meta.label, "actions": actions})
    gated.sort(key=lambda entry: entry["label"])

    User = get_user_model()
    staff = list(
        User.objects.filter(is_staff=True, is_active=True)
    )
    with_grant = sum(1 for user in staff if has_fresh_step_up(user))
    return {
        "enforce": step_up_enforced(),
        "capable": step_up_capable(),
        "active": step_up_active(),
        "scope": cfg["SCOPE"],
        "max_age": cfg["MAX_AGE"],
        "levels": sorted(cfg["LEVELS"]),
        "gated_models": gated,
        "fresh_grants": {"staff_total": len(staff), "with_fresh_grant": with_grant},
    }


def build_report() -> dict[str, Any]:
    from django.apps import apps

    from .conf import access_settings

    roles = effective_roles()
    models = sorted(
        (_model_entry(model, roles) for model in apps.get_models()),
        key=lambda entry: entry["label"],
    )
    return {
        "strict": bool(access_settings.STRICT),
        "roles": {
            name: {
                "clearance": role.clearance.name.lower(),
                "apps": {app: level.name.lower() for app, level in sorted(role.apps.items())},
            }
            for name, role in sorted(roles.items())
        },
        "models": models,
        "dac_escalations": _staff_dac_grants(roles),
        "undeclared": [entry["label"] for entry in models if not entry["declared"]],
        "step_up": _step_up_section(),
    }


def render_text(report: dict[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append

    add("STAPEL ACCESS REPORT")
    add(f"strict mode: {'ON (mandate is a ceiling)' if report['strict'] else 'off (DAC escalation allowed, audited)'}")
    add("")

    add("== Roles ==")
    for name, role in report["roles"].items():
        scopes = ", ".join(f"{app}={level}" for app, level in role["apps"].items())
        add(f"  {name:<16} clearance={role['clearance']}" + (f"  [{scopes}]" if scopes else ""))
    add("")

    role_names = list(report["roles"])
    add("== Model × role matrix (letters = allowed: v/a/c/d) ==")
    header = f"  {'model':<44} {'category':<9} {'v/a/c/d requirement':<34}" + " ".join(
        f"{n:<10}" for n in role_names
    )
    add(header)
    for entry in report["models"]:
        req = "/".join(entry["requirements"][a] for a in ACTIONS)
        cells = " ".join(f"{entry['roles'][n]:<10}" for n in role_names)
        add(f"  {entry['label']:<44} {entry['category']:<9} {req:<34}" + cells)
    add("")

    add("== DAC grants above mandate (A4) ==")
    if not report["dac_escalations"]:
        add("  none")
    for row in report["dac_escalations"]:
        clearance = row["clearance"] or "no mandate roles"
        add(
            f"  {row['user']} ({row['user_id']}): {row['perm']} "
            f"requires {row['required']}, clearance {clearance}, roles {row['roles']}"
        )
    add("")

    add("== Models without an @access declaration (implicit standard) ==")
    if not report["undeclared"]:
        add("  none")
    for label in report["undeclared"]:
        add(f"  {label}")
    add("")

    step = report.get("step_up")
    if step is not None:
        if step["active"]:
            state = "ACTIVE"
        elif step["enforce"]:
            state = "enforced but DEGRADED (no verification factor registered)"
        else:
            state = "off (ENFORCE=False)"
        add("== Step-up on HIGH operations (AS-6) ==")
        add(
            f"  {state}; scope={step['scope']}, max_age={step['max_age']}s, "
            f"levels={'/'.join(step['levels'])}"
        )
        grants = step["fresh_grants"]
        add(
            f"  fresh grants: {grants['with_fresh_grant']}/{grants['staff_total']} "
            "active staff hold one"
        )
        if not step["gated_models"]:
            add("  gated models: none")
        for entry in step["gated_models"]:
            add(f"  gated: {entry['label']} [{', '.join(entry['actions'])}]")
        add("")
    return "\n".join(lines)


__all__ = ["build_report", "render_text"]
