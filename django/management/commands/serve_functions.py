"""NATS Function server: expose this service's registered functions.

Runs as a worker process next to the web/celery containers:

    python manage.py serve_functions

Every function registered via @function/register_function gets a NATS
subscription on ``<prefix>.<name>`` with queue group = service name, so
multiple replicas of the same service load-balance automatically.

Handlers execute in a thread pool (Django ORM is synchronous);
close_old_connections() guards against stale DB connections per call.
"""
from __future__ import annotations

import asyncio
import json
import logging

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Serve this service's registered comm Functions over NATS request-reply."

    def handle(self, *args, **options):
        from stapel_core.comm.config import comm_setting
        from stapel_core.comm.registry import function_registry

        names = function_registry.names()
        if not names:
            self.stdout.write("no functions registered — nothing to serve")
            return
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
            from django.db import close_old_connections

            close_old_connections()
            try:
                function_registry.validate(name, payload)
                result = function_registry.get(name)(payload)
                return json.dumps({"result": result}, default=str).encode()
            except Exception as exc:
                logger.exception("function %s failed", name)
                return json.dumps({"error": repr(exc)}).encode()
            finally:
                close_old_connections()

        async def _handler(msg):
            name = msg.subject.rsplit(".", 1)[-1] if "." in msg.subject else msg.subject
            # Recover the full function name from the subject prefix
            prefix = subject_for("")
            full_name = msg.subject[len(prefix):] if msg.subject.startswith(prefix) else name
            try:
                body = json.loads(msg.data.decode() or "{}")
                payload = body.get("payload") or {}
            except Exception:
                await msg.respond(json.dumps({"error": "invalid request body"}).encode())
                return
            reply = await loop.run_in_executor(None, _execute, full_name, payload)
            await msg.respond(reply)

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
