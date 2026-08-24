"""Structured logging — one JSON object per event, correlated by trace id.

A text log is grepped; a structured log is queried. That difference is the
whole reason this module exists: "gigabytes in the aggregator" is not a
volume problem so much as a shape problem — with ``{trace_id, service,
level, …}`` on every record, an operator follows ONE id through N services
instead of reading everything a time window contains.

Mandatory field set on every record (the contract other tooling may rely on)::

    {"ts", "level", "service", "logger", "msg",
     "trace_id", "span_id", "correlation_id", "causation_id", "request_id"}

plus ``exc_type``/``exc_message``/``stack`` when an exception is being
logged, plus whatever the call site passed as ``extra=`` — minus the fields
named in ``STAPEL_OBSERVABILITY["REDACT_FIELDS"]``, whose values are replaced
with ``"***"``. A structured logger takes whatever ``extra=`` hands it, so
the redaction is here, at the formatter, where nothing can route around it.

Wiring, once, in a settings module::

    from stapel_core.observability import logging_config
    LOGGING = logging_config(service="chat")

or imperatively (management commands, workers, tests)::

    from stapel_core.observability import configure_logging
    configure_logging(service="chat")
"""
from __future__ import annotations

import datetime as _datetime
import json
import logging
import traceback
from typing import Any

from .context import trace_ids

__all__ = [
    "JsonFormatter",
    "TraceContextFilter",
    "logging_config",
    "configure_logging",
    "TEXT_FORMAT",
]

#: Non-JSON format that still carries correlation — for a developer terminal.
TEXT_FORMAT = (
    "%(asctime)s %(levelname)-8s %(name)s [trace=%(trace_id)s] %(message)s"
)

# LogRecord attributes that are plumbing, not payload. Anything else on a
# record came from the call site's `extra=` and belongs in the JSON object.
_RESERVED = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName",
        "taskName", "trace_id", "span_id", "correlation_id", "causation_id",
        "request_id", "service",
    }
)

_TRACE_KEYS = (
    "trace_id", "span_id", "correlation_id", "causation_id", "request_id",
)


class TraceContextFilter(logging.Filter):
    """Stamp the in-flight trace ids onto every record passing through.

    A filter rather than formatter-only work, so a host that keeps its own
    text formatter can still write ``%(trace_id)s`` — correlation should not
    be something you only get by adopting our formatter too.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        ids = trace_ids()
        for key in _TRACE_KEYS:
            if not getattr(record, key, ""):
                setattr(record, key, ids[key])
        return True


def _redact_fields() -> frozenset:
    try:
        from .conf import observability_settings

        return frozenset(
            str(f).lower() for f in (observability_settings.REDACT_FIELDS or ())
        )
    except Exception:
        return frozenset()


def _jsonable(value: Any) -> Any:
    """Best-effort conversion of an arbitrary extra value.

    A log line must not be lost because something in it did not serialize —
    an unserializable value becomes its ``repr``.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (_datetime.datetime, _datetime.date)):
        return value.isoformat()
    return repr(value)


