"""``/nav`` — machine-readable navigation for a future frontend (BACKLOG §37).

Mirrors the admin-index module block (:mod:`stapel_core.django.nav`,
rendered into ``admin/base_site.html``) as JSON: which Stapel modules this
process hosts, plus their admin/Swagger/schema links. Staff-gated like the
rest of the admin surface — this is an internal navigation aggregate, not a
public API.
"""
from __future__ import annotations

from django.http import JsonResponse
from django.urls import path

from .nav import build_modules, build_services, nav_sections


def nav_view(request):
    """``GET /nav`` — modules of this process + sibling services + extra links.

    401/403 (not a bare 404) for a non-staff caller — same admissibility the
    admin-index block itself applies, so the JSON never leaks a link map a
    browsing anonymous user could not already reach through the admin.
    """
    user = getattr(request, "user", None)
    if not (user and getattr(user, "is_authenticated", False) and getattr(user, "is_staff", False)):
        return JsonResponse({"detail": "staff access required"}, status=403)

    from .nav import NavConfigError

    try:
        services = build_services()
        sections = nav_sections(user)
    except NavConfigError as exc:
        # Fail soft on the service/nav-links registries (already E-flagged by
        # the stapel_nav system check) — the module list is independent
        # (pure INSTALLED_APPS introspection) and still worth returning.
        return JsonResponse(
            {"modules": build_modules(), "services": [], "sections": {}, "error": str(exc)}
        )

    return JsonResponse(
        {
            "modules": build_modules(),
            "services": services,
            "sections": sections,
        }
    )


def get_nav_urls(prefix: str = ""):
    """URL patterns for the ``/nav`` aggregate.

    Usage in a project's urls.py::

        from stapel_core.django.nav_views import get_nav_urls

        urlpatterns = [
            ...,
            *get_nav_urls(),
        ]
    """
    return [path(f"{prefix}nav/", nav_view, name="stapel-nav")]


__all__ = ["nav_view", "get_nav_urls"]
