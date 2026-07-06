"""Minimal namespaced URLconf for mount-registry tests.

Emulates a service URLconf with an admin mounted at ``admin/`` — the
``admin`` namespace is what stapel_core.django.mounts reverses, so
django.contrib.admin itself is not needed here.
"""
from django.http import HttpResponse
from django.urls import include, path


def _ok(request, *args, **kwargs):
    return HttpResponse("ok")


admin_patterns = (
    [
        path("login/", _ok, name="login"),
        path("logout/", _ok, name="logout"),
        path("", _ok, name="index"),
    ],
    "admin",
)

urlpatterns = [
    path("admin/", include(admin_patterns)),
]
