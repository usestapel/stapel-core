"""URLconf fixture for the §37 mount-surface-containment check
(``stapel_core.django.checks.check_module_surface_containment``, E004).

Every view below has its ``__module__`` overridden to pretend it lives
inside a fake Stapel module's package — the same dotted-path signal
``_callback_owner_app_label`` reads in real deployments, without needing an
actual installed ``stapel_billing``/``stapel_translate``/``stapel_calendar``
distribution in this test environment. Ownership is then matched against
whatever ``AppConfig``s the test mocks into ``django.apps.apps.get_app_configs``.
"""
from django.http import HttpResponse
from django.urls import include, path


def _view(request, *args, **kwargs):
    return HttpResponse("ok")


def _owned_by(module_name):
    """A fresh view callable "owned" by *module_name* (``__module__`` override)."""

    def _v(request, *args, **kwargs):
        return HttpResponse("ok")

    _v.__module__ = module_name
    return _v


# Compliant — billing's whole API surface sits under .../api/v1/...
billing_wallet = _owned_by("stapel_billing.views")

# Compliant — nested "admin" segment *inside* an api/ mount (auth's admin_api
# gate) is still fine: "api" is present anywhere in the full path.
auth_admin_audit = _owned_by("stapel_auth.views")

# Violation — a hand-rolled dashboard route with no canonical segment
# anywhere in its path (the translate finding this check exists to catch).
translate_dashboard = _owned_by("stapel_translate.views")

# Violation — a bare module root (the /calendar incident: nginx reserving
# this exact shape breaks the SPA page mounted at the same prefix).
calendar_bare_root = _owned_by("stapel_calendar.views")

# Not a Stapel module at all — the host project's own page. Never flagged,
# regardless of shape, because no installed Stapel AppConfig owns it.
host_page = _owned_by("myproject.views")

urlpatterns = [
    path("billing/api/v1/wallet", billing_wallet, name="billing-wallet"),
    path("auth/api/v1/admin/audit/", auth_admin_audit, name="auth-admin-audit"),
    path("translate/dashboard/", translate_dashboard, name="translate-dashboard"),
    path("calendar", calendar_bare_root, name="calendar-bare-root"),
    path("whatever/", host_page, name="host-page"),
    # A nested include — the containment walk must recurse into resolvers,
    # not just top-level patterns.
    path("billing/", include([
        path("api/v1/extra", billing_wallet, name="billing-extra"),
    ])),
]
