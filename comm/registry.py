"""Name-addressed registries — the loose-coupling seam.

Providers/subscribers register under a string name at import time (via the
decorators in actions.py / functions.py, usually from AppConfig.ready()).
Callers resolve by name. Neither side imports the other.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from .config import validation_enabled
from .exceptions import (
    FunctionNotRegistered,
    SchemaValidationError,
    SchemaValidatorUnavailable,
)

logger = logging.getLogger(__name__)

ActionHandler = Callable[..., None]
FunctionHandler = Callable[[dict], Any]


def _validate(name: str, payload: dict, schema: dict | None) -> None:
    if not schema or not validation_enabled():
        return
    try:
        import jsonschema
    except ImportError as exc:
        # Fail closed. A silent skip here meant a declared schema and an
        # unchecked payload looked identical from the outside, forever.
        raise SchemaValidatorUnavailable(
            f"payload for '{name}' declares a schema and validation is on, "
            "but jsonschema is not installed, so nothing can be checked "
            "(pip install jsonschema). Set STAPEL_COMM[\"VALIDATE_SCHEMAS\"] "
            "= False to state, explicitly, that this deployment accepts "
            "unvalidated payloads."
        ) from exc
    try:
        jsonschema.validate(payload, schema)
    except jsonschema.ValidationError as exc:
        raise SchemaValidationError(f"payload for '{name}' violates schema: {exc.message}") from exc


class FunctionRegistry:
    """name → exactly one provider callable."""

    def __init__(self) -> None:
        self._providers: dict[str, FunctionHandler] = {}
        self._schemas: dict[str, dict | None] = {}
        self._lock = threading.Lock()

    def register(self, name: str, handler: FunctionHandler, *, schema: dict | None = None) -> None:
        with self._lock:
            existing = self._providers.get(name)
            if existing is not None and existing is not handler:
                raise ValueError(
                    f"function '{name}' already registered by {existing!r}; "
                    "a function name has exactly one provider"
                )
            self._providers[name] = handler
            self._schemas[name] = schema

    def register_schema(self, name: str, schema: dict | None) -> None:
        """Attach/override a payload schema without touching the provider
        (used by the schemas/ autoloader)."""
        with self._lock:
            if schema is not None:
                self._schemas[name] = schema

    def get(self, name: str) -> FunctionHandler:
        try:
            return self._providers[name]
        except KeyError:
            raise FunctionNotRegistered(
                f"no provider registered for function '{name}' "
                "(is the owning app in INSTALLED_APPS?)"
            ) from None

    def validate(self, name: str, payload: dict) -> None:
        _validate(name, payload, self._schemas.get(name))

    def names(self) -> list[str]:
        return sorted(self._providers)

    def clear(self) -> None:
        """Tests only."""
        with self._lock:
            self._providers.clear()
            self._schemas.clear()


def _caller_package(depth: int = 2) -> str:
    """Top-level package of the code that called into the registry."""
    import sys

    frame = sys._getframe(depth)
    while frame is not None:
        module = frame.f_globals.get("__name__", "")
        if module and not module.startswith("stapel_core.comm"):
            return module.split(".")[0]
        frame = frame.f_back
    return "unknown"


class ActionRegistry:
    """name → 0..N subscriber callables, and one EMIT schema per name.

    Two kinds of schema, two owners:

    - the **emit schema** is the fact's contract, declared by the module that
      emits it (its ``schemas/emits/<name>.json``, loaded by
      :func:`~stapel_core.comm.schemas.autoload_schemas`, or
      :meth:`register_schema`). :func:`~stapel_core.comm.emit` validates
      against it. Nobody else writes it.
    - a **consumer schema** is what ``on_action(name, schema=...)`` passes. It
      is stored per subscription and checked only on what THAT handler is
      about to receive; a mismatch parks the delivery to that handler and
      never reaches the emitter. A consumer schema stricter than the owner's
      therefore filters that one consumer and nothing else.

    Two declared emitters of one name with different schemas are a real
    conflict, reported by ``stapel_core.comm.E011`` (see
    :mod:`stapel_core.comm.schema_ownership`). The first declaration stays the
    effective one until it is resolved; a later one never silently replaces it.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[ActionHandler]] = {}
        #: name → the effective emit schema (the first owner's declaration).
        self._schemas: dict[str, dict | None] = {}
        #: name → {owner package: declared emit schema}, in declaration order.
        self._emit_declarations: dict[str, dict[str, dict]] = {}
        #: name → [(handler, consumer schema)], one entry per subscription.
        self._consumer_schemas: dict[str, list[tuple[ActionHandler, dict]]] = {}
        self._lock = threading.Lock()

    def subscribe(
        self, name: str, handler: ActionHandler, *, schema: dict | None = None
    ) -> None:
        """Subscribe *handler*; *schema* (optional) guards only its deliveries."""
        with self._lock:
            handlers = self._subscribers.setdefault(name, [])
            if handler not in handlers:
                handlers.append(handler)
            if schema is not None:
                entries = self._consumer_schemas.setdefault(name, [])
                entries[:] = [(h, s) for h, s in entries if h != handler]
                entries.append((handler, schema))

    def register_schema(
        self, name: str, schema: dict | None, *, owner: str | None = None
    ) -> None:
        """Declare the EMIT schema of *name* on behalf of *owner*.

        *owner* is the emitting package (``app_config.name`` for the
        ``schemas/emits/`` autoload); omitted, it is the caller's top-level
        package. The same owner may re-declare (the latest wins); a second
        owner is recorded for the conflict check and never replaces the
        first one's schema.
        """
        if schema is None:
            return
        owner = owner or _caller_package()
        with self._lock:
            declarations = self._emit_declarations.setdefault(name, {})
            declarations[owner] = schema
            first_owner = next(iter(declarations))
            self._schemas[name] = declarations[first_owner]

    def handlers(self, name: str) -> list[ActionHandler]:
        return list(self._subscribers.get(name, []))

    def validate(self, name: str, payload: dict) -> None:
        """Validate an EMIT of *name* against its owner's schema."""
        _validate(name, payload, self._schemas.get(name))

    def consumer_schema(self, name: str, handler: ActionHandler) -> dict | None:
        for subscribed, schema in self._consumer_schemas.get(name, ()):
            if subscribed == handler:
                return schema
        return None

    def validate_delivery(self, name: str, handler: ActionHandler, payload: dict) -> None:
        """Validate what *handler* is about to receive against ITS schema."""
        _validate(name, payload, self.consumer_schema(name, handler))

    def emit_declarations(self) -> dict[str, dict[str, dict]]:
        """``{name: {owner: schema}}`` for every declared emit schema."""
        with self._lock:
            return {name: dict(owners) for name, owners in self._emit_declarations.items()}

    def consumer_schemas(self) -> dict[str, list[tuple[ActionHandler, dict]]]:
        """``{name: [(handler, schema)]}`` for every subscription with a schema."""
        with self._lock:
            return {name: list(entries) for name, entries in self._consumer_schemas.items()}

    def names(self) -> list[str]:
        return sorted(self._subscribers)

    def clear(self) -> None:
        """Tests only."""
        with self._lock:
            self._subscribers.clear()
            self._schemas.clear()
            self._emit_declarations.clear()
            self._consumer_schemas.clear()


function_registry = FunctionRegistry()
action_registry = ActionRegistry()
