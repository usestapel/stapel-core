"""What a delete actually did — one vocabulary, measured, for the whole package.

Why this is a mechanism and not a helper
----------------------------------------
0.46.0 gave ``stapel_core.verification`` three terminal verbs that report what
they removed instead of returning ``None``, and the audit that shipped with it
found the same shape in six more places. The shape is not "the function
returned nothing". Django's ``cache.delete`` returns ``False``, not ``None`` —
**so it was never the absence of a return value that hid the defect. The
return was a truthful answer about a key that module never writes, which is no
evidence at all about the record the caller meant.** A truthful answer to the
wrong question is worse than silence, because it looks like information.

Its siblings said even less. ``lift_tombstone``, ``unblacklist_user``,
``remove_from_blacklist`` and ``clear_all`` all returned ``True`` for "the call
did not raise" — a value that is identical whether the record was removed,
was never there, or is still readable afterwards. The costliest of them is
``lift_tombstone``: the operator calling it is restoring a wrongly deleted
user, and a ``True`` that lifted nothing leaves that person locked out with no
signal at all.

So the vocabulary moved here, out of :mod:`stapel_core.verification.grants`
where it was born, and every removal verb in the package speaks it. One
vocabulary rather than a second one per module: the point of
:class:`DropOutcome` is that its members are facts that must never be folded
into each other, and two enums with overlapping members would fold them again
at the seam between modules.

The rule
--------
**A delete must measure, not claim.** :func:`measured_drop` reads the key,
deletes it, and reads it BACK, so ``DROPPED`` is an observation. It costs one
extra round-trip on paths that run once per record at most.

**A caller who ignores the return still gets no silence.** ``NOT_FOUND`` logs a
warning and ``STILL_PRESENT`` and ``UNAVAILABLE`` log errors, each naming the
namespace the key was computed under — the first thing to compare with the
writer's when an expected record was not there.

**Where a function genuinely cannot read back a key, it says so** rather than
returning a comforting ``True``. :func:`measured_clear` is the one such verb in
the package (``TokenBlacklist.clear_all`` empties a whole connection and cannot
enumerate what it emptied); it measures what it *can* — whether the clear
reached the namespace this library writes — with a probe, and documents the
limit.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from enum import Enum
from typing import Callable

logger = logging.getLogger(__name__)

#: What to tell the reader of a ``NOT_FOUND`` when the caller has nothing more
#: specific to say. Callers with a named namespace setting pass their own.
DEFAULT_MISS_HINT = (
    "check that whatever wrote the record used this module, the same cache "
    "alias and the same namespace"
)


class DropOutcome(str, Enum):
    """What a drop actually did to the store.

    Four different facts, and none of them may be folded into another — the
    same rule :class:`~stapel_core.verification.codes.CodeOutcome` states for
    reads. Collapsing them is precisely the defect: a delete that removed
    nothing is not a delete that worked, and a store that could not answer has
    not removed anything at all.
    """

    #: A record was there under this key; a read-back confirms it is gone.
    DROPPED = "dropped"
    #: Nothing was stored under this key. Already spent, aged out — or the
    #: writer computed a DIFFERENT key (a different namespace, or a caller
    #: reaching for ``django.core.cache.cache`` instead of this module).
    NOT_FOUND = "not_found"
    #: The delete ran and the record is STILL readable. The store did not obey;
    #: never report this as success.
    STILL_PRESENT = "still_present"
    #: The store could not be reached, so nothing is known about the record.
    #: Not a removal, and not evidence of absence either (0.47.0). The write
    #: paths of ``OneTimeCodeStore`` raise ``StoreUnavailable`` for the same
    #: reason: "we could not ask" is not an answer.
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class DropReport:
    """The verdict of a drop, plus enough context to explain a ``NOT_FOUND``.

    Falsy unless the record was found and is now gone, so the ordinary
    ``assert drop_challenge(cid)`` is a real assertion rather than a truthy
    enum member that passes on every outcome.
    """

    outcome: DropOutcome
    #: What was being dropped: ``"challenge"``, ``"deletion tombstone"``, ...
    what: str
    #: The unprefixed key, as the owning module computes it.
    key: str
    #: The namespace the key was computed under. The first thing to compare
    #: against the writer's when an expected record was ``NOT_FOUND``. Fleet
    #: namespaces are bare (``"stapel_revocation"``); a key that lands in the
    #: service's OWN cache prefix says so (``"service-local:auth"``), because
    #: for those a peer's copy is a different record entirely.
    namespace: str
    #: Why the store could not answer. Only set for ``UNAVAILABLE``.
    error: str | None = None

    def __bool__(self) -> bool:
        return self.outcome is DropOutcome.DROPPED


def service_namespace(alias: str = "default") -> str:
    """Label for a key that lands in THIS service's own cache prefix.

    Django builds the real key as ``f"{KEY_PREFIX}:{VERSION}:{key}"`` from the
    deployment's own ``CACHES`` entry, and every service in a split deployment
    sets ``KEY_PREFIX`` differently on purpose. A report that just said
    ``"default"`` would hide that: the value belongs in the report so a reader
    can see at a glance that the record a peer holds under the same logical key
    is a *different record*, untouched by this drop.
    """
    try:
        from django.conf import settings

        conf = (getattr(settings, "CACHES", None) or {}).get(alias) or {}
        prefix = conf.get("KEY_PREFIX") or ""
    except Exception:  # pragma: no cover - settings not configured
        return "service-local:<unknown>"
    return f"service-local:{prefix}" if prefix else "service-local:<no KEY_PREFIX>"


def measured_drop(
    *,
    key: str,
    what: str,
    namespace: str,
    exists: Callable[[], bool],
    delete: Callable[[], object],
    log: logging.Logger = logger,
    hint: str = DEFAULT_MISS_HINT,
    absence_is_normal: bool = False,
) -> DropReport:
    """Delete *key* and report what that did, having read the store back.

    Read, delete, read again. The read-back is what makes ``DROPPED`` a
    measurement instead of a claim.

    *exists* and *delete* are supplied by the owning module so this function
    never has to know how that module reaches its store — a fleet-shared
    connection, the service-local cache, or a wrapper that raises its own
    outage exception. **Any exception from either is an outage**, reported as
    ``UNAVAILABLE`` rather than swallowed into a value that reads like a
    verdict.

    *absence_is_normal* lowers the ``NOT_FOUND`` log to DEBUG for the one kind
    of caller where nothing-to-drop is the ordinary case (a fleet-wide
    invalidation broadcast reaching a peer that never cached that user). It is
    a per-call-site decision and never a default: a warning on every event
    would teach the reader to ignore the warning that matters.
    """
    try:
        existed = bool(exists())
    except Exception as exc:  # noqa: BLE001 — any backend error is an outage
        return _unavailable(key, what, namespace, exc, log, "read")
    try:
        delete()
    except Exception as exc:  # noqa: BLE001
        return _unavailable(key, what, namespace, exc, log, "delete")
    try:
        survived = bool(exists())
    except Exception as exc:  # noqa: BLE001
        return _unavailable(key, what, namespace, exc, log, "read back")

    if survived:
        log.error(
            "%s NOT dropped: %r is still readable in namespace %r after delete "
            "— the store did not obey; do not treat this as success",
            what, key, namespace,
        )
        return DropReport(DropOutcome.STILL_PRESENT, what, key, namespace)
    if existed:
        return DropReport(DropOutcome.DROPPED, what, key, namespace)

    log.log(
        logging.DEBUG if absence_is_normal else logging.WARNING,
        "%s drop found nothing at %r in namespace %r. If you expected a record "
        "here, whatever wrote it computed a different key — %s.",
        what, key, namespace, hint,
    )
    return DropReport(DropOutcome.NOT_FOUND, what, key, namespace)


def drop_cache_key(
    cache,
    key: str,
    *,
    what: str,
    namespace: str,
    log: logging.Logger = logger,
    hint: str = DEFAULT_MISS_HINT,
    absence_is_normal: bool = False,
) -> DropReport:
    """:func:`measured_drop` over an ordinary Django cache connection.

    *cache* is the connection, or a zero-argument callable returning one — a
    callable when opening the connection can itself fail, so that failure is
    reported as ``UNAVAILABLE`` instead of escaping a function whose whole
    contract is that it answers.
    """

    def _conn():
        return cache() if callable(cache) else cache

    return measured_drop(
        key=key,
        what=what,
        namespace=namespace,
        exists=lambda: _conn().get(key) is not None,
        delete=lambda: _conn().delete(key),
        log=log,
        hint=hint,
        absence_is_normal=absence_is_normal,
    )


def measured_clear(
    cache,
    *,
    what: str,
    namespace: str,
    probe_prefix: str,
    log: logging.Logger = logger,
) -> DropReport:
    """Empty a whole cache connection, and measure that it happened.

    A clear cannot be measured the way :func:`measured_drop` measures a single
    key: there is no key to read back, and nothing enumerates what was there.
    That is exactly the situation where returning ``True`` for "did not raise"
    is a comforting lie, so this measures what it *can* — that the clear
    reached the namespace this library writes — by writing a probe first and
    reading it back afterwards.

    Outcomes here are three, not four; ``NOT_FOUND`` cannot occur because the
    probe was just written:

    * ``DROPPED`` — the probe is gone, so the clear reached this namespace.
    * ``STILL_PRESENT`` — the probe survived: the clear did not obey.
    * ``UNAVAILABLE`` — the store raised, or does not retain what it is given
      (a dummy/no-op backend), so no clear can be measured on it at all.

    **What it deliberately does NOT claim:** that only this library's keys were
    removed. ``clear()`` empties the whole connection, and on a backend where
    several key prefixes share one store (LocMemCache keys everything under one
    ``LOCATION``) that is more than revocation. The verb is an operator and
    test primitive for that reason.
    """
    probe = f"{probe_prefix}{secrets.token_hex(8)}"

    def _conn():
        return cache() if callable(cache) else cache

    try:
        _conn().set(probe, "1", 60)
        if _conn().get(probe) is None:
            return _unavailable(
                probe, what, namespace,
                "the store did not retain the probe, so a clear cannot be "
                "measured on it (a dummy or unreachable backend)",
                log, "probe",
            )
        _conn().clear()
        survived = _conn().get(probe) is not None
    except Exception as exc:  # noqa: BLE001
        return _unavailable(probe, what, namespace, exc, log, "clear")

    if survived:
        log.error(
            "%s NOT cleared: the probe %r is still readable in namespace %r "
            "after clear() — the store did not obey; do not treat this as "
            "success",
            what, probe, namespace,
        )
        _conn().delete(probe)
        return DropReport(DropOutcome.STILL_PRESENT, what, probe, namespace)
    return DropReport(DropOutcome.DROPPED, what, probe, namespace)


def _unavailable(key, what, namespace, exc, log, phase) -> DropReport:
    log.error(
        "%s drop could not be measured at %r in namespace %r: the %s failed "
        "(%s). Nothing was removed as far as anyone can tell, and this is NOT "
        "evidence that the record is absent.",
        what, key, namespace, phase, exc,
    )
    return DropReport(DropOutcome.UNAVAILABLE, what, key, namespace, error=str(exc))


__all__ = [
    "DEFAULT_MISS_HINT",
    "DropOutcome",
    "DropReport",
    "drop_cache_key",
    "measured_clear",
    "measured_drop",
    "service_namespace",
]
