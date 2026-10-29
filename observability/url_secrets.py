"""Bearer tokens in URL paths never reach a log line.

A share link is a credential carried in the path: ``/share/<token>`` in the
browser, ``…/public/shares/<token>`` on the API, and the page's own requests
carry the first one as their ``Referer``. Every web process logs both — the
WSGI server's access log (request line and referer) and Django's
``Not Found: <path>`` warning — so whoever can read logs can open every
shared document. Redacting at the edge proxy does not reach these lines; they
are written by the process itself.

:func:`install` attaches :class:`UrlSecretFilter` to the loggers that write
request paths. A logger-level filter runs before any handler, so every
handler of that logger (console, file, an error reporter reading the
message) receives the redacted record. The segment after each name in
``STAPEL_OBSERVABILITY["SECRET_PATH_SEGMENTS"]`` is replaced with
``<redacted>``; the rest of the line is unchanged.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

#: What the token is replaced with — the same mark the edge proxy can use,
#: so one grep finds both layers.
REDACTED = "<redacted>"

#: Loggers that write request paths or referers. ``gunicorn.access`` logs the
#: request line, path, query and referer as a dict of atoms;
#: ``django.request`` / ``django.server`` log the path as an argument.
LOGGERS = ("gunicorn.access", "django.request", "django.server", "uvicorn.access")

_compiled: tuple[tuple[str, ...], re.Pattern | None] = ((), None)


def secret_path_segments() -> tuple[str, ...]:
    """The configured segment names, normalised (no slashes, no blanks)."""
    try:
        from .conf import observability_settings

        configured = observability_settings.SECRET_PATH_SEGMENTS or ()
    except Exception:  # settings not configured (a bare script)
        configured = ("share", "shares")
    if isinstance(configured, str):
        configured = [configured]
    return tuple(
        str(name).strip("/ ") for name in configured if str(name).strip("/ ")
    )


def _pattern(segments: tuple[str, ...]) -> re.Pattern | None:
    global _compiled
    if _compiled[0] == segments:
        return _compiled[1]
    pattern = None
    if segments:
        names = "|".join(re.escape(s) for s in segments)
        # The segment after `/<name>/`, up to the next separator of a path,
        # a query, a fragment or the end of a quoted log field.
        pattern = re.compile(r"(/(?:%s)/)[^/?#\s\"']+" % names)
    _compiled = (segments, pattern)
    return pattern


def redact_url_secrets(text, segments: Iterable[str] | None = None):
    """``text`` with the token after every secret segment replaced.

    Anything that is not a ``str`` is returned unchanged.
    """
    if not isinstance(text, str) or "/" not in text:
        return text
    names = tuple(segments) if segments is not None else secret_path_segments()
    pattern = _pattern(names)
    if pattern is None:
        return text
    return pattern.sub(r"\1" + REDACTED, text)


class UrlSecretFilter(logging.Filter):
    """Rewrites a record's message and arguments; never drops a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact_url_secrets(record.msg)
            args = record.args
            if isinstance(args, dict):
                record.args = {k: redact_url_secrets(v) for k, v in args.items()}
            elif isinstance(args, tuple):
                record.args = tuple(redact_url_secrets(v) for v in args)
        except Exception:  # pragma: no cover - a filter must never lose a line
            pass
        return True


def install(loggers: Iterable[str] = LOGGERS) -> list[str]:
    """Attach the filter to *loggers* once each. Returns the names it was
    attached to now (an empty list when every one already had it)."""
    attached = []
    for name in loggers:
        logger = logging.getLogger(name)
        if any(isinstance(f, UrlSecretFilter) for f in logger.filters):
            continue
        logger.addFilter(UrlSecretFilter())
        attached.append(name)
    return attached


__all__ = [
    "LOGGERS",
    "REDACTED",
    "UrlSecretFilter",
    "install",
    "redact_url_secrets",
    "secret_path_segments",
]
