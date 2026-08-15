"""The third principal state at the tenancy seam.

:mod:`stapel_core.django.mandate` gives the fleet the missing word — GUEST,
an authenticated account holding no mandate anywhere — and
``HasWorkspaceMandate`` enforces it on a view. This module carries the same
word one layer down, to the place three modules independently invented and
then defaulted open: the ``SCOPE_PROVIDER`` seam.

Why the seam and not the views
------------------------------
stapel-chat, stapel-tasks and stapel-video each ship a ``ScopeProvider``
contract and a ``DefaultScopeProvider`` documented "for single-tenant hosts
and tests — swap for a workspace-aware provider in production". Each default
answers every tenancy question with yes: ``can()`` returns True
unconditionally, ``is_member()`` returns ``user.is_authenticated``,
``filter()`` is identity. Their system checks validate that the configured
path *imports* and *is the right type* — nothing warned that a multi-tenant
deployment was running the fail-open default, so the shipped answer was also
the deployed one.

The defect is the default, not the eleven views that trusted it. Two halves
fix it once:

* :class:`MandateScopeMixin` — the shipped defaults stop answering yes to a
  caller who is provably mandate-less, *whenever this deployment can ask*.
  A genuinely standalone deployment (nothing wired, no workspaces installed)
  keeps single-tenant semantics, because there are no mandates to hold there
  and refusing everyone would be a different bug.
* :func:`check_shipped_scope_provider` — one check body the modules register,
  which **errors** when workspaces is reachable and the provider is still the
  shipped one, and warns when the deployment is standalone.

The split between the two matters. "This user holds no mandate" is a verdict
and answers False. "I could not ask" is not a verdict at all: it raises
``MandateUnavailable`` (503) straight through the provider, so a blip in the
workspaces seam degrades to refusal instead of admitting everyone quietly —
the failure mode this whole axis exists to prevent.

What the mixin does NOT do
--------------------------
It closes the guest state; it does not isolate tenants. A mandated member of
workspace A still passes a shipped single-scope provider inside workspace B,
because a provider that cannot name the request's active workspace cannot
filter by one. That is exactly what :func:`check_shipped_scope_provider`
raises at E-level, with the hint to write the workspace-aware provider. The
mixin is the floor, not the ceiling.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

def deployment_is_standalone() -> bool:
    """True when nothing here can answer "does this user hold a mandate".

    Neither the ``workspaces.check_mandate`` seam nor an in-process
    ``stapel_workspaces``. Such a deployment has no mandates to hold, so the
    guest state does not exist in it and the shipped single-scope providers
    keep their documented single-tenant behaviour.

    Re-read per call rather than cached: it is settings-and-registry only
    (never a probe), and a cached answer would outlive the settings override
    that switched the deployment shape.
    """
    from stapel_core.django.mandate import mandate_seam_unreachable_reason

    return mandate_seam_unreachable_reason() is not None


class MandateScopeMixin:
    """Gives a ``ScopeProvider`` the third principal state.

    Mixed in FRONT of a module's provider (``class DefaultScopeProvider(
    MandateScopeMixin, ScopeProvider)``) so the module keeps ownership of what
    a scope means; this only supplies the predicate the defaults were missing.

    Usage inside a provider::

        def can(self, request, action, board=None):
            return self.mandate_admits(request)

        def filter(self, queryset, request):
            return queryset if self.mandate_admits(request) else queryset.none()
    """

    def mandate_admits(self, request) -> bool:
        """False iff *request*'s caller is provably mandate-less.

        Returns True for a mandated caller, and for every caller in a
        standalone deployment (:func:`deployment_is_standalone`).

        Raises:
            MandateUnavailable: the seam is wired but rendered no answer.
                A DRF ``APIException`` with status 503 on purpose — it travels
                out of the provider, through the view that never thought to
                catch it, and reaches the client as "cannot verify right now"
                rather than as a verdict about the user. Never 403.
        """
        from stapel_core.django.api.permissions import MandateUnavailable
        from stapel_core.django.mandate import (
            MandateLookupUnavailable,
            MandateState,
            mandate_state,
        )

        if deployment_is_standalone():
            return True
        try:
            state = mandate_state(getattr(request, "user", None))
        except MandateLookupUnavailable as exc:
            raise MandateUnavailable() from exc
        return state is MandateState.MANDATED


def check_shipped_scope_provider(
    *,
    setting: str,
    provider,
    shipped_cls,
    error_id: str,
    warning_id: str,
    isolates: str,
):
    """One check body for "the deployment is still running the shipped provider".

    Args:
        setting: the settings key, for the message (``STAPEL_TASKS['SCOPE_PROVIDER']``).
        provider: the resolved value of that key (class or instance).
        shipped_cls: the module's own single-scope default class.
        error_id / warning_id: the module's check ids for the two levels.
        isolates: what a real provider would separate, in the module's own
            words ("boards and cards", "conversations", "rooms").

    A host that swapped the provider is silent. A host still on the shipped one
    gets an ERROR when this deployment can ask about mandates (workspaces is
    installed or the seam is routed — i.e. it is multi-tenant and the shipped
    provider cannot name a tenant), and a WARNING otherwise, because
    single-tenant is a legitimate shape the default is honestly documented for.
    """
    from django.core import checks

    target = provider if isinstance(provider, type) else type(provider)
    if not (isinstance(target, type) and issubclass(target, shipped_cls)):
        return []

    if deployment_is_standalone():
        return [checks.Warning(
            f"{setting} is the shipped single-scope provider: every "
            f"{isolates} lives in one global scope. Correct for a "
            f"single-tenant host, and this deployment looks like one — "
            f"nothing here can answer 'does this user hold a mandate'.",
            hint=f"If this host is multi-tenant, point {setting} at a "
                 f"workspace-aware provider and install/route stapel_workspaces.",
            id=warning_id,
        )]

    return [checks.Error(
        f"{setting} is the shipped single-scope provider, but this deployment "
        f"has workspaces: it can answer 'does this user hold a mandate'. The "
        f"shipped provider closes the guest state and nothing else — it cannot "
        f"name the request's active workspace, so every mandated member of any "
        f"workspace reaches every {isolates} of every other one.",
        hint=f"Point {setting} at a provider that resolves and filters by the "
             f"active workspace (subclass the module's ScopeProvider; ask "
             f"stapel_core.django.workspaces.require_capability). Keeping the "
             f"shipped default here is a tenancy hole, not a configuration "
             f"style.",
        id=error_id,
    )]


__all__ = [
    "MandateScopeMixin",
    "check_shipped_scope_provider",
    "deployment_is_standalone",
]
