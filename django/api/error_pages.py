"""JSON envelopes for a 404/405 that never reached a DRF view.

The defect (found on a fleet stand): a request to an unknown path under an
API prefix — ``/search/api/v1/facets``, say — answers Django's HTML "Not
Found" page, because the resolver never matched any URL pattern and Django's
own ``page_not_found`` view rendered the stock template. The same happens for
a 405 on a plain (non-DRF) view: ``View.http_method_not_allowed`` returns an
``HttpResponseNotAllowed`` with an empty ``text/html`` body. Either way, an
API client or a probe gets HTML where it parses JSON.

A DRF view never has this problem: ``stapel_exception_handler``
(:mod:`stapel_core.django.api.errors`) already answers unmatched-inside-a-view
404s and DRF's own ``MethodNotAllowed`` with the fleet's JSON envelope. The
gap is everything DRF's exception machinery never sees — an unmatched URL (no
view was ever entered) and a plain Django view's 405.

:class:`ApiErrorPagesMiddleware` closes it from the outside: it looks at the
*final* response, and only rewrites a 404/405 that (a) falls under a
configured API prefix and (b) is not already JSON — so a DRF-rendered error
is left untouched and a non-API 404 keeps Django's normal HTML page.
"""
from __future__ import annotations

from typing import Any, Dict

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils.deprecation import MiddlewareMixin

from stapel_core.conf import AppSettings

#: STAPEL_CORE — the settings namespace for cross-cutting Django-integration
#: behavior that does not belong to any one submodule (see conf.py for the
#: AppSettings pattern every other Stapel package uses).
core_settings = AppSettings(
    "STAPEL_CORE",
    defaults={
        # A request path is treated as API surface when one of these markers
        # appears anywhere in it. The default matches how every fleet service
        # mounts its REST surface — .../api/... (the same "api" segment
        # django.checks._CANONICAL_MODULE_SEGMENTS already treats as the
        # module's backend canon) — so a service needs no settings edit to
        # get JSON 404/405 answers on its own API routes. A deployment with
        # an unusual mount (no "/api/" anywhere, or one that wants a
        # narrower/wider match) overrides the list; matching is plain
        # substring containment against the full request path, not a regex.
        "API_PREFIXES": ["/api/"],
    },
    # A stray same-named env var must not silently narrow or widen which
    # paths get JSON error pages — this is a routing decision, not a knob a
    # deployment environment should flip by accident.
    no_env=("API_PREFIXES",),
)

#: Dotted path this module's middleware is installed under — the string
#: every settings preset and system check compares against.
MIDDLEWARE_PATH = "stapel_core.django.api.error_pages.ApiErrorPagesMiddleware"

#: Status codes this middleware ever rewrites. Kept narrow and explicit:
#: nothing else in the fleet's error shape needs an outside-the-view rescue —
#: every other status is either raised from inside a DRF view (already
#: handled by stapel_exception_handler) or is not a "wrong path/verb" case.
HANDLED_STATUSES = (404, 405)


def is_api_path(path: str) -> bool:
    """True when *path* falls under a configured ``API_PREFIXES`` marker."""
    if not path.startswith("/"):
        path = f"/{path}"
    return any(marker in path for marker in core_settings.API_PREFIXES)


def _is_json_response(response: HttpResponse) -> bool:
    content_type = (response.get("Content-Type", "") or "").lower()
    return "json" in content_type


def _error_envelope(status_code: int) -> Dict[str, Any]:
    """The fleet's ``StapelError`` body for *status_code*, as a plain dict.

    Reuses the same builders DRF views call
    (:func:`stapel_core.django.api.errors.error_404_not_found` /
    ``error_405_method_not_allowed``) so the envelope — keys, error codes,
    i18n lookup — never drifts from the DRF path. Their return value is a DRF
    ``Response`` whose ``.data`` is already the plain serialized dict (a
    serializer's ``.data`` needs no renderer/accepted-media-type context, only
    ``.render()`` does), so no DRF content negotiation is needed here.
    """
    from stapel_core.django.api.errors import (
        error_404_not_found,
        error_405_method_not_allowed,
    )

    builder = {
        404: error_404_not_found,
        405: error_405_method_not_allowed,
    }[status_code]
    return builder().data


class ApiErrorPagesMiddleware(MiddlewareMixin):
    """Rewrites an HTML 404/405 under an API prefix as the fleet's JSON error.

    Placed early in ``MIDDLEWARE`` (see
    ``stapel_core.django.settings.COMMON_MIDDLEWARE``) so its
    ``process_response`` — which Django calls in reverse ``MIDDLEWARE``
    order — runs LAST, after every other middleware has already finished
    shaping the response. That is what lets it see the true final
    status/content-type instead of guessing from partway through the chain.

    Left alone, on purpose:

    * any response whose ``Content-Type`` already says JSON — a DRF view's
      own error response, or a project's hand-rolled JSON 404;
    * any 404/405 outside a configured API prefix — the Django default stays
      the Django default there;
    * any status other than 404/405.
    """

    def process_response(
        self, request: HttpRequest, response: HttpResponse
    ) -> HttpResponse:
        if response.status_code not in HANDLED_STATUSES:
            return response
        if _is_json_response(response):
            return response
        if not is_api_path(request.path):
            return response

        json_response = JsonResponse(
            _error_envelope(response.status_code), status=response.status_code
        )
        # A plain Django 405 (HttpResponseNotAllowed) carries the permitted
        # verbs in its Allow header — worth keeping, since it is the one bit
        # of information the JSON body itself does not restate.
        allow = response.get("Allow")
        if allow:
            json_response["Allow"] = allow
        return json_response


__all__ = [
    "MIDDLEWARE_PATH",
    "HANDLED_STATUSES",
    "ApiErrorPagesMiddleware",
    "core_settings",
    "is_api_path",
]
