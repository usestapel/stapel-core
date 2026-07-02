"""NATS request-reply transport for the Function primitive.

The recommended RPC for multi-service deployments: one multiplexed TCP
connection per process (no pool exhaustion), protocol-level request
correlation and timeouts, and queue groups load-balance across service
replicas. No FUNCTION_ROUTES needed — the subject IS the function name.

    STAPEL_COMM = {
        "FUNCTION_TRANSPORT": "nats",
        # "NATS_URL": "nats://nats:4222",
    }

Provider side runs ``python manage.py serve_functions`` (a worker process,
like celery) which subscribes every registered function.

nats-py is asyncio-only while Django call sites are synchronous, so the
client keeps a single dedicated event-loop thread and bridges calls with
run_coroutine_threadsafe — connect once, reuse forever (nats-py handles
reconnects internally).
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

from .config import comm_setting
from .exceptions import FunctionCallError, FunctionNotRegistered

logger = logging.getLogger(__name__)

DEFAULT_NATS_URL = "nats://nats:4222"
DEFAULT_SUBJECT_PREFIX = "stapel.fn"

_bridge = None
_bridge_lock = threading.Lock()


def subject_for(name: str) -> str:
    prefix = comm_setting("NATS_SUBJECT_PREFIX", DEFAULT_SUBJECT_PREFIX)
    return f"{prefix}.{name}"


class NatsBridge:
    """Owns one event-loop thread and one NATS connection per process."""

    def __init__(self, url: str):
        self._url = url
        self._nc = None
        self._connect_lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="stapel-nats", daemon=True
        )
        self._thread.start()

    def _run(self, coro, timeout: float):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout)

    def _connection(self, timeout: float):
        if self._nc is None or self._nc.is_closed:
            with self._connect_lock:
                if self._nc is None or self._nc.is_closed:
                    try:
                        import nats
                    except ImportError as exc:  # pragma: no cover
                        raise FunctionCallError(
                            "nats transport selected but nats-py is not installed "
                            "(pip install 'stapel-core[nats]')"
                        ) from exc
                    self._nc = self._run(
                        nats.connect(
                            self._url,
                            max_reconnect_attempts=-1,
                            reconnect_time_wait=1,
                        ),
                        timeout,
                    )
                    logger.info("stapel-nats connected to %s", self._url)
        return self._nc

    def request(self, subject: str, data: bytes, timeout: float) -> bytes:
        nc = self._connection(timeout)
        # +2s slack so the protocol-level timeout fires first with a
        # precise error, not the bridge future.
        msg = self._run(nc.request(subject, data, timeout=timeout), timeout + 2)
        return msg.data

    def close(self) -> None:
        if self._nc is not None and not self._nc.is_closed:
            try:
                self._run(self._nc.drain(), 5)
            except Exception:  # pragma: no cover
                logger.exception("stapel-nats drain failed")
        self._loop.call_soon_threadsafe(self._loop.stop)


def get_bridge() -> NatsBridge:
    global _bridge
    if _bridge is None:
        with _bridge_lock:
            if _bridge is None:
                _bridge = NatsBridge(comm_setting("NATS_URL", DEFAULT_NATS_URL))
    return _bridge


def reset_bridge() -> None:
    """Tests / settings-change hook."""
    global _bridge
    with _bridge_lock:
        if _bridge is not None:
            try:
                _bridge.close()
            except Exception:  # pragma: no cover
                pass
        _bridge = None


def nats_function_transport(name: str, payload: dict, *, timeout: float | None = None) -> Any:
    """Client side: call *name* over NATS request-reply."""
    effective_timeout = timeout or comm_setting("FUNCTION_TIMEOUT", 5.0)
    data = json.dumps({"payload": payload}, default=str).encode()

    try:
        raw = get_bridge().request(subject_for(name), data, effective_timeout)
    except FunctionCallError:
        raise
    except Exception as exc:
        # nats-py NoRespondersError means nothing subscribes to the subject
        # — the wiring equivalent of HTTP 404.
        if type(exc).__name__ == "NoRespondersError":
            raise FunctionNotRegistered(
                f"no service is serving function '{name}' "
                f"(subject {subject_for(name)!r}); is its serve_functions worker up?"
            ) from exc
        raise FunctionCallError(f"function '{name}' failed over NATS: {exc!r}") from exc

    reply = json.loads(raw.decode() or "{}")
    if isinstance(reply, dict) and reply.get("error"):
        raise FunctionCallError(f"function '{name}' failed remotely: {reply['error']}")
    return reply.get("result") if isinstance(reply, dict) else reply
