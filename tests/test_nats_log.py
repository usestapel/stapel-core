"""Levels of the nats-py connection callbacks and of nats-py's own logger."""
from __future__ import annotations

import asyncio
import logging
import socket
import sys
import types

import pytest

nats = pytest.importorskip("nats")
from nats import errors  # noqa: E402

from stapel_core.net.nats_log import (  # noqa: E402
    NATS_CLIENT_LOGGER,
    ConnectionLog,
    TransientErrorFilter,
    install_client_log_filter,
    is_transient,
)


_OURS = ("stapel_core.nats", NATS_CLIENT_LOGGER)


def _records(caplog, level=logging.DEBUG):
    return [r for r in caplog.records if r.levelno >= level and r.name in _OURS]


def _levels(caplog):
    return [r.levelname for r in _records(caplog)]


@pytest.fixture
def caplog_all(caplog):
    caplog.set_level(logging.DEBUG)
    return caplog


@pytest.mark.parametrize(
    "exc",
    [
        errors.UnexpectedEOF(),
        errors.StaleConnectionError(),
        errors.ConnectionClosedError(),
        socket.gaierror(-2, "Name or service not known"),
        ConnectionRefusedError(111, "Connection refused"),
        errors.TimeoutError(),
        asyncio.TimeoutError(),
    ],
    ids=lambda e: type(e).__name__,
)
def test_transient_error_is_one_warning_without_traceback(caplog_all, exc):
    log = ConnectionLog("test")
    asyncio.run(log.error_cb(exc))
    errors_logged = _records(caplog_all, logging.ERROR)
    assert errors_logged == [], [r.getMessage() for r in errors_logged]
    warnings = [r for r in _records(caplog_all) if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].exc_info is None
    assert type(exc).__name__ in warnings[0].getMessage()


def test_repeated_reconnect_errors_warn_once_per_outage(caplog_all):
    log = ConnectionLog("test")

    async def outage():
        await log.error_cb(errors.UnexpectedEOF())
        await log.disconnected_cb()
        for _ in range(5):
            await log.error_cb(socket.gaierror(-2, "Name or service not known"))
        await log.reconnected_cb()
        await log.error_cb(errors.UnexpectedEOF())

    asyncio.run(outage())
    levels = [(r.levelname, r.getMessage()) for r in _records(caplog_all)]
    assert [lv for lv, _ in levels].count("WARNING") == 3, levels  # error, disconnect, next outage
    assert [lv for lv, _ in levels].count("DEBUG") == 5, levels
    assert [lv for lv, _ in levels].count("INFO") == 1, levels
    assert not _records(caplog_all, logging.ERROR)


@pytest.mark.parametrize(
    "exc",
    [errors.SlowConsumerError(subject="s", reply="", sid=1, sub=None), ValueError("boom"),
     errors.DrainTimeoutError(), errors.AuthorizationError()],
    ids=lambda e: type(e).__name__,
)
def test_other_errors_stay_error(caplog_all, exc):
    asyncio.run(ConnectionLog("test").error_cb(exc))
    logged = _records(caplog_all, logging.ERROR)
    assert len(logged) == 1
    assert logged[0].exc_info is not None


def test_unrequested_close_is_error_requested_close_is_info(caplog_all):
    lost = ConnectionLog("lost")
    asyncio.run(lost.closed_cb())
    assert _levels(caplog_all) == ["ERROR"]

    caplog_all.clear()
    ours = ConnectionLog("ours")
    ours.mark_closing()

    async def close():
        await ours.disconnected_cb()
        await ours.closed_cb()

    asyncio.run(close())
    assert _levels(caplog_all) == ["INFO", "INFO"]


def test_is_transient_none_is_false():
    assert is_transient(None) is False


def test_nats_client_logger_downgrades_transient_classes(caplog_all):
    install_client_log_filter()
    client_logger = logging.getLogger(NATS_CLIENT_LOGGER)
    client_logger.error("nats: encountered error", exc_info=errors.UnexpectedEOF())
    client_logger.error("nats: encountered error", exc_info=socket.gaierror(-2, "x"))
    client_logger.error("nats: encountered error", exc_info=ValueError("real"))
    levels = [(r.levelname, r.exc_info is None) for r in _records(caplog_all)]
    assert levels == [("WARNING", True), ("WARNING", True), ("ERROR", False)]
    assert "UnexpectedEOF" in _records(caplog_all)[0].getMessage()


def test_nats_default_error_callback_is_downgraded(caplog_all):
    """nats-py's own fallback callback logs at ERROR; the filter catches it."""
    from nats.aio import client as nats_client

    install_client_log_filter()
    asyncio.run(nats_client._default_error_callback(errors.UnexpectedEOF()))
    assert _levels(caplog_all) == ["WARNING"]


def test_filter_installs_once():
    install_client_log_filter()
    install_client_log_filter()
    ConnectionLog("a")
    filters = logging.getLogger(NATS_CLIENT_LOGGER).filters
    assert sum(isinstance(f, TransientErrorFilter) for f in filters) == 1


def test_real_client_refused_connect_logs_no_error(caplog_all):
    """A real nats-py client against a closed port: warnings only."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    log = ConnectionLog("test")

    async def attempt():
        # nats-py retries a first connect while attempts remain; one attempt
        # per server is what makes it give up with NoServersError.
        with pytest.raises(errors.NoServersError):
            await asyncio.wait_for(
                nats.connect(
                    f"nats://127.0.0.1:{port}",
                    connect_timeout=1,
                    reconnect_time_wait=0.1,
                    max_reconnect_attempts=1,
                    **log.callbacks(),
                ),
                10,
            )

    asyncio.run(attempt())
    assert not _records(caplog_all, logging.ERROR), [
        (r.name, r.getMessage()) for r in _records(caplog_all, logging.ERROR)
    ]
    assert any(r.levelno == logging.WARNING for r in _records(caplog_all))


def test_comm_bridge_passes_all_callbacks(monkeypatch):
    from stapel_core.comm.nats import NatsBridge

    seen = {}

    class _NC:
        is_closed = False

        async def drain(self):
            pass

    async def connect(url, **kwargs):
        seen.update(kwargs)
        return _NC()

    fake = types.ModuleType("nats")
    fake.connect = connect
    monkeypatch.setitem(sys.modules, "nats", fake)

    bridge = NatsBridge("nats://test:4222")
    try:
        bridge.max_payload(2.0)
    finally:
        bridge.close()
    assert seen["max_reconnect_attempts"] == -1
    for name in ("error_cb", "disconnected_cb", "reconnected_cb", "closed_cb"):
        assert callable(seen[name]), name


def test_bridge_timeout_cancels_the_coroutine():
    """A timed-out call must not leave its coroutine running on the loop."""
    from stapel_core.comm.nats import NatsBridge

    bridge = NatsBridge("nats://never:4222")
    cancelled = []

    async def forever():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    try:
        with pytest.raises(TimeoutError):
            bridge._run(forever(), 0.2)
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0.05), bridge._loop).result(2)
    finally:
        bridge.close()
    assert cancelled == [True]
