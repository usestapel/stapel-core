"""
Routing bus backend — a different broker per topic prefix.

Lets one deployment split primitives across brokers, e.g. Tasks on Kafka
(hard retention/replay) while ordinary events stay on NATS:

    STAPEL_BUS_BACKEND=routing
    STAPEL_BUS_ROUTES={"task.": "kafka", "": "nats"}

``STAPEL_BUS_ROUTES`` maps a topic prefix to a backend — a shorthand
(``memory`` | ``kafka`` | ``nats``) or any dotted path. Resolution is
longest-prefix-wins; the empty prefix ``""`` is the default route.
Configure it as env JSON (12-factor, wins) or a Django setting dict of
the same name. One backend instance is created lazily and cached per
distinct target, so two prefixes pointing at the same broker share a
connection.

``consume()`` requires every requested topic to resolve to the SAME
backend: a consumer process binds to one broker. Mixed topics raise
``ValueError`` — split the consumer instead.
"""
from __future__ import annotations

import importlib
import json
import os
import threading
from typing import Callable

from ..base import BusBackend
from ..event import Event

_ROUTES_NAME = "STAPEL_BUS_ROUTES"


def _load_routes() -> dict[str, str]:
    raw = os.environ.get(_ROUTES_NAME, "")
    if raw:
        try:
            routes = json.loads(raw)
        except ValueError as exc:
            raise ValueError(
                f"{_ROUTES_NAME} env var is not valid JSON: {exc}"
            ) from exc
    else:
        try:
            from django.conf import settings

            routes = getattr(settings, _ROUTES_NAME, None)
        except Exception:  # settings not configured (plain scripts)
            routes = None
    if not routes:
        raise ValueError(
            f"RoutingBus needs {_ROUTES_NAME} — a JSON env var or Django "
            'setting dict mapping topic prefix to backend, e.g. '
            '{"task.": "kafka", "": "nats"}'
        )
    if not isinstance(routes, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in routes.items()
    ):
        raise ValueError(
            f"{_ROUTES_NAME} must be a dict of str prefix -> str backend, "
            f"got {routes!r}"
        )
    return dict(routes)


class RoutingBus(BusBackend):
    """Delegate publish/consume to per-topic-prefix backends."""

    def __init__(self) -> None:
        self._routes = _load_routes()
        self._backends: dict[str, BusBackend] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Route resolution
    # ------------------------------------------------------------------

    def _target_for(self, topic: str) -> str:
        """Resolved dotted path of the backend owning *topic*.

        Longest matching prefix wins; ``""`` matches everything (default).
        """
        from ..router import SHORTHANDS

        best: str | None = None
        for prefix in self._routes:
            if topic.startswith(prefix) and (best is None or len(prefix) > len(best)):
                best = prefix
        if best is None:
            raise ValueError(
                f"no {_ROUTES_NAME} route matches topic {topic!r} — add a "
                'default route with the empty prefix ""'
            )
        target = self._routes[best]
        dotted = SHORTHANDS.get(target, target)
        if dotted == SHORTHANDS["routing"]:
            raise ValueError(
                f"{_ROUTES_NAME} route {best!r} points back at the routing "
                "backend — routes must target concrete backends"
            )
        return dotted

    def _backend_for(self, dotted: str) -> BusBackend:
        with self._lock:
            backend = self._backends.get(dotted)
            if backend is None:
                module_path, class_name = dotted.rsplit(".", 1)
                backend = getattr(importlib.import_module(module_path), class_name)()
                self._backends[dotted] = backend
        return backend

    # ------------------------------------------------------------------
    # BusBackend interface
    # ------------------------------------------------------------------

    def publish(self, topic: str, event: Event) -> None:
        self._backend_for(self._target_for(topic)).publish(topic, event)

    def consume(
        self,
        topics: list[str],
        group: str,
        handler: Callable[[Event], None],
        *,
        poll_timeout: float = 0.1,
    ) -> None:
        targets = {topic: self._target_for(topic) for topic in topics}
        distinct = sorted(set(targets.values()))
        if len(distinct) != 1:
            raise ValueError(
                "consume() topics resolve to different bus backends "
                f"({targets!r}); a consumer process binds to one broker — "
                "split the consumer so each process only consumes topics "
                "from a single backend"
            )
        self._backend_for(distinct[0]).consume(
            topics, group, handler, poll_timeout=poll_timeout
        )
