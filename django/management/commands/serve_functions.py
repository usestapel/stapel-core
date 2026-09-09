"""NATS Function server: expose this service's registered functions.

Runs as a worker process next to the web/celery containers:

    python manage.py serve_functions

Every function registered via @function/register_function gets a NATS
subscription on ``<prefix>.<name>`` with queue group = service name, so
multiple replicas of the same service load-balance automatically.

Handlers execute in a thread pool (Django ORM is synchronous);
close_stale_connections() guards against stale DB connections per call.
"""
from __future__ import annotations

import asyncio
import json
import logging

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)

#: Bytes a function's serialized reply came to, per function.
REPLY_BYTES_METRIC = "comm_function_reply_bytes"
#: Replies the broker refused. The number that should be alerted on: every
#: one of them is work this system did and then threw away.
REPLY_TOO_LARGE_METRIC = "comm_function_reply_too_large_total"

#: Powers of two from 1 KiB to 64 MiB. Sizes here span six orders of
#: magnitude — a boolean answer and a meeting transcript — and the default
#: duration buckets say nothing useful about either.
REPLY_SIZE_BUCKETS = tuple(float(1024 * 2 ** i) for i in range(17))


def _observe_reply_size(name: str, size: int, max_payload: int) -> None:
    """Record one reply's size. Never raises — this is a reply path."""
    try:
        from stapel_core.observability import metrics

        metrics.histogram(
            REPLY_BYTES_METRIC, float(size), {"function": name},
            description="Serialized size of a comm Function reply, in bytes.",
            buckets=REPLY_SIZE_BUCKETS,
        )
        if max_payload and size > max_payload:
            metrics.counter(
                REPLY_TOO_LARGE_METRIC, 1.0, {"function": name},
                description=(
                    "Comm Function replies the broker refused as oversized. "
                    "Each one is completed work that was thrown away."
                ),
            )
    except Exception:  # pragma: no cover — the facade already guards itself
        logger.debug("comm: reply size not recorded for %s", name, exc_info=True)


def fit_reply(data: bytes, max_payload: int, name: str) -> bytes:
    """*data*, or a small marker explaining why the real answer cannot be sent.

    Split out of the handler so it is testable: this is the half that actually
    broke on a client stand, and a closure inside an asyncio callback is not
    something a test can reach.

    ``max_payload`` of 0 means "the broker announced no limit" — pass the data
    through rather than inventing a cap of our own.

    EVERY reply is measured, not only the ones that fail. The failure this
    guards was invisible until it took a client's recording (2026-09-09:
    llm.transcribe over a 2h28m meeting, 8 647 617 bytes against 8 MiB — it
    missed by 3%, so the seam had been one long meeting away from breaking
    for months with nothing to show it). A histogram of reply sizes is what
    turns "this function is approaching the wire limit" into something a
    dashboard can say BEFORE a user loses work, and the fleet has other
    Functions whose answers grow with their input the same way.
    """
    _observe_reply_size(name, len(data), max_payload)
    if not max_payload or len(data) <= max_payload:
        return data
    logger.error(
        "function %s: reply is %d bytes, over the broker's max_payload of %d "
        "— sending a too-large marker instead. A function is a "
        "request/response seam, not a file transfer: return a reference "
        "(object key / URL) the caller resolves, or raise the broker's "
        "max_payload if this size is genuinely expected.",
        name, len(data), max_payload,
    )
    return json.dumps({
        "error": (
            f"reply of {len(data)} bytes exceeds the transport limit "
            f"of {max_payload} bytes"
        ),
        "error_code": "payload_too_large",
        "size": len(data),
        "limit": max_payload,
    }).encode()


