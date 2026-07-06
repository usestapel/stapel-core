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
    """The code-side declaration of *model* (implicit standard if undecorated)."""
    return getattr(model, DECLARATION_ATTR, STANDARD)


def is_declared(model: type) -> bool:
    """True when *model* carries an explicit ``@access`` declaration."""
    return getattr(model, DECLARATION_ATTR, None) is not None


def effective_access(model: type) -> AccessDeclaration:
    """Declaration of *model* with the ``STAPEL_ACCESS["MODELS"]`` overlay.

    Overlay keys for apps that are not installed in this service are legal
    (one deploy config is shared across microservices) — they simply never
    match a local model; the system check flags them at W-level as a typo
    guard, never E.
    """
    from .conf import access_settings

    overlay: Mapping[str, Any] = access_settings.MODELS or {}
    label = model._meta.label  # "app_label.ModelName"
    if label not in overlay:
        return declared_access(model)
    entry = overlay[label]
    if entry is None:
        # Remove the module's declaration: back to the implicit default.
        return STANDARD
    return declared_access(model).patched(
        entry, source=f"STAPEL_ACCESS['MODELS'][{label!r}]"
    )


__all__ = [
    "ACTIONS",
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
