"""URLconf fixture for the module-API-address check
(``stapel_core.django.checks.check_module_api_address``, E005/E006).

Every mount below is a transcription of a real one from app.ironmemo.com at
the moment the workspace list was found to have never worked. The point of
copying them verbatim is that the three are *not* variations of one mistake
— they are three different relationships between the host's mount literal
and the path the library contributes to itself, and only one of the three is
correct:

===================  ==========================  ===================  ======
host mount literal   library's own urls.py adds  resolved address     verdict
===================  ==========================  ===================  ======
``agent/``           ``api/v1/``                 ``/agent/api/v1/``   ok
``workspaces/api/``  ``v1/``                     ``/workspaces/api/   ok
                                                 v1/``
``workspaces/api/    ``v1/``                     ``/workspaces/api/   E005
workspaces/``                                    workspaces/v1/``
``''`` (site root)   ``api/v1/``                 ``/api/v1/``         E005
===================  ==========================  ===================  ======

Note rows 1 and 2: the two *correct* mounts have different literals, because
``stapel_agent`` contributes ``api/v1/`` from inside its own urls.py while
``stapel_workspaces`` contributes only ``v1/``. A check comparing host
literals against each other, or against a settings constant, must call one
of those two wrong. Only the resolved address distinguishes them, which is
why this check runs in-process against the live resolver.

Views get their ``__module__`` overridden to pretend they live inside a fake
Stapel module's package — the same dotted-path signal
``callback_owner_app_label`` reads in a real deployment, without needing the
actual distributions installed here (mirrors ``mounts_surface_urls.py``).
"""
from django.http import HttpResponse
from django.urls import include, path


def _owned_by(module_name):
    """A fresh view callable "owned" by *module_name* (``__module__`` override)."""

    def _v(request, *args, **kwargs):
        return HttpResponse("ok")

    _v.__module__ = module_name
    return _v


agent_view = _owned_by("stapel_agent.views")
workspaces_view = _owned_by("stapel_workspaces.views")
workspaces_error_keys = _owned_by("stapel_workspaces.errors")
translate_view = _owned_by("stapel_translate.views")
gdpr_view = _owned_by("stapel_gdpr.views")
billing_view = _owned_by("stapel_billing.views")
host_view = _owned_by("myproject.views")


# --- correct: library contributes "api/v1/", host mounts the bare prefix ---
CORRECT_AGENT = [
    path("agent/", include([
        path("api/v1/llm/complete", agent_view, name="llm-complete"),
    ])),
]

# --- correct: library contributes "v1/", host mounts through "api/" --------
CORRECT_WORKSPACES = [
    path("workspaces/api/", include([
        path("v1/roles", workspaces_view, name="ws-roles"),
    ])),
    # The host's own error-keys route under the module's api/ prefix: owned by
    # the module (the view class ships in stapel_workspaces.errors) but
    # carrying no version segment. Not an addressing subject — E004 territory.
    path("workspaces/api/error-keys/", workspaces_error_keys, name="ws-errors"),
]

# --- E005: one segment too many between api/ and v1/ (the live incident) ---
BROKEN_WORKSPACES = [
    path("workspaces/api/workspaces/", include([
        path("v1/roles", workspaces_view, name="ws-roles"),
    ])),
]

# --- green: canon served, legacy address ALSO served as a deprecation shim -
# How ironmemo actually had to bridge this incident. stapel-core's own
# workspaces client probes only the doubled-segment path, and sibling
# services call the pod directly, so nginx could not rewrite it for them —
# the compatibility had to live in the URLconf until every peer's client
# knows the canon. Every caller built against the canon is served, so this
# is not the defect and must not be reported as one.
CANON_PLUS_LEGACY_SHIM = [
    path("workspaces/api/", include([
        path("v1/roles", workspaces_view, name="ws-roles"),
    ])),
    path("workspaces/api/workspaces/", include([
        path("v1/roles", workspaces_view, name="ws-roles-legacy"),
    ])),
]

# --- E005: a second wrong address, for the "list them all" case ------------
BROKEN_MISSING_API_WORKSPACES = [
    path("workspaces/", include([
        path("v1/roles", workspaces_view, name="ws-roles-noapi"),
    ])),
]

# --- E005: mounted at the site root, module prefix missing entirely --------
BROKEN_TRANSLATE_AT_ROOT = [
    path("", include([
        path("api/v1/langs/", translate_view, name="tr-langs"),
    ])),
]

# --- E005: the api/ segment dropped, version straight after the module -----
BROKEN_MISSING_API = [
    path("agent/", include([
        path("v1/llm/complete", agent_view, name="llm-complete"),
    ])),
]

# --- a module deliberately co-mounted inside a sibling's prefix ------------
# stapel_gdpr served by the auth service under auth/, as iron-auth does.
# E005 unless the deployment declares STAPEL_MOUNTS = {"gdpr": {...}}.
CO_MOUNTED_GDPR = [
    path("auth/api/", include([
        path("v1/export", gdpr_view, name="gdpr-export"),
    ])),
]

# --- host's own routes, never a subject -----------------------------------
# The host route is deliberately shaped like a canonical module API at a
# non-module prefix: if ownership were decided by path shape rather than by
# the view's owning package, this would be read as billing mounted wrong.
HOST_ONLY = [
    path("whatever/api/v1/thing", host_view, name="host-thing"),
]

HOST_ALONGSIDE_CORRECT_MODULE = HOST_ONLY + [
    path("billing/api/", include([
        path("v1/wallet", billing_view, name="billing-wallet"),
    ])),
]

# --- several modules at once, only one of them wrong ----------------------
MIXED = CORRECT_AGENT + BROKEN_WORKSPACES + [
    path("billing/api/", include([
        path("v1/wallet", billing_view, name="billing-wallet"),
    ])),
]

# --- a module mounted, but publishing no versioned route ------------------
# (admin-only surface: real, and not this check's business)
UNVERSIONED_ONLY = [
    path("billing/admin/audit/", billing_view, name="billing-admin-audit"),
]

# Default for tests that just import the module.
urlpatterns = CORRECT_AGENT + CORRECT_WORKSPACES
