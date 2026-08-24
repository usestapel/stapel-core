"""``TraceContextMiddleware`` — where a trace starts.

Add it as high in ``MIDDLEWARE`` as possible (right after security/CORS,
before anything that logs), and every log line, metric, Action and Function
of the request carries the same ids::

    MIDDLEWARE = [
        "django.middleware.security.SecurityMiddleware",
        "stapel_core.observability.middleware.TraceContextMiddleware",
        ...
    ]

What it does per request:

1. Reads the caller's ``traceparent`` (W3C) or ``X-Trace-Id`` /
   ``X-Request-ID`` / ``X-Correlation-Id`` headers when
   ``TRUST_INCOMING_TRACE`` allows it — this is what makes ONE trace span
   several services — and sanitizes whatever it finds.
2. Binds a :class:`~stapel_core.observability.context.TraceContext` for the
   duration of the request, so ``stapel_core.comm.emit`` stamps the ids into
   every envelope it produces and every log record carries them.
3. Puts ``request.trace_id`` / ``request.request_id`` /
   ``request.correlation_id`` on the request, for view code and DRF
   exception handlers.
4. Echoes the ids on the response, so a client (and the access log) can
   quote the id of the operation it just triggered.
5. Records ``<ns>http_requests_total`` and
   ``<ns>http_request_duration_seconds``, labelled by method/status/route.

The route label is the URL **pattern**, never the resolved path: a label
whose cardinality is "one per object id" is how a metrics backend is taken
down by its own instrumentation.
"""
from __future__ import annotations

import time

from . import metrics
from .context import start_trace

__all__ = ["TraceContextMiddleware"]


class TraceContextMiddleware:
    """Bind a trace context around every request; measure and echo it."""

    def __init__(self, get_response):
        self.get_response = get_response

    # ── header plumbing ────────────────────────────────────────────────
    @staticmethod
    def _header(request, name: str) -> str:
        return request.META.get(
            "HTTP_" + name.upper().replace("-", "_"), ""
        )

    def _incoming(self, request) -> dict:
        from .conf import observability_settings as s

        if not s.TRUST_INCOMING_TRACE:
            # Still honour a request id: it is the client's own handle on the
            # call and carries no claim about a distributed trace.
            return {"request_id": self._header(request, s.REQUEST_ID_HEADER)}
        return {
            "traceparent": self._header(request, "traceparent"),
            "trace_id": self._header(request, s.TRACE_ID_HEADER),
            "request_id": self._header(request, s.REQUEST_ID_HEADER),
            "correlation_id": self._header(request, s.CORRELATION_ID_HEADER),
        }

    @staticmethod
    def _route(request) -> str:
        """The URL pattern for this request — bounded-cardinality label."""
        match = getattr(request, "resolver_match", None)
        if match is None:
            return "unmatched"
        return match.route or match.view_name or "unmatched"

    # ── the request ────────────────────────────────────────────────────
    def __call__(self, request):
        from .conf import observability_settings as s

        incoming = self._incoming(request)
        started = time.perf_counter()

        with start_trace(**incoming) as ctx:
            request.trace_id = ctx.trace_id
            request.span_id = ctx.span_id
            # request_id defaults to the span: a request always has a handle,
            # even when the caller supplied none.
            request.request_id = ctx.request_id or ctx.span_id
            request.correlation_id = ctx.correlation_id

            response = self.get_response(request)

            if s.ECHO_TRACE_HEADERS:
                response[s.TRACE_ID_HEADER] = ctx.trace_id
                response[s.REQUEST_ID_HEADER] = request.request_id
                response[s.CORRELATION_ID_HEADER] = ctx.correlation_id

            if s.REQUEST_METRICS:
                labels = {
                    "method": request.method,
                    "status": str(getattr(response, "status_code", 0)),
                    "route": self._route(request),
                }
                metrics.counter(
                    "http_requests_total",
                    labels=labels,
                    description="HTTP requests handled",
                )
                metrics.histogram(
                    "http_request_duration_seconds",
                    time.perf_counter() - started,
                    labels=labels,
                    description="HTTP request duration in seconds",
                )

            return response
