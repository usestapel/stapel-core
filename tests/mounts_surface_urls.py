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
from django.urls import include, path, re_path


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

# Compliant module whose router registers itself with a regex anchor.
currencies_rates = _owned_by("stapel_currencies.views")

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
    # Compliant, but only if regex anchors are stripped PER SEGMENT: a module
    # that registers a re_path router (stapel-currencies uses r"api/v1") and
    # is mounted under a host prefix yields `currencies/^api/v1/...`, so the
    # `^` lands mid-path. Stripping anchors once across the whole path left
    # the segment reading "^api" and flagged a canonical mount as E004 —
    # every generated project that included currencies failed its own
    # `manage.py check` (2026-07-26).
    path("currencies/", include([
        re_path(r"^api/v1/rates", currencies_rates, name="currencies-rates"),
    ])),
]
