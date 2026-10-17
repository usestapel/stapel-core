"""Log levels for a nats-py connection's lifecycle.

A broker restart, or a name lookup that fails while the broker is coming
back, is a disconnect nats-py recovers from by itself: WARNING, once per
outage, no traceback. A connection that closes while nobody asked it to is
gone for good: ERROR. Anything else nats-py reports keeps ERROR.

    conn_log = ConnectionLog("functions client")
    nc = await nats.connect(url, max_reconnect_attempts=-1, **conn_log.callbacks())
    ...
    conn_log.mark_closing()
    await nc.drain()

Without an ``error_cb`` nats-py logs every one of these as
``ERROR nats: encountered error`` with a traceback; its read loop does the
same for exceptions it does not expect. ``install_client_log_filter`` covers
that second path by downgrading transient classes on the nats-py logger.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("stapel_core.nats")

NATS_CLIENT_LOGGER = "nats.aio.client"


def _classes() -> tuple[tuple[type, ...], tuple[type, ...]]:
    transient: list[type] = [OSError, asyncio.TimeoutError, EOFError]
    fatal: list[type] = []
    try:
        from nats import errors
    except ImportError:  # pragma: no cover - nats-py is an optional extra
        return tuple(transient), ()
    transient += [
        errors.StaleConnectionError,  # includes UnexpectedEOF
        errors.ConnectionClosedError,
        errors.ConnectionReconnectingError,
        errors.TimeoutError,
    ]
    # A drain that times out loses in-flight messages; that is not a blip.
    fatal.append(errors.DrainTimeoutError)
    return tuple(transient), tuple(fatal)


def is_transient(exc: BaseException | None) -> bool:
    """True for connection errors nats-py reconnects through by itself."""
    if exc is None:
        return False
    transient, fatal = _classes()
    return isinstance(exc, transient) and not isinstance(exc, fatal)


def _describe(exc: BaseException) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


class TransientErrorFilter(logging.Filter):
    """Downgrades nats-py's own ERROR records for transient errors."""

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        if record.levelno >= logging.ERROR and is_transient(exc):
            record.msg = f"{record.getMessage()}: {_describe(exc)}"
            record.args = ()
            record.levelno = logging.WARNING
            record.levelname = logging.getLevelName(logging.WARNING)
            record.exc_info = None
            record.exc_text = None
        return True


def install_client_log_filter() -> None:
    """Idempotent: attach ``TransientErrorFilter`` to the nats-py logger."""
    client_logger = logging.getLogger(NATS_CLIENT_LOGGER)
    if not any(isinstance(f, TransientErrorFilter) for f in client_logger.filters):
        client_logger.addFilter(TransientErrorFilter())


class ConnectionLog:
    """The four nats-py callbacks for one connection, at deliberate levels."""

    def __init__(self, label: str) -> None:
        self.label = label
        self._closing = False
        self._in_outage = False
        install_client_log_filter()

    def mark_closing(self) -> None:
        """Call before ``close()``/``drain()``: the close that follows is ours."""
        self._closing = True

    def callbacks(self) -> dict:
        return {
            "error_cb": self.error_cb,
            "disconnected_cb": self.disconnected_cb,
            "reconnected_cb": self.reconnected_cb,
            "closed_cb": self.closed_cb,
        }

    async def error_cb(self, exc: Exception) -> None:
        if not is_transient(exc):
            logger.error("nats %s: %s", self.label, _describe(exc), exc_info=exc)
            return
        if self._in_outage or self._closing:
            # Every reconnect attempt reports again; the first one said it.
            logger.debug("nats %s: still reconnecting: %s", self.label, _describe(exc))
            return
        self._in_outage = True
        logger.warning(
            "nats %s: connection error, reconnecting: %s", self.label, _describe(exc)
        )

    async def disconnected_cb(self) -> None:
        if self._closing:
            logger.info("nats %s: disconnected on close", self.label)
            return
        logger.warning("nats %s: disconnected, reconnecting", self.label)

    async def reconnected_cb(self) -> None:
        self._in_outage = False
        logger.info("nats %s: reconnected", self.label)

    async def closed_cb(self) -> None:
        if self._closing:
            logger.info("nats %s: connection closed", self.label)
            return
        logger.error(
            "nats %s: connection closed for good, no further reconnects", self.label
        )
