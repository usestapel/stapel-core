"""Merge-registry of verbs — deny-by-default.

Two sources, merged at resolve time (the core merge-registry pattern —
open registries merge over builtins):

1. **Code** — modules register in ``AppConfig.ready()`` via
   :func:`register_verb` / the ``@verb`` decorator.
2. **Settings** — ``STAPEL_GATEWAY["VERBS"]``: per-verb dict entries patch
   a code-registered verb (policy merges per key, schema/handler replace),
   declare brand-new settings-only verbs, or — with an entry of ``None`` —
   disable a verb entirely.

A name in neither source **does not exist**: resolution raises
:class:`VerbNotDeclared`. There is no wildcard, no fallback handler, no
"unknown verb passthrough".
"""
from __future__ import annotations

import threading
from typing import Any, Mapping

from .base import VerbDeclaration, VerbPolicy
from .exceptions import GatewayConfigError, VerbNotDeclared


class VerbRegistry:
    def __init__(self) -> None:
        self._verbs: dict[str, VerbDeclaration] = {}
        self._lock = threading.Lock()

    def register(self, declaration: VerbDeclaration) -> None:
        with self._lock:
            existing = self._verbs.get(declaration.name)
            if existing is not None and existing != declaration:
                raise ValueError(
                    f"verb {declaration.name!r} already registered; "
                    "a verb name has exactly one declaration "
                    "(patch it via STAPEL_GATEWAY['VERBS'] instead)"
                )
            self._verbs[declaration.name] = declaration

    def _settings_entry(self, name: str) -> tuple[bool, Any]:
        """(present, value) of the settings-side entry for *name*."""
        from .conf import gateway_settings

        overlay: Mapping[str, Any] = gateway_settings.VERBS or {}
        if name in overlay:
            return True, overlay[name]
        return False, None

    def resolve(self, name: str) -> VerbDeclaration:
        """The effective declaration of *name* (code merged with settings).

        Raises :class:`VerbNotDeclared` when the name exists in neither
        source or is disabled by a ``None`` settings entry.
        """
        base = self._verbs.get(name)
        present, entry = self._settings_entry(name)

        if not present:
            if base is None:
                raise VerbNotDeclared(f"verb {name!r} is not declared")
            return base
        if entry is None:
            # Explicit disable: back to deny-by-default.
            raise VerbNotDeclared(f"verb {name!r} is disabled by settings")
        if not isinstance(entry, Mapping):
            raise GatewayConfigError(
                f"STAPEL_GATEWAY['VERBS'][{name!r}] must be a dict or None, "
                f"got {type(entry).__name__}"
            )

        unknown = set(entry) - {"schema", "handler", "policy"}
        if unknown:
            raise GatewayConfigError(
                f"STAPEL_GATEWAY['VERBS'][{name!r}] has unknown keys: {sorted(unknown)}"
            )

        if base is None:
            # Settings-only verb: must be complete.
            try:
                return VerbDeclaration(
                    name=name,
                    schema=entry.get("schema"),
                    handler=entry.get("handler"),
                    policy=VerbPolicy.from_mapping(entry.get("policy")),
                )
            except (TypeError, ValueError) as exc:
                raise GatewayConfigError(
                    f"STAPEL_GATEWAY['VERBS'][{name!r}] is not a valid declaration: {exc}"
                ) from exc

        # Patch over the code-registered declaration.
        policy = base.policy
        if "policy" in entry:
            try:
                policy = policy.merged(entry["policy"] or {})
            except (TypeError, ValueError) as exc:
                raise GatewayConfigError(
                    f"STAPEL_GATEWAY['VERBS'][{name!r}]['policy'] is invalid: {exc}"
                ) from exc
        try:
            return VerbDeclaration(
                name=name,
                schema=entry.get("schema", base.schema),
                handler=entry.get("handler", base.handler),
                policy=policy,
            )
        except (TypeError, ValueError) as exc:
            raise GatewayConfigError(
                f"STAPEL_GATEWAY['VERBS'][{name!r}] patch is invalid: {exc}"
            ) from exc

    def names(self) -> list[str]:
        """Effective verb names: code + settings, minus disabled."""
        from .conf import gateway_settings

        overlay: Mapping[str, Any] = gateway_settings.VERBS or {}
        names = set(self._verbs)
        for name, entry in overlay.items():
            if entry is None:
                names.discard(name)
            else:
                names.add(name)
        return sorted(names)

    def clear(self) -> None:
        """Tests only."""
        with self._lock:
            self._verbs.clear()


verb_registry = VerbRegistry()


def register_verb(
    name: str,
    *,
    schema: dict,
    handler,
    policy: VerbPolicy | Mapping[str, Any] | None = None,
) -> None:
    """Declare a verb in code (call from ``AppConfig.ready()``)."""
    verb_registry.register(
        VerbDeclaration(
            name=name,
            schema=schema,
            handler=handler,
            policy=VerbPolicy.from_mapping(policy),
        )
    )


def verb(name: str, *, schema: dict, policy: VerbPolicy | Mapping[str, Any] | None = None):
    """Decorator flavor of :func:`register_verb`::

        @verb("send_email", schema={...}, policy={"rate_limit": "30/h"})
        def send_email(args: dict, caller: CallerContext) -> dict: ...
    """

    def decorator(handler):
        register_verb(name, schema=schema, handler=handler, policy=policy)
        return handler

    return decorator


__all__ = ["VerbRegistry", "register_verb", "verb", "verb_registry"]
