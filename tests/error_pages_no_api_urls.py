"""URLconf fixture with no API surface at all — the check must stay quiet
here regardless of whether ApiErrorPagesMiddleware is installed."""
from django.http import HttpResponse
from django.urls import path


def _ok(request, *args, **kwargs):
    return HttpResponse("ok")


urlpatterns = [
    path("dashboard/", _ok),
]
