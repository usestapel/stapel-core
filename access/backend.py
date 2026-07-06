"""Mandate enforcement — runtime auth backends, no Permission-string generation.

``MandateBackend`` computes ``has_perm`` from (model declaration × role
clearance) at call time (design choice (б) of admin-suite §3.4): a decorator
change takes effect on deploy, nothing is materialized per service, drift is
impossible by construction (A1). Nothing here writes to the database.

Intended chain::

    AUTHENTICATION_BACKENDS = [
        "stapel_core.access.backend.MandateBackend",
        "stapel_core.access.backend.AuditedModelBackend",   # DAC overlay
    ]

``AuditedModelBackend`` is a drop-in ``ModelBackend``: manual grants (the
DAC point-fix "this user gets change on one table above their role") keep
working, but a grant used *above* the user's mandate is logged and signalled
(:data:`~stapel_core.access.signals.dac_escalation`) — allowed, not silent
(A4). ``STAPEL_ACCESS["STRICT"] = True`` flips the mandate into a ceiling:
such grants are denied for staff. A plain ``ModelBackend`` in the chain also
works (Django ORs backends) — you only lose the escalation audit and STRICT;
the ``stapel_access`` system checks point this out.

Scope notes:

- The mandate governs only the four default model operations
  (view/add/change/delete). Custom permission codenames are DAC territory —
  both backends leave them to plain ``ModelBackend`` semantics.
- Superuser is outside the mandate (A5): ``MandateBackend`` grants an
  active superuser any governed permission, mirroring Django semantics.
- Non-staff users never receive mandate grants — ``staff_roles`` is a staff
  concept by design (admin-suite §4).
- Object-level checks: the mandate is class-level; a class-level grant
  applies to every row (``obj`` is accepted and ignored — per-row mechanics
  are explicitly out of scope, admin-suite §4).
- With no roles resolvable for a user the mandate disengages entirely:
  ``MandateBackend`` grants nothing and ``AuditedModelBackend`` behaves as
  today's ``ModelBackend`` — the feature is opt-in by the first role.
"""
from __future__ import annotations

import logging

from django.apps import apps
from django.contrib.auth.backends import BaseBackend, ModelBackend

from .declaration import ACTIONS, effective_access
from .levels import Level
from .roles import clearance_for
from .signals import dac_escalation
from .sources import user_roles

logger = logging.getLogger("stapel_core.access")

_AUDITED_ATTR = "_stapel_dac_audited"


def resolve_perm(perm: str):
    """``"app_label.action_modelname"`` → ``(app_label, action, model)`` or None.

    None means "not a mandate-governed permission" (custom codename, unknown
    model, malformed string) — the caller must fall back to DAC semantics.
    """
    if not isinstance(perm, str) or "." not in perm:
        return None
    app_label, _, codename = perm.partition(".")
    action, sep, model_name = codename.partition("_")
    if not sep or action not in ACTIONS or not model_name:
        return None
    try:
        model = apps.get_model(app_label, model_name)
    except (LookupError, ValueError):
        return None
    return app_label, action, model


def mandate_decision(user, app_label: str, action: str, model) -> tuple[bool, Level | None, Level]:
    """``(allowed, clearance, required)`` of the pure mandate computation.

    ``clearance`` is None when the user holds no known roles (mandate not
    engaged for that user).
    """
    required = effective_access(model).required(action)
    clearance = clearance_for(user_roles(user), app_label)
    if clearance is None:
        return False, None, required
    return clearance >= required, clearance, required


class MandateBackend(BaseBackend):
    """MAC half of the chain — grants strictly by (declaration × clearance)."""

    def authenticate(self, request, **credentials):  # not an authentication path
        return None

    def has_perm(self, user_obj, perm, obj=None):
        if not getattr(user_obj, "is_active", False):
            return False
        target = resolve_perm(perm)
        if target is None:
            return False
        if getattr(user_obj, "is_superuser", False):  # A5 — outside the mandate
            return True
        if not getattr(user_obj, "is_staff", False):
            return False
        app_label, action, model = target
        allowed, clearance, _required = mandate_decision(user_obj, app_label, action, model)
        return allowed if clearance is not None else False

    def has_module_perms(self, user_obj, app_label):
        if not getattr(user_obj, "is_active", False):
            return False
        if getattr(user_obj, "is_superuser", False):
            return True
        if not getattr(user_obj, "is_staff", False):
            return False
        try:
            app_config = apps.get_app_config(app_label)
        except LookupError:
            return False
        if not user_roles(user_obj):
            return False
        return any(
            mandate_decision(user_obj, app_label, "view", model)[0]
            for model in app_config.get_models()
        )


class AuditedModelBackend(ModelBackend):
    """DAC overlay: ``ModelBackend`` + escalation audit + STRICT ceiling (A4)."""

    def has_perm(self, user_obj, perm, obj=None):
        granted = super().has_perm(user_obj, perm, obj)
        if not granted:
            return False
        if getattr(user_obj, "is_superuser", False) or not getattr(user_obj, "is_staff", False):
            return True  # superuser outside the mandate; non-staff is pure DAC
        target = resolve_perm(perm)
        if target is None:
            return True  # custom codename — not mandate-governed
        app_label, action, model = target
        allowed, clearance, required = mandate_decision(user_obj, app_label, action, model)
        if clearance is None:
            return True  # no roles — mandate not engaged, legacy behavior
        if allowed:
            return True

        # DAC grant above the mandate.
        from .conf import access_settings

        if access_settings.STRICT:
            logger.warning(
                "STRICT mandate: denied DAC grant %s for staff user %s "
                "(clearance=%s, required=%s)",
                perm, user_obj.pk, clearance.name, required.name,
            )
            return False
        self._audit(user_obj, perm, clearance=clearance, required=required)
        return True

    def _audit(self, user_obj, perm, *, clearance: Level, required: Level) -> None:
        # Once per (user instance, perm) — i.e. once per request in practice.
        audited = getattr(user_obj, _AUDITED_ATTR, None)
        if audited is None:
            audited = set()
            try:
                setattr(user_obj, _AUDITED_ATTR, audited)
            except AttributeError:
                pass
        if perm in audited:
            return
        audited.add(perm)
        logger.warning(
            "DAC escalation above mandate: user=%s perm=%s clearance=%s required=%s "
            "(allowed; set STAPEL_ACCESS['STRICT']=True to deny)",
            user_obj.pk, perm, clearance.name, required.name,
        )
        dac_escalation.send(
            sender=self.__class__,
            user=user_obj,
            perm=perm,
            clearance=clearance,
            required=required,
        )


__all__ = ["AuditedModelBackend", "MandateBackend", "mandate_decision", "resolve_perm"]