class Command(BaseCommand):
    help = "Serve this service's registered comm Functions over NATS request-reply."

    #: See BaseBusConsumerCommand — read by check
    #: stapel_core.observability.W005.
    stapel_serves_metrics = True

    def handle(self, *args, **options):
        from stapel_core.comm.config import comm_setting
        from stapel_core.comm.registry import function_registry

        names = function_registry.names()
        if not names:
            self.stdout.write("no functions registered — nothing to serve")
            return
        # This process serves NATS, not HTTP, so nothing can scrape the
        # metrics it records unless it opens a port of its own. Off unless
        # STAPEL_OBSERVABILITY["EXPORTER_PORT"] is set.
        from stapel_core.observability.exporter import serve_metrics

        serve_metrics()
        url = comm_setting("NATS_URL", "nats://nats:4222")
        self.stdout.write(f"serving {len(names)} function(s) on {url}: {', '.join(names)}")
        asyncio.run(self._serve(url, names))

    async def _serve(self, url: str, names: list[str]):
        import nats

        from stapel_core.comm.config import service_name
        from stapel_core.comm.nats import subject_for
        from stapel_core.comm.registry import function_registry

        nc = await nats.connect(url, max_reconnect_attempts=-1, reconnect_time_wait=1)
        queue = service_name() or "stapel"
        loop = asyncio.get_running_loop()

        def _execute(name: str, payload: dict) -> bytes:
            # Probing, not just ageing out: this server is idle between calls
            # for hours, which is exactly when a database drops a connection
            # without telling anyone (see stapel_core.django.db).
            from stapel_core.django.db import close_stale_connections

            close_stale_connections()
            try:
                function_registry.validate(name, payload)
                result = function_registry.get(name)(payload)
                return json.dumps({"result": result}, default=str).encode()
            except Exception as exc:
                logger.exception("function %s failed", name)
                return json.dumps({"error": repr(exc)}).encode()
            finally:
                close_stale_connections()

        # The broker's per-message cap, as this server announced it on connect.
        max_payload = int(getattr(nc, "max_payload", 0) or 0)

        async def _reply(msg, data: bytes, full_name: str) -> None:
            """Answer, or say WHY there is no answer — never nothing.

            The defect this exists for: an oversized reply made
            ``msg.respond()`` raise ``MaxPayloadError`` inside the subscription
            callback. The function had already run; the result was simply
            dropped, nothing reached the caller, and it sat until its timeout
            and reported a generic failure. The only line naming the real cause
            was in THIS process's log, on another host. Measured on a client stand
            (2026-08-06): llm.complete over a meeting transcript, upload path.

            So: check the size ourselves and send a small, structured marker the
            client turns back into FunctionPayloadTooLarge. And never let this
            coroutine raise — an exception here is, again, a caller that hears
            nothing at all.
            """
            data = fit_reply(data, max_payload, full_name)
            try:
                await msg.respond(data)
            except Exception:
                logger.exception(
                    "function %s: could not deliver a reply at all (%d bytes); "
                    "the caller will time out", full_name, len(data),
                )

        async def _handler(msg):
            name = msg.subject.rsplit(".", 1)[-1] if "." in msg.subject else msg.subject
            # Recover the full function name from the subject prefix
            prefix = subject_for("")
            full_name = msg.subject[len(prefix):] if msg.subject.startswith(prefix) else name
            try:
                body = json.loads(msg.data.decode() or "{}")
                payload = body.get("payload") or {}
            except Exception:
                await _reply(
                    msg, json.dumps({"error": "invalid request body"}).encode(), full_name
                )
                return
            reply = await loop.run_in_executor(None, _execute, full_name, payload)
            await _reply(msg, reply, full_name)

        for name in names:
            await nc.subscribe(subject_for(name), queue=queue, cb=_handler)
            logger.info("serving %s (queue=%s)", subject_for(name), queue)

        stop = asyncio.Event()
        try:
            await stop.wait()  # run until SIGTERM/SIGINT cancels us
        except (KeyboardInterrupt, asyncio.CancelledError):  # pragma: no cover
            pass
        finally:
            await nc.drain()
