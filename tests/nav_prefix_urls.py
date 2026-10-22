"""URLconf for the nav url_name tests: a module page mounted under a host
prefix and an ``api/`` include, the shape a monolith with ``URL_PREFIX`` uses."""
from django.http import HttpResponse
from django.urls import include, path


def _page(request):
    return HttpResponse("ok")


module_patterns = [
    path("mod/admin/dashboard/", _page, name="navtest-mod-dashboard"),
]

urlpatterns = [
    path("shop/api/", include(module_patterns)),
]
