"""Contract checks (tag ``stapel_contract``) — a route the schema cannot see.

A service's ``docs/schema.json`` is built by drf-spectacular, and
drf-spectacular describes DRF views. A plain Django ``View`` mounted under the
API prefix is skipped in silence: the route is live, clients poll it, consumers
generate against a document that does not mention it — and the contract gate
passes, because a gate cannot notice the absence of something it was never told
about. That is the failure mode, not a style preference: the green tick is over
a surface smaller than the one the service serves.

Found on ``stapel_core.django.jwt.views.JWTStatusView`` (0.85.0): mounted by an
installed module at ``…/api/v1/jwt/status/``, polled by the admin
session-timeout widget on every open admin tab, absent from the consumer's
schema for as long as it existed. The view moved to DRF; this check is so that
the NEXT one is named at ``manage.py check`` time instead of the next time
somebody reads a schema closely.

Checks
------
W001  a route under the API prefix whose view is not a DRF view and carries no
      exemption. The route is invisible to drf-spectacular.
W002  ``stapel_contract_exempt`` is present but is not an exemption — a typo
      must not read as a declaration.

Warning, not Error, and deliberately so: the finding is usually about a file
in somebody else's wheel (the module that mounted the route), an Error would
block deploys over it, and a blocked deploy is how a whole tag ends up in
``SILENCED_SYSTEM_CHECKS``. The module name is in the message so the fix can
be asked for where it belongs.

Exempting a route
-----------------
Not every API-prefixed route belongs to DRF. A server-sent-events stream, a
file download, a webhook receiver speaking somebody else's envelope — those are
plain views on purpose, and the answer is to say so::

    class EventStreamView(View):
        # stapel: contract-exempt
        stapel_contract_exempt = "SSE — DRF has no renderer for a live stream"

``True`` is accepted as the bare marker; a string is better, because the reason
is the thing a reader of ``docs/schema.json`` will want six months later. What
is NOT accepted is silence: an undescribed route and a deliberately undescribed
route look identical from outside, and only one of them is a defect.

What this does not catch
------------------------
* **A DRF view that drf-spectacular still cannot describe** (a dynamic
  serializer, an undecorated ``@api_view`` with no annotations). Being a DRF
  view is necessary, not sufficient — ``stapel-tools``' schema lint asks the
  next question, against the emitted document.
* **Routes under a prefix this deployment does not spell** ``api``. The prefix
  is the §37 canon segment; a service that serves its API somewhere else is
  outside the canon this check reads.
* **Whether the emitted operation is CORRECT.** This check asks only whether
  the route can appear at all.
"""
from __future__ import annotations

from typing import Any, Optional

from django.core import checks

W001_API_ROUTE_INVISIBLE = "stapel_core.contract.W001"
W002_BAD_CONTRACT_EXEMPTION = "stapel_core.contract.W002"

#: The attribute a view sets to declare that it is a plain view on purpose.
CONTRACT_EXEMPT_ATTR = "stapel_contract_exempt"

#: The §37 canon segment that makes a route part of the API surface
#: (``stapel_core.django.checks._CANONICAL_MODULE_SEGMENTS`` reads the same
#: token). Presence anywhere in the path, not position: ``auth/api/v1/…`` and
#: ``api/v1/…`` are both the API surface.
API_SEGMENT = "api"

_MISSING = object()

_HINT = (
    "Make it a DRF view (rest_framework.views.APIView, a ViewSet, or a "
    "function with @api_view) so drf-spectacular can describe it and the "
    f"contract gate can see it — or, if it is a plain view on purpose (a "
    f"stream, a download, a foreign webhook envelope), declare that: "
    f"`{CONTRACT_EXEMPT_ATTR} = \"<why>\"`. Both answers make this green; "
    "silence is the finding."
)


