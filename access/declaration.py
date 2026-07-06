"""Model access declarations — the ``@access`` decorator and presets.

One declaration, three consumers (admin-suite §0): admin visibility (AS-3),
default staff rights (this package), and the audit report. The declaration
lives on the model class as a plain attribute — no ``Meta.permissions``, no
migrations; changing a decorator takes effect on deploy (§3.6).

An *undeclared* model behaves as ``@access.standard`` (business; view=LOW,
add/change=MID, delete=HIGH) — the "important tables are visible by default"
requirement is met with zero effort from a module.

Host override: ``STAPEL_ACCESS["MODELS"]`` is a merge-registry over the
decorators (canonical seam semantics — dict entry patches per key, ``None``
removes the declaration, i.e. falls back to implicit standard). Keys are
``"app_label.ModelName"`` (``model._meta.label``).
"""
from __future__ import annotations

import dataclasses
from typing import Any, Mapping

from .exceptions import AccessConfigError
from .levels import Level, parse_category

#: Attribute the decorator stores the declaration under. Deliberately a
#: plain (MRO-inherited) attribute: a subclass of a ``secret`` model stays
#: secret unless it declares otherwise — fail-closed.
DECLARATION_ATTR = "_stapel_access_declaration"

#: Operations the mandate governs (the Django default model permissions).
ACTIONS = ("view", "add", "change", "delete")

#: django.contrib service tables pinned to the ``ops`` category (Q9 /
#: admin-suite §6.8 — the "закрепить списком в коре" option): infrastructure
#: the host project did not author and staff should not casually browse
#: (groups drive DAC grants, sessions carry live login state). Applies only
#: while the model has no explicit ``@access`` declaration; the host can
#: re-categorize through the MODELS registries like for any other model.
CONTRIB_OPS_LABELS = frozenset({
    "admin.LogEntry",
    "auth.Group",
    "auth.Permission",
    "contenttypes.ContentType",
    "sessions.Session",
})


@dataclasses.dataclass(frozen=True)
class AccessDeclaration:
    category: str = "business"
    view: Level = Level.LOW
    add: Level = Level.MID
    change: Level = Level.MID
    delete: Level = Level.HIGH

    def required(self, action: str) -> Level:
        if action not in ACTIONS:
            raise ValueError(f"unknown access action {action!r}")
        return getattr(self, action)

    def patched(self, entry: Mapping[str, Any], *, source: str = "override") -> "AccessDeclaration":
        """A copy with *entry* merged in (settings-override semantics)."""
        if not isinstance(entry, Mapping):
            raise AccessConfigError(
                f"{source} must be a dict or None, got {type(entry).__name__}"
            )
        unknown = set(entry) - {"category", *ACTIONS}
        if unknown:
            raise AccessConfigError(f"{source} has unknown keys: {sorted(unknown)}")
        changes: dict[str, Any] = {}
        if "category" in entry:
            changes["category"] = parse_category(entry["category"])
        for action in ACTIONS:
            if action in entry:
                changes[action] = Level.parse(entry[action])
        return dataclasses.replace(self, **changes)


STANDARD = AccessDeclaration()
SENSITIVE = AccessDeclaration(
    view=Level.MID, add=Level.HIGH, change=Level.HIGH, delete=Level.HIGH
)
OPS = AccessDeclaration(
    category="ops",
    view=Level.HIGH,
    add=Level.FORBIDDEN,
    change=Level.FORBIDDEN,
    delete=Level.FORBIDDEN,
)
SECRET = AccessDeclaration(
    category="secret",
    view=Level.SUPERUSER,
    add=Level.SUPERUSER,
    change=Level.SUPERUSER,
    delete=Level.SUPERUSER,
)


class _AccessDecorator:
    """``@access(...)`` / ``@access.standard`` — see the module docstring."""

    def __call__(
        self,
        model: type | None = None,
        *,
        category: str = "business",
        view: Level | str = Level.LOW,
        add: Level | str = Level.MID,
        change: Level | str = Level.MID,
        delete: Level | str = Level.HIGH,
    ):
        if model is not None:  # bare ``@access`` — explicit standard
            return self._apply(model, STANDARD)
        declaration = AccessDeclaration(
            category=parse_category(category),
            view=Level.parse(view),
            add=Level.parse(add),
            change=Level.parse(change),
            delete=Level.parse(delete),
        )

        def decorator(cls: type) -> type:
            return self._apply(cls, declaration)

        return decorator

    @staticmethod
    def _apply(cls: type, declaration: AccessDeclaration) -> type:
        setattr(cls, DECLARATION_ATTR, declaration)
        return cls

    # Presets (admin-suite §3.2) — bound methods double as bare decorators.
    def standard(self, cls: type) -> type:
        """business; view=LOW, add/change=MID, delete=HIGH (the default)."""
        return self._apply(cls, STANDARD)

    def sensitive(self, cls: type) -> type:
        """business, but PII/money-grade: view=MID, add/change/delete=HIGH."""
        return self._apply(cls, SENSITIVE)

    def ops(self, cls: type) -> type:
        """ops journal: view=HIGH, mutations forbidden by declaration."""
        return self._apply(cls, OPS)

    def secret(self, cls: type) -> type:
        """secret carrier: every operation superuser-only."""
        return self._apply(cls, SECRET)


