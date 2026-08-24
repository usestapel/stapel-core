"""Error-reporting seam — a Sentry-shaped interface, nobody's DSN.

An exception with context is a third signal, next to metrics and logs, and
the fleet's habit until now was ``sentry_sdk.init(...)`` in a settings module
and ``sentry_sdk.capture_exception`` at call sites — a vendor named in
library code, and a hard requirement on the SDK for anyone who installs the
library.

So: call sites call :func:`report_error`, deployments name the reporter in
``STAPEL_OBSERVABILITY["ERROR_REPORTER"]``, and the default reports nowhere.
The default matters — shipping a framework that sends exceptions (with their
context) to a third party unless you opt OUT would be a decision the
framework has no standing to make.

The interface is Sentry-shaped on purpose: ``capture_exception`` /
``capture_message`` / ``level`` / ``tags`` / ``context`` map one-to-one onto
sentry-sdk, GlitchTip, Rollbar and Bugsnag, so a house reporter is a thin
adapter rather than a translation layer.

Every reporter also receives the in-flight trace ids as tags, which is the
join that makes an error page useful: the exception in Sentry and the log
lines in the aggregator carry the same ``trace_id``.
"""
from __future__ import annotations

import logging
import sys
import threading
from typing import Mapping

from .context import trace_ids

logger = logging.getLogger(__name__)

__all__ = [
    "ErrorReporter",
    "NoopErrorReporter",
    "LoggingErrorReporter",
    "SentryErrorReporter",
    "report_error",
    "report_message",
    "get_error_reporter",
    "set_error_reporter",
    "reset_error_reporter",
]


class ErrorReporter:
    """The interface :func:`report_error` speaks.

    Implementations must not raise: a reporting failure during error
    handling would replace a diagnosable exception with an undiagnosable one.
    Both methods return the backend's event id when it has one, else ``None``.
    """

    #: False when the reporter is a placeholder that discards everything.
    #: Surfaced by check ``stapel_core.observability.W003``.
    active = True

    def capture_exception(
        self,
        exc: BaseException | None = None,
        *,
        context: Mapping | None = None,
        tags: Mapping | None = None,
        level: str = "error",
    ) -> str | None:
        """Report *exc* (default: the exception being handled)."""
        return None

    def capture_message(
        self,
        message: str,
        *,
        context: Mapping | None = None,
        tags: Mapping | None = None,
        level: str = "error",
    ) -> str | None:
        """Report a message with no exception attached."""
        return None


class NoopErrorReporter(ErrorReporter):
    """The default. Discards everything, at DEBUG.

    Not silent-by-accident: the DEBUG line is how a developer wondering "did
    that get reported?" finds out that no reporter is configured.
    """

    active = False

    def capture_exception(self, exc=None, *, context=None, tags=None, level="error"):
        logger.debug(
            "error reporting is not configured; dropped %r",
            exc or sys.exc_info()[1],
        )
        return None

    def capture_message(self, message, *, context=None, tags=None, level="error"):
        logger.debug("error reporting is not configured; dropped %r", message)
        return None


class LoggingErrorReporter(ErrorReporter):
    """Reports into the logging stack, with the context as structured fields.

    Under :class:`~stapel_core.observability.logs.JsonFormatter` this is a
    real destination, not a stub: the exception, its context and the trace
    ids land in the aggregator as one queryable record. The sensible choice
    for a deployment that has log storage and no error tracker.
    """

    def __init__(self, logger_name: str = "stapel.errors") -> None:
        self._logger = logging.getLogger(logger_name)

    def _extra(self, context, tags):
        extra = dict(trace_ids())
        if context:
            extra.update({str(k): v for k, v in context.items()})
        if tags:
            extra["tags"] = {str(k): str(v) for k, v in tags.items()}
        return extra

    def capture_exception(self, exc=None, *, context=None, tags=None, level="error"):
        exc = exc or sys.exc_info()[1]
        self._logger.log(
            logging.getLevelName(level.upper()) if isinstance(level, str) else level,
            "%s: %s",
            type(exc).__name__ if exc else "error",
            exc,
            exc_info=exc if exc is not None else True,
            extra=self._extra(context, tags),
        )
        return None

    def capture_message(self, message, *, context=None, tags=None, level="error"):
        self._logger.log(
            logging.getLevelName(level.upper()) if isinstance(level, str) else level,
            "%s",
            message,
            extra=self._extra(context, tags),
        )
        return None


