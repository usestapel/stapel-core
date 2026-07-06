"""``StapelModelAdmin`` — access-declaration-aware ModelAdmin (admin-suite AS-3).

Enforcement of *visibility* is not done here — it is done by
:class:`stapel_core.access.backend.MandateBackend` through the standard
``has_perm`` protocol (Django admin consults it for every list/change/add
URL, so a direct ``/admin/app/model/`` link is closed exactly like the index
entry). This class adds the *admin-specific* behavior that the backend cannot
express:

- **ops** — read-only even for a superuser (the declaration forbids
  add/change/delete, but a superuser bypasses the mandate, A5; the read-only
  journal contract has to be re-imposed at the admin layer). With
  ``STAPEL_ADMIN["SHOW_OPS_MODELS"]`` the model becomes viewable by any staff
  (dev mode) while staying read-only.
- **secret** — secret-bearing fields are masked: excluded from forms, and
  rendered as a read-only placeholder, so the plaintext never reaches the
  response — even for the superuser who is the only one able to open it.
  Fields are auto-detected by name patterns or pinned with
  :attr:`~StapelModelAdmin.secret_fields` (which also masks on non-secret
  models, e.g. a session key on an ops journal).
- **business** — plain ModelAdmin behavior; the mandate does all the work.

A bare ``admin.ModelAdmin`` keeps working (the backend still enforces
visibility) — subclassing this only adds the category cosmetics.
"""
from __future__ import annotations

from django.contrib import admin
from django.core.exceptions import FieldDoesNotExist
from django.utils.html import format_html

from stapel_core.access import effective_access

from .conf import show_ops_models

#: Substrings marking a field as secret-bearing on a ``secret`` model. Used
#: only when :attr:`StapelModelAdmin.secret_fields` is not given explicitly.
SECRET_FIELD_PATTERNS = (
    "token", "secret", "password", "hash", "key", "signature",
    "credential", "private", "salt",
)

#: Rendered in place of a masked secret value.
MASK_PLACEHOLDER = "•••••• (masked)"

_MASK_PREFIX = "stapel_masked_"


class StapelModelAdmin(admin.ModelAdmin):
    """ModelAdmin that reads the model's ``@access`` declaration."""

    #: Explicit secret field names. When set, exactly these are masked (on
    #: any category); when empty, pattern detection runs on secret models.
    secret_fields: tuple[str, ...] = ()
    #: Substrings used to auto-detect secret fields on a ``secret`` model.
    secret_field_patterns: tuple[str, ...] = SECRET_FIELD_PATTERNS

    # -- declaration helpers ------------------------------------------------

    def _category(self) -> str:
        return effective_access(self.model).category

    def _masked_field_names(self) -> tuple[str, ...]:
        """Concrete field names to mask.

        An explicit ``secret_fields`` list always masks, whatever the
        category; pattern auto-detection engages only for ``secret`` models.
        A host that re-categorizes a secret model via the MODELS registries
        keeps the explicit masking (declared secrets stay masked cosmetics
        aside — see the W-level ``stapel_admin`` check on downgrades).
        """
        if self.secret_fields:
            return tuple(self.secret_fields)
        if effective_access(self.model).category != "secret":
            return ()
        patterns = self.secret_field_patterns
        return tuple(
            f.name
            for f in self.model._meta.concrete_fields
            if any(p in f.name.lower() for p in patterns)
        )

    # -- permission enforcement (admin layer over the backend) --------------

    def has_view_permission(self, request, obj=None):
        if self._category() == "ops" and show_ops_models():
            # Dev mode: any staff user may look at ops journals (read-only).
            return bool(getattr(request.user, "is_staff", False))
        return super().has_view_permission(request, obj)

    def has_module_permission(self, request):
        if self._category() == "ops" and show_ops_models():
            return bool(getattr(request.user, "is_staff", False))
        return super().has_module_permission(request)

    def has_add_permission(self, request):
        if self._category() == "ops":
            return False  # read-only journal — even for a superuser (§1.1)
        return super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        if self._category() == "ops":
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if self._category() == "ops":
            return False
        return super().has_delete_permission(request, obj)

    # -- read-only / masked rendering ---------------------------------------

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        masked = self._masked_field_names()
        if self._category() == "ops":
            # Every concrete field read-only; masked ones via the placeholder.
            for field in self.model._meta.concrete_fields:
                name = (
                    self._mask_display_name(field.name)
                    if field.name in masked
                    else field.name
                )
                if name not in readonly and field.name not in readonly:
                    readonly.append(name)
            return readonly
        for name in masked:
            display = self._mask_display_name(name)
            if display not in readonly:
                readonly.append(display)
        return readonly

    def get_exclude(self, request, obj=None):
        """Masked fields never become form fields — no widget, no initial value."""
        exclude = list(super().get_exclude(request, obj) or ())
        for name in self._masked_field_names():
            if name not in exclude:
                exclude.append(name)
        return exclude or None

    def get_list_display(self, request):
        masked = self._masked_field_names()
        return [
            self._mask_display_name(name) if name in masked else name
            for name in super().get_list_display(request)
        ]

    def get_search_fields(self, request):
        """Masked fields are not searchable — icontains probing is an oracle."""
        masked = set(self._masked_field_names())
        return [
            lookup
            for lookup in super().get_search_fields(request)
            if lookup.split("__")[0].lstrip("^=@") not in masked
        ]

    # -- masking machinery ---------------------------------------------------

    def _mask_display_name(self, field_name: str) -> str:
        """Name of the read-only callable that renders *field_name* masked.

        A callable (rather than the raw field name) is registered on the
        admin instance so Django's readonly rendering resolves it first and
        the real attribute value never reaches the template.
        """
        attr = f"{_MASK_PREFIX}{field_name}"
        if not hasattr(self, attr):
            def render(obj, _field=field_name):
                value = getattr(obj, _field, None)
                if value in (None, ""):
                    return "—"
                return format_html("<code>{}</code>", MASK_PLACEHOLDER)

            try:
                label = self.model._meta.get_field(field_name).verbose_name
            except FieldDoesNotExist:
                label = field_name
            render.short_description = label  # type: ignore[attr-defined]
            setattr(self, attr, render)
        return attr


__all__ = [
    "MASK_PLACEHOLDER",
    "SECRET_FIELD_PATTERNS",
    "StapelModelAdmin",
]
