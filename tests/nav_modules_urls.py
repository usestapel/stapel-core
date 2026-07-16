"""Minimal URLconf for module-discovery tests (stapel_core.django.nav).

Gives ``discover_modules()`` something real to ``reverse()`` against:
- ``admin:app_list`` (the stock Django per-app admin index), so
  ``_module_admin_url`` exercises its primary path instead of the
  mounts-registry fallback;
- a ``billing`` namespace with its own ``swagger-ui``/``schema`` — the
  future §37 per-module canon, preferred when present;
- a deployment-wide, unnamespaced ``swagger-ui``/``schema`` — today's
  typical monolith, the fallback a module with no dedicated mount gets.
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
        path("<str:app_label>/", _ok, name="app_list"),
    ],
    "admin",
)

billing_patterns = (
    [
        path("swagger/", _ok, name="swagger-ui"),
        path("schema/", _ok, name="schema"),
    ],
    "billing",
)

urlpatterns = [
    path("admin/", include(admin_patterns)),
    path("billing/", include(billing_patterns)),
    # Deployment-wide Swagger/schema (today's monolith reality) — unnamespaced.
    path("swagger/", _ok, name="swagger-ui"),
    path("schema/", _ok, name="schema"),
]