class SentryErrorReporter(ErrorReporter):
    """``sentry-sdk`` adapter. Requires ``pip install 'stapel-core[sentry]'``.

    Assumes ``sentry_sdk.init()`` has already run — this class reports, it
    does not configure a DSN (``stapel_core.django.settings.setup_sentry``
    does that, from the environment). Without the SDK installed, or without
    an init, it degrades to inactive rather than raising: an error path is
    the worst possible place to discover a missing dependency.
    """

    def __init__(self) -> None:
        try:
            import sentry_sdk
        except ImportError:
            self._sdk = None
            logger.warning(
                "stapel_core.observability: ERROR_REPORTER is SentryErrorReporter "
                "but sentry-sdk is not installed — errors are reported nowhere. "
                "pip install 'stapel-core[sentry]'"
            )
        else:
            self._sdk = sentry_sdk

    @property
    def active(self) -> bool:  # type: ignore[override]
        return self._sdk is not None

    def _scope(self, context, tags):
        merged_tags = dict(trace_ids())
        if tags:
            merged_tags.update({str(k): str(v) for k, v in tags.items()})
        return merged_tags, dict(context or {})

    def _with_scope(self, tags, context, action):
        sdk = self._sdk
        try:
            # push_scope is the API across sentry-sdk 1.x and 2.x; the newer
            # new_scope() is preferred where present.
            scope_cm = getattr(sdk, "new_scope", None) or sdk.push_scope
            with scope_cm() as scope:
                for key, value in tags.items():
                    if value:
                        scope.set_tag(key, value)
                if context:
                    scope.set_context("stapel", context)
                return action()
        except Exception:
            logger.warning(
                "stapel_core.observability: Sentry reporting failed",
                exc_info=True,
            )
            return None

    def capture_exception(self, exc=None, *, context=None, tags=None, level="error"):
        if self._sdk is None:
            return None
        tags, context = self._scope(context, tags)
        exc = exc or sys.exc_info()[1]
        return self._with_scope(
            tags, context, lambda: self._sdk.capture_exception(exc)
        )

    def capture_message(self, message, *, context=None, tags=None, level="error"):
        if self._sdk is None:
            return None
        tags, context = self._scope(context, tags)
        return self._with_scope(
            tags, context, lambda: self._sdk.capture_message(message, level=level)
        )


# ── the facade ──────────────────────────────────────────────────────────

_lock = threading.Lock()
_reporter: ErrorReporter | None = None
_override: ErrorReporter | None = None
_signal_connected = False


def _connect_reset_signal() -> None:
    global _signal_connected
    if _signal_connected:
        return
    try:
        from django.test.signals import setting_changed
    except Exception:  # pragma: no cover
        _signal_connected = True
        return
    setting_changed.connect(_on_setting_changed, weak=False)
    _signal_connected = True


def _on_setting_changed(*, setting=None, **kwargs):
    if setting in (None, "STAPEL_OBSERVABILITY", "ERROR_REPORTER"):
        reset_error_reporter()


def get_error_reporter() -> ErrorReporter:
    """The configured reporter, built once per process.

    Anything that cannot be imported or is not an :class:`ErrorReporter`
    degrades to :class:`NoopErrorReporter` with one log line; check
    ``stapel_core.observability.W003`` reports it at deploy time.
    """
    global _reporter
    if _override is not None:
        return _override
    if _reporter is not None:
        return _reporter
    with _lock:
        if _reporter is None:
            _reporter = _build_reporter()
            _connect_reset_signal()
    return _reporter


def _build_reporter() -> ErrorReporter:
    try:
        from .conf import observability_settings

        value = observability_settings.ERROR_REPORTER
    except Exception as exc:
        logger.warning(
            "stapel_core.observability: ERROR_REPORTER could not be resolved "
            "(%s); errors are reported nowhere.",
            exc,
        )
        return NoopErrorReporter()

    if isinstance(value, ErrorReporter):
        return value
    if isinstance(value, type):
        try:
            instance = value()
        except Exception as exc:
            logger.warning(
                "stapel_core.observability: ERROR_REPORTER %r could not be "
                "instantiated (%s); errors are reported nowhere.",
                value,
                exc,
            )
            return NoopErrorReporter()
        if isinstance(instance, ErrorReporter):
            return instance
    logger.warning(
        "stapel_core.observability: ERROR_REPORTER %r is not an ErrorReporter; "
        "errors are reported nowhere.",
        value,
    )
    return NoopErrorReporter()


def set_error_reporter(reporter: ErrorReporter | None) -> None:
    """Pin *reporter* for this process, ignoring settings. ``None`` unpins."""
    global _override
    _override = reporter


def reset_error_reporter() -> None:
    """Forget the memoized reporter; the next call rebuilds it."""
    global _reporter
    with _lock:
        _reporter = None


def report_error(
    exc: BaseException | None = None,
    *,
    context: Mapping | None = None,
    tags: Mapping | None = None,
    level: str = "error",
) -> str | None:
    """Report *exc* (default: the exception being handled) through the seam.

    Never raises — including when the reporter itself fails. Reporting an
    error must not become a second error.

    The "exception being handled" default is resolved HERE rather than left
    to each reporter: a house reporter that forgets it would silently report
    ``None``, and the failure would only show up as an empty issue in an
    error tracker nobody reads twice.
    """
    try:
        return get_error_reporter().capture_exception(
            exc if exc is not None else sys.exc_info()[1],
            context=context,
            tags=tags,
            level=level,
        )
    except Exception:
        logger.warning("error reporter failed", exc_info=True)
        return None


def report_message(
    message: str,
    *,
    context: Mapping | None = None,
    tags: Mapping | None = None,
    level: str = "error",
) -> str | None:
    """Report a message with no exception attached. Never raises."""
    try:
        return get_error_reporter().capture_message(
            message, context=context, tags=tags, level=level
        )
    except Exception:
        logger.warning("error reporter failed", exc_info=True)
        return None