class JsonFormatter(logging.Formatter):
    """Render a :class:`logging.LogRecord` as one JSON object, one line.

    *service* names the emitting service on every record; when omitted it is
    resolved from ``STAPEL_OBSERVABILITY["SERVICE_NAME"]`` and, failing that,
    from the comm service name — so a log line and the events that request
    emitted agree about who produced them.

    *static_fields* are merged into every record (deployment, region, image
    tag). *include_source* adds ``module``/``func``/``line``, which is worth
    the bytes in a debug build and usually not in production.
    """

    def __init__(
        self,
        service: str | None = None,
        *,
        static_fields: dict | None = None,
        include_source: bool | None = None,
        redact: frozenset | None = None,
    ) -> None:
        super().__init__()
        self._service = service
        self._static = dict(static_fields or {})
        self._include_source = include_source
        self._redact = redact

    # ── resolution of the settings-backed knobs, lazily ────────────────
    def _resolve_service(self) -> str:
        if self._service:
            return self._service
        try:
            from .conf import observability_settings

            configured = observability_settings.SERVICE_NAME
            if configured:
                self._service = str(configured)
                return self._service
        except Exception:
            pass
        try:
            from ..comm.config import service_name

            self._service = str(service_name())
        except Exception:
            self._service = "unknown"
        return self._service

    def _resolve_redact(self) -> frozenset:
        if self._redact is None:
            self._redact = _redact_fields()
        return self._redact

    def _resolve_include_source(self) -> bool:
        if self._include_source is None:
            try:
                from .conf import observability_settings

                self._include_source = bool(
                    observability_settings.LOG_INCLUDE_SOURCE
                )
            except Exception:
                self._include_source = False
        return self._include_source

    # ── formatting ─────────────────────────────────────────────────────
    def format(self, record: logging.LogRecord) -> str:
        ids = trace_ids()
        payload = {
            "ts": _datetime.datetime.fromtimestamp(
                record.created, tz=_datetime.timezone.utc
            ).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "service": getattr(record, "service", None) or self._resolve_service(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in _TRACE_KEYS:
            payload[key] = getattr(record, key, "") or ids[key]

        if self._resolve_include_source():
            payload["module"] = record.module
            payload["func"] = record.funcName
            payload["line"] = record.lineno

        payload.update(self._static)

        if record.exc_info:
            exc_type, exc_value, _tb = record.exc_info
            payload["exc_type"] = getattr(exc_type, "__name__", str(exc_type))
            payload["exc_message"] = str(exc_value)
            payload["stack"] = "".join(
                traceback.format_exception(*record.exc_info)
            ).rstrip()
        elif record.exc_text:
            payload["stack"] = record.exc_text
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)

        redact = self._resolve_redact()
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = "***" if key.lower() in redact else _jsonable(value)

        # A record must never be lost to a serialization failure.
        try:
            return json.dumps(payload, default=repr, ensure_ascii=False)
        except Exception:
            return json.dumps(
                {
                    "ts": payload["ts"],
                    "level": payload["level"],
                    "service": payload["service"],
                    "logger": payload["logger"],
                    "msg": payload["msg"],
                    "trace_id": payload["trace_id"],
                    "log_format_error": True,
                },
                ensure_ascii=False,
            )


def logging_config(
    *,
    service: str | None = None,
    level: str | None = None,
    log_format: str | None = None,
    static_fields: dict | None = None,
    include_source: bool | None = None,
    loggers: dict | None = None,
    stream: str = "ext://sys.stdout",
) -> dict:
    """A ``LOGGING`` dict: one stdout handler, JSON formatted, trace-stamped.

    Everything is a keyword with a settings-backed default, so the common
    case is ``LOGGING = logging_config(service="chat")``.

    stdout, not a file: a container's logs belong to whatever collects the
    container's output. *loggers* is merged over the generated per-logger
    section for hosts that want a different level for a noisy third party.
    """
    from .conf import observability_settings as s

    level = (level or s.LOG_LEVEL or "INFO").upper()
    log_format = (log_format or s.LOG_FORMAT or "json").lower()
    formatter = "json" if log_format == "json" else "text"

    config = {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "trace_context": {
                "()": "stapel_core.observability.logs.TraceContextFilter",
            },
        },
        "formatters": {
            "json": {
                "()": "stapel_core.observability.logs.JsonFormatter",
                "service": service or s.SERVICE_NAME,
                "static_fields": dict(static_fields or s.LOG_STATIC_FIELDS or {}),
                "include_source": (
                    s.LOG_INCLUDE_SOURCE if include_source is None
                    else include_source
                ),
            },
            "text": {"format": TEXT_FORMAT},
        },
        "handlers": {
            "stapel": {
                "class": "logging.StreamHandler",
                "stream": stream,
                "formatter": formatter,
                "filters": ["trace_context"],
                "level": level,
            },
        },
        "root": {"handlers": ["stapel"], "level": level},
        "loggers": {
            # Request noise is the access log's job; Django's own handler for
            # it duplicates every line the gateway already records.
            "django.server": {"level": "WARNING", "propagate": True},
        },
    }
    for name in s.LOG_EXEMPT_LOGGERS or ():
        config["loggers"][str(name)] = {"propagate": False}
    if loggers:
        config["loggers"].update(loggers)
    return config


def configure_logging(**kwargs) -> dict:
    """Apply :func:`logging_config` right now. Returns the config used.

    For processes that do not go through Django settings — management
    commands, bus consumers, workers, a test that wants to see the real
    output shape.
    """
    import logging.config as _logging_config

    config = logging_config(**kwargs)
    _logging_config.dictConfig(config)
    return config
