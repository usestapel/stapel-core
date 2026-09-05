"""URLconf fixture for ApiErrorPagesMiddleware / error_pages_checks tests.

Mirrors a service that mounts its REST surface under ``search/api/v1/`` (the
canonical ``.../api/...`` shape) alongside a plain, non-API dashboard route —
exactly the split the middleware and its check need to reason about.
"""
from django.http import HttpResponse, HttpResponseNotAllowed
from django.urls import path


def _ok(request, *args, **kwargs):
    return HttpResponse("ok")


def _get_only(request, *args, **kwargs):
    """A plain (non-DRF) view that answers Django's own 405 for non-GET —
    the exact shape ``View.http_method_not_allowed`` produces."""
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    return HttpResponse("ok")


urlpatterns = [
    path("search/api/v1/things/", _get_only),
    path("search/dashboard/", _ok),
]
