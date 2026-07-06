"""Registration-time admin hooks (admin-suite AS-3, §1.4 + Q9).

Two concerns that cannot live on a ModelAdmin class because they mutate the
*registry*, not a registered admin:

- **django.contrib re-registration (Q9)** — ``auth.Group`` is re-registered
  under a declaration-aware subclass and ``sessions.Session`` gets a masked
  read-only admin, so the contrib service tables obey the same ops contract
  as stapel journals (hidden by default, ``SHOW_OPS_MODELS`` reveals them
  read-only). Their *category* comes from
  :data:`stapel_core.access.declaration.CONTRIB_OPS_LABELS` and is enforced
  by the backend regardless of this hook; the hook only adds the admin-layer
  behavior (read-only, masking, dev-mode visibility).
- **``STAPEL_ADMIN["MODELS"]`` admin-side semantics** — ``None`` unregisters
  the model entirely (direct URL becomes 404; API-level permissions are
  untouched), ``admin_class`` swaps the registered admin for the host's one.
  The access-shaped keys of an entry are consumed by
  :func:`stapel_core.access.effective_access` instead (one resolution, §3.7).

Both run from ``CommonDjangoConfig.ready()`` — list ``stapel_core.django``
*after* ``django.contrib.admin`` (the standard layout) so autodiscover has
already filled the registry. Exotic layouts can call
:func:`setup_admin_visibility` themselves.
"""
from __future__ import annotations

from typing import Mapping

from django.contrib import admin

from .base import StapelModelAdmin

_group_admin_class: type | None = None


def group_admin_class() -> type:
    """``StapelGroupAdmin`` — contrib Group under the ops contract.

    Groups are the DAC surface — editing one grants permissions; per Q9 they
    are machinery, not staff working material. Hidden below clearance HIGH,
    read-only for everyone in the admin (grants are managed by fixtures /
    ``ensure_staff_group_permissions`` / the mandate, not by hand). A host
    that wants the old editable Group back re-categorizes it:
    ``STAPEL_ADMIN = {"MODELS": {"auth.Group": {"category": "business"}}}``.

    Built lazily: importing ``django.contrib.auth.admin`` registers into the
    default site and therefore requires ``django.contrib.admin`` installed —
    a top-level import would break projects without the admin app.
    """
    global _group_admin_class
    if _group_admin_class is None:
        from django.contrib.auth.admin import GroupAdmin as DjangoGroupAdmin

        class StapelGroupAdmin(StapelModelAdmin, DjangoGroupAdmin):
            __doc__ = group_admin_class.__doc__

        _group_admin_class = StapelGroupAdmin
    return _group_admin_class


class StapelSessionAdmin(StapelModelAdmin):
    """contrib Session — ops journal with the key material masked."""

    list_display = ("session_key", "expire_date")
    ordering = ("-expire_date",)
    # Explicit masking (session hijack material) on top of the ops category.
    secret_fields = ("session_key", "session_data")


def setup_contrib_admins(site: admin.AdminSite | None = None) -> None:
    """Re-register django.contrib service tables under ops-aware admins (Q9)."""
    site = site or admin.site
    from django.apps import apps as django_apps

    if django_apps.is_installed("django.contrib.auth"):
        from django.contrib.auth.models import Group

        if site.is_registered(Group):
            site.unregister(Group)
        site.register(Group, group_admin_class())

    if django_apps.is_installed("django.contrib.sessions"):
        from django.contrib.sessions.models import Session

        if not site.is_registered(Session):
            site.register(Session, StapelSessionAdmin)


def apply_admin_overrides(site: admin.AdminSite | None = None) -> None:
    """Apply the admin-side keys of ``STAPEL_ADMIN["MODELS"]``.

    ``None`` → unregister; ``admin_class`` (dotted path or class) → swap.
    Unknown labels are skipped (shared deploy config may target another
    service's models — the ``stapel_admin`` system check W-flags them);
    malformed entries are skipped here and E-flagged by the same check.
    """
    site = site or admin.site
    from django.apps import apps as django_apps
    from django.utils.module_loading import import_string

    from .conf import admin_settings

    for label, entry in (admin_settings.MODELS or {}).items():
        try:
            model = django_apps.get_model(label)
        except (LookupError, ValueError):
            continue  # another service's model — W-checked, not an error
        if entry is None:
            if site.is_registered(model):
                site.unregister(model)
            continue
        if not isinstance(entry, Mapping):
            continue  # E-checked
        admin_class = entry.get("admin_class")
        if not admin_class:
            continue  # pure declaration patch — handled by effective_access
        cls = import_string(admin_class) if isinstance(admin_class, str) else admin_class
        if site.is_registered(model):
            site.unregister(model)
        site.register(model, cls)


def setup_admin_visibility(site: admin.AdminSite | None = None) -> None:
    """Contrib re-registration + host overrides, in that order (host wins)."""
    from django.apps import apps as django_apps

    if not django_apps.is_installed("django.contrib.admin"):
        return
    setup_contrib_admins(site)
    apply_admin_overrides(site)


__all__ = [
    "StapelSessionAdmin",
    "apply_admin_overrides",
    "group_admin_class",
    "setup_admin_visibility",
    "setup_contrib_admins",
]