access = _AccessDecorator()


def declared_access(model: type) -> AccessDeclaration:
    """The code-side declaration of *model* (implicit standard if undecorated).

    Undecorated django.contrib service tables (:data:`CONTRIB_OPS_LABELS`)
    default to ``ops`` instead — Q9: auth.Group / sessions are machinery,
    hidden unless the host raises them or flips ``SHOW_OPS_MODELS``.
    """
    declaration = getattr(model, DECLARATION_ATTR, None)
    if declaration is not None:
        return declaration
    meta = getattr(model, "_meta", None)
    if meta is not None and getattr(meta, "label", None) in CONTRIB_OPS_LABELS:
        return OPS
    return STANDARD


def is_declared(model: type) -> bool:
    """True when *model* carries an explicit ``@access`` declaration."""
    return getattr(model, DECLARATION_ATTR, None) is not None


#: Category name → the preset declaration an ``STAPEL_ADMIN`` category
#: override re-bases on (see :func:`effective_access`).
CATEGORY_PRESETS: Mapping[str, AccessDeclaration] = {
    "business": STANDARD,
    "ops": OPS,
    "secret": SECRET,
}


def effective_access(model: type) -> AccessDeclaration:
    """Declaration of *model* with the settings overlays applied.

    One resolution for both host registries (admin-suite §3.7):

    1. ``STAPEL_ACCESS["MODELS"]`` — patches the declaration per key
       (``None`` = drop the module's declaration, back to implicit standard).
    2. ``STAPEL_ADMIN["MODELS"]`` — the *access-shaped* keys of an entry are
       applied here too, so ``{"category": "business"}`` on an ops journal is
       real visibility (the backend grants view), not app-list cosmetics.
       A ``category`` key **re-bases** the declaration on that category's
       preset (that is what "show to every staff" means — §1.4 example),
       then the remaining level keys patch on top. Admin-only keys
       (``admin_class``) and the ``None`` entry (= unregister from the admin;
       API-level permissions unchanged) are consumed at registration time
       (:mod:`stapel_core.django.admin.registration`), not here.

    Overlay keys for apps that are not installed in this service are legal
    (one deploy config is shared across microservices) — they simply never
    match a local model; the system checks flag them at W-level as a typo
    guard, never E.
    """
    from .conf import access_settings

    label = model._meta.label  # "app_label.ModelName"
    declaration = declared_access(model)

    overlay: Mapping[str, Any] = access_settings.MODELS or {}
    if label in overlay:
        entry = overlay[label]
        if entry is None:
            # Remove the module's declaration: back to the implicit default.
            declaration = STANDARD
        else:
            declaration = declaration.patched(
                entry, source=f"STAPEL_ACCESS['MODELS'][{label!r}]"
            )

    admin_entry = _admin_models_overlay().get(label)
    if isinstance(admin_entry, Mapping):
        source = f"STAPEL_ADMIN['MODELS'][{label!r}]"
        access_entry = {
            key: value for key, value in admin_entry.items()
            if key in ("category", *ACTIONS)
        }
        if "category" in access_entry:
            declaration = CATEGORY_PRESETS[parse_category(access_entry.pop("category"))]
        if access_entry:
            declaration = declaration.patched(access_entry, source=source)
    return declaration


def _admin_models_overlay() -> Mapping[str, Any]:
    from stapel_core.django.admin.conf import admin_settings

    return admin_settings.MODELS or {}


__all__ = [
    "ACTIONS",
    "CATEGORY_PRESETS",
    "CONTRIB_OPS_LABELS",
    "AccessDeclaration",
    "DECLARATION_ATTR",
    "OPS",
    "SECRET",
    "SENSITIVE",
    "STANDARD",
    "access",
    "declared_access",
    "effective_access",
    "is_declared",
]