def is_drf_view(view: Any) -> bool:
    """Is *view* something drf-spectacular will describe?

    Class-based DRF views are ``APIView`` subclasses; a function decorated with
    ``@api_view`` is introspected through the generated ``WrappedAPIView`` class
    that :func:`stapel_core.django.urlsurvey.view_of` already resolves for us,
    so both shapes answer here without a second code path.
    """
    try:
        from rest_framework.views import APIView
    except Exception:  # pragma: no cover - DRF not installed
        return False
    return isinstance(view, type) and issubclass(view, APIView)


def contract_exempt(reason):
    """Declare a view deliberately absent from the schema, and say why.

    Usable on a class or a function view::

        @contract_exempt("Prometheus text exposition — DRF renders JSON")
        def prometheus_metrics(request):
            ...

    The reason is the payload: an undescribed route and a deliberately
    undescribed one look identical from outside, and only one of them is a
    defect. Setting the attribute by hand is equivalent.
    """
    if not _is_admissible(reason):
        raise ValueError(
            f"{CONTRACT_EXEMPT_ATTR} needs a non-empty reason (or True); "
            f"got {reason!r}"
        )

    def apply(view):
        setattr(view, CONTRACT_EXEMPT_ATTR, reason)
        return view

    return apply


def _is_admissible(declared: Any) -> bool:
    return declared is True or (isinstance(declared, str) and bool(declared.strip()))


def library_package(view: Any) -> Optional[str]:
    """The installed ``stapel_*`` package a view came from, or None for the
    project's own code — the same question, and the same answer, as
    :func:`stapel_core.django.adoption_checks.library_package`."""
    module = getattr(view, "__module__", "") or ""
    top = module.split(".", 1)[0]
    return top if top.startswith("stapel_") else None


@checks.register("stapel_contract")
def check_api_routes_are_drf_views(app_configs=None, **kwargs):
    """W001/W002 — every route under the API prefix must be describable, or
    say why it is not.

    See the module docstring: the defect is a contract gate that is green over
    a route it cannot see, and the remedy is either a DRF view or an explicit
    exemption.
    """
    from stapel_core.django.urlsurvey import iter_surface, path_segments

    findings = []
    seen: set = set()
    for entry in iter_surface():
        if API_SEGMENT not in path_segments(entry.full_path):
            continue
        view = entry.view
        if is_drf_view(view):
            continue

        key = entry.dotted_name
        if key in seen:
            continue
        seen.add(key)

        where = f"{key} (at /{entry.full_path.lstrip('/')})"
        library = library_package(view)
        ships_in = (
            f" The view ships in the installed '{library}' package — the "
            f"declaration belongs in that module's own source."
            if library else ""
        )

        declared = getattr(view, CONTRACT_EXEMPT_ATTR, _MISSING)
        if declared is not _MISSING and declared not in (None, False):
            if _is_admissible(declared):
                continue
            findings.append(checks.Warning(
                f"{where} sets {CONTRACT_EXEMPT_ATTR} = {declared!r}, which is "
                f"not an exemption. Admissible values: True, or a non-empty "
                f"string giving the reason.",
                hint="A misspelled value must not read as a declaration — "
                     "that is the one way this check could be defeated by "
                     "accident." + ships_in,
                id=W002_BAD_CONTRACT_EXEMPTION,
            ))
            continue

        findings.append(checks.Warning(
            f"{where} is on the API surface but is not a DRF view, so "
            f"drf-spectacular emits no path for it: the route is served and "
            f"the schema — and every client generated from it — does not "
            f"know it exists.",
            hint=_HINT + ships_in,
            id=W001_API_ROUTE_INVISIBLE,
        ))
    return findings


__all__ = [
    "API_SEGMENT",
    "CONTRACT_EXEMPT_ATTR",
    "W001_API_ROUTE_INVISIBLE",
    "W002_BAD_CONTRACT_EXEMPTION",
    "check_api_routes_are_drf_views",
    "contract_exempt",
    "is_drf_view",
    "library_package",
]
