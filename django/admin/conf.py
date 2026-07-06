"""Settings namespace for admin visibility (``STAPEL_ADMIN``) — admin-suite AS-3.

Merge-registry semantics mirror ``STAPEL_ACCESS`` (canonical seam,
library-standard §3.3): a dict entry patches, ``None`` removes, an unknown
key is a config error. ``STAPEL_ADMIN["MODELS"]`` and
``STAPEL_ACCESS["MODELS"]`` live in one resolution (admin-suite §3.7): the
category/level keys of *both* feed :func:`stapel_core.access.effective_access`
(so ``{"category": "business"}`` on an ops table really makes it visible to
staff, not just cosmetically); the admin-only keys (``admin_class``, ``None``)
are consumed at registration time by the autodiscover hook.
"""
from stapel_core.conf import AppSettings

#: Keys of a ``STAPEL_ADMIN["MODELS"]`` entry consumed by the *admin*
#: registration layer (the remaining keys — category/view/add/change/delete —
#: are access-declaration overrides consumed by ``effective_access``).
ADMIN_ONLY_MODEL_KEYS = frozenset({"admin_class"})

admin_settings = AppSettings(
    "STAPEL_ADMIN",
    defaults={
        # Merge-registry over @access + admin registration, keyed by
        # "app_label.ModelName":
        #   {"stapel_outbox.OutboxEvent": {"category": "business"},   # show to staff
        #    "stapel_billing.StripeWebhookEvent": None,               # unregister
        #    "stapel_listings.Listing": {"admin_class": "app.admin.MyAdmin"}}
        "MODELS": {},
        # Dev-mode toggle: ops models become viewable by any staff user
        # (read-only is still enforced by StapelModelAdmin). Env-readable
        # (a deploy/dev convenience, unlike the trust-shaping ACCESS keys).
        "SHOW_OPS_MODELS": False,
        # Merge-registry (admin-suite AS-4 §2.3) over the code-registered nav
        # links (register_nav_link), keyed by an opaque link id:
        #   {"monitoring.grafana": {"section": "monitoring", "title": "Grafana",
        #                            "url": "/monitoring/grafana/", "external": True},
        #    "translate.dashboard": None}   # disable a built-in link
        # A partial dict patches a code-registered link; a full dict adds a
        # new one; None removes. Consumed by stapel_core.django.nav.
        # "service_dashboard": True marks a link as the explicit AS-4 §2
        # current-service-dashboard arbitration flag (current_dashboard_url);
        # a link without it falls back to the legacy prefix-matching
        # heuristic. At most one flagged link is expected per deployment
        # (stapel_core.nav.W003 warns on a duplicate).
        "NAV_LINKS": {},
    },
    # MODELS and NAV_LINKS shape visibility/trust decisions — never from a
    # stray env var. SHOW_OPS_MODELS is a dev toggle and intentionally *does*
    # read the env.
    no_env=("MODELS", "NAV_LINKS"),
)


def show_ops_models() -> bool:
    """``SHOW_OPS_MODELS`` coerced to bool (env values arrive as strings)."""
    value = admin_settings.SHOW_OPS_MODELS
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


__all__ = ["ADMIN_ONLY_MODEL_KEYS", "admin_settings", "show_ops_models"]
