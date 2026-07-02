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
from .exceptions import FunctionNotRegistered, SchemaValidationError

logger = logging.getLogger(__name__)

ActionHandler = Callable[..., None]
FunctionHandler = Callable[[dict], Any]


def _validate(name: str, payload: dict, schema: dict | None) -> None:
    if not schema or not validation_enabled():
        return
    try:
        import jsonschema
    except ImportError:  # validation is best-effort tooling, not runtime dep
        logger.debug("jsonschema not installed; skipping validation for %s", name)
        return
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


class ActionRegistry:
    """name → 0..N subscriber callables."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[ActionHandler]] = {}
        self._schemas: dict[str, dict | None] = {}
        self._lock = threading.Lock()

    def subscribe(self, name: str, handler: ActionHandler) -> None:
        with self._lock:
            handlers = self._subscribers.setdefault(name, [])
            if handler not in handlers:
                handlers.append(handler)

    def register_schema(self, name: str, schema: dict | None) -> None:
        with self._lock:
            if schema is not None:
                self._schemas[name] = schema

    def handlers(self, name: str) -> list[ActionHandler]:
        return list(self._subscribers.get(name, []))

    def validate(self, name: str, payload: dict) -> None:
        _validate(name, payload, self._schemas.get(name))

    def names(self) -> list[str]:
        return sorted(self._subscribers)

    def clear(self) -> None:
        """Tests only."""
        with self._lock:
            self._subscribers.clear()
            self._schemas.clear()


function_registry = FunctionRegistry()
action_registry = ActionRegistry()
