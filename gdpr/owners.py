"""The data-owner side of the erasure protocol — one implementation.

stapel-gdpr orchestrates an erasure: it creates one ``ErasurePart`` per
owner that claims the subject type, emits ``gdpr.erasure.requested``, and
waits for a receipt. An owner library answers with three handlers that are
the same in every library that has ever written them — deterministic
receipt id, receipt inside the erase's transaction, silence for a subject
it does not claim, a logged drop for a malformed payload, and a probe
answered from the same module so ``gdpr.owner.alive`` is evidence that the
erasure path is *consumed* rather than that a container is running.

Nine libraries carried that code verbatim. Here it is once::

    # stapel_recordings/apps.py
    class RecordingsConfig(AppConfig):
        def ready(self):
            from stapel_core.gdpr import register_gdpr_owner
            from .erasure import erase_subject

            register_gdpr_owner(
                "recordings",
                ["account", "workspace", "meeting", "recording"],
                erase_subject,
            )

The library keeps exactly what is its own: ``erase_subject(subject_type,
subject_key, workspace_id)`` — idempotent, counting what it removed,
returning ``{"recordings": 3, "segments": 12}`` or ``None`` for "this key
names nothing of mine". Everything around it is protocol.

Handlers are idempotent because delivery is at-least-once (outbox retries,
broker redelivery): a redelivery re-runs ``erase``, which reports the ``0``
rows it touched rather than pretending it did the work twice, and mints the
SAME receipt id.
"""
from __future__ import annotations

import logging
import sys
import threading
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

logger = logging.getLogger(__name__)

#: Action names of the protocol. Consumed by an owner, emitted by an owner.
ERASURE_REQUESTED = "gdpr.erasure.requested"
OWNER_PROBE = "gdpr.owner.probe"
SECTION_ERASED = "gdpr.section.erased"
OWNER_ALIVE = "gdpr.owner.alive"
USER_DELETED = "user.deleted"

#: Prefix of every pseudonymized id in the fleet. It is what makes
#: :func:`pseudonymize` idempotent: a value already carrying it is already a
#: pseudonym, so a redelivered erasure cannot mint a second pseudonym for one
#: subject and split its history in two.
PSEUDONYM_PREFIX = "erased:"

#: Payload contracts. Deliberately the LOOSEST correct shape — required keys
#: and their types, additional properties allowed. A schema registered here
#: is registered for the whole process, and an owner library must not be the
#: reason a field stapel-gdpr added is refused at the emitter.
ERASURE_REQUESTED_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": ERASURE_REQUESTED,
    "type": "object",
    "required": ["correlation_id", "subject_type", "subject_key"],
    "properties": {
        "correlation_id": {"type": ["string", "integer"]},
        "subject_type": {"type": "string"},
        "subject_key": {"type": ["string", "integer"]},
        "workspace_id": {"type": ["string", "integer", "null"]},
    },
}

OWNER_PROBE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": OWNER_PROBE,
    "type": "object",
    "properties": {"correlation_id": {"type": ["string", "integer"]}},
}

#: ``erase(subject_type, subject_key, workspace_id) -> counts | None``.
EraseCallable = Callable[[str, str, "str | None"], "Mapping[str, object] | None"]

_lock = threading.Lock()
_owners: dict[str, "GdprOwner"] = {}


def receipt_id(owner: str, subject_type: str, subject_key: str, correlation_id: str) -> str:
    """Durable, deterministic proof-of-work id for one erasure.

    Derived rather than random so a redelivery produces the SAME receipt:
    the orchestrator stores it on the part, and two ids for one erasure
    would make an audit trail that cannot be followed back.
    """
    return f"{owner}:{subject_type}:{subject_key}:{correlation_id}"


def pseudonymize(value, prefix: str = PSEUDONYM_PREFIX) -> str:
    """Replace an id with a stable pseudonym — the fleet's one scheme.

    A keyed digest (HMAC-SHA256 under the deployment's ``SECRET_KEY``,
    truncated to 32 hex): stable, so one subject's rows stay one subject and
    per-subject arithmetic still works, and not reversible without the key.
    Never a plain hash — a bare digest of a user id is a rainbow table away
    from being the id again.

    This is what a ledger-carrying owner erases with. Economics columns are
    the product's record and survive; the ids that NAME the person do not.

    A ``SECRET_KEY`` rotation splits pseudonyms — an erasure after the
    rotation produces a different value for the same id. Accepted
    fleet-wide: the alternative is a second key nobody rotates.
    """
    import hashlib
    import hmac

    from django.conf import settings

    text = str(value)
    if text.startswith(prefix):
        return text
    digest = hmac.new(
        str(settings.SECRET_KEY).encode(), text.encode(), hashlib.sha256
    ).hexdigest()[:32]
    return f"{prefix}{digest}"


@dataclass(frozen=True)
class GdprOwner:
    """What :func:`register_gdpr_owner` registered — and its handlers.

    Returned so a library can unit-test its own erasure through the exact
    handlers the bus will call, without reaching into this module.
    """

    owner: str
    subject_types: tuple[str, ...]
    erase: EraseCallable
    legacy_user_deleted: bool
    handle_erasure_requested: Callable[[object], None]
    handle_owner_probe: Callable[[object], None]
    handle_user_deleted: Callable[[object], None] | None


def register_gdpr_owner(
    owner: str,
    subject_types: Sequence[str],
    erase: EraseCallable,
    *,
    legacy_user_deleted: bool = True,
) -> GdprOwner:
    """Subscribe this library as a stapel-gdpr data owner. Call from ``ready()``.

    *owner* is the name the host declares in ``STAPEL_GDPR["DATA_OWNERS"]``
    (the library's ``GDPRProvider.section``: ``workspaces``, ``profile``,
    ``recordings``, ``media`` …). *subject_types* are the types this module
    can really erase — the probe answers with these, so declaring one the
    ``erase`` callable cannot handle turns a liveness answer into a lie.

    ``erase(subject_type, subject_key, workspace_id)`` must be idempotent and
    return a counts mapping (``{"recordings": 3}``), or ``None`` when the key
    names nothing this module owns. ``None`` receipts nothing: an erasure the
    orchestrator is not waiting for is not this owner's to confirm.

    *legacy_user_deleted* also subscribes the pre-0.5.0 ``user.deleted``
    account signal, which stapel-gdpr emits beside the request for one minor.
    It runs the same ``erase("account", …)`` — there is no second erasure
    implementation to drift — and receipts only when the payload carried a
    ``correlation_id``. Pass ``False`` once the fleet has dropped it, or for
    an owner that does not claim ``account``.

    Registering the same owner twice with the same arguments is a no-op (a
    ready() that runs twice must not double-subscribe); with different ones
    it raises, because the second registration would silently lose to the
    first for some payloads and win for others.
    """
    from stapel_core.comm import subscribe_action

    caller_module = _calling_module(erase)
    name = str(owner or "").strip()
    if not name:
        raise ValueError("register_gdpr_owner() needs a non-empty owner name")
    types = tuple(dict.fromkeys(str(t) for t in subject_types))
    if not types:
        raise ValueError(
            f"gdpr owner {name!r} declares no subject types — an owner that "
            "erases nothing must not answer the probe at all"
        )
    if not callable(erase):
        raise TypeError(f"gdpr owner {name!r}: erase must be callable")

    with _lock:
        existing = _owners.get(name)
        if existing is not None:
            if (
                existing.subject_types != types
                or existing.erase is not erase
                or existing.legacy_user_deleted != legacy_user_deleted
            ):
                raise ValueError(
                    f"gdpr owner {name!r} is already registered with different "
                    f"terms (subject types {list(existing.subject_types)}, "
                    f"erase {existing.erase!r}); one name is one owner"
                )
            return existing
        registration = _build(name, types, erase, legacy_user_deleted, caller_module)
        _owners[name] = registration

    subscribe_action(
        ERASURE_REQUESTED,
        registration.handle_erasure_requested,
        schema=ERASURE_REQUESTED_SCHEMA,
    )
    subscribe_action(
        OWNER_PROBE, registration.handle_owner_probe, schema=OWNER_PROBE_SCHEMA
    )
    if registration.handle_user_deleted is not None:
        subscribe_action(USER_DELETED, registration.handle_user_deleted)
    logger.debug(
        "gdpr owner %s subscribed for subject types %s", name, list(types)
    )
    return registration


def registered_gdpr_owners() -> dict[str, tuple[str, ...]]:
    """Owner name → the subject types it registered, in this process."""
    with _lock:
        return {name: reg.subject_types for name, reg in _owners.items()}


def _reset_gdpr_owners() -> None:
    """Tests only — forget the registrations (the comm registry is separate)."""
    with _lock:
        _owners.clear()


def _emit_receipt(
    owner: str,
    correlation_id: str,
    subject_type: str,
    subject_key: str,
    counts: Mapping[str, object],
    *,
    user_id: str | None = None,
) -> None:
    """Emit ``gdpr.section.erased``. The caller holds the erase transaction."""
    from stapel_core.comm import emit

    payload = {
        "owner": owner,
        "subject_type": subject_type,
        "subject_key": subject_key,
        "correlation_id": correlation_id,
        "receipt_id": receipt_id(owner, subject_type, subject_key, correlation_id),
        "counts": dict(counts),
    }
    if user_id is not None:
        payload["user_id"] = user_id
    emit(SECTION_ERASED, payload, key=subject_key)


def _calling_module(erase: EraseCallable) -> str:
    """The library module that asked for this registration.

    Handlers built here are closures of ``stapel_core.gdpr.owners``, so their
    ``__module__`` names core rather than the library whose rows they touch.
    Every check that groups subscribers by app would charge core for all of
    them. The caller's frame (the library's ``AppConfig.ready()``) is the
    honest answer; ``erase`` is the fallback when there is no Python frame to
    read.
    """
    try:
        frame = sys._getframe(2)
    except ValueError:  # pragma: no cover — no caller frame
        frame = None
    module = frame.f_globals.get("__name__", "") if frame is not None else ""
    return str(module or getattr(erase, "__module__", "") or "")


def _build(
    owner: str,
    subject_types: tuple[str, ...],
    erase: EraseCallable,
    legacy_user_deleted: bool,
    caller_module: str = "",
) -> GdprOwner:
    def handle_erasure_requested(event) -> None:
        """Erase this module's slice of one subject and confirm it.

        Erasure and confirmation are one transaction (outbox discipline):
        the receipt leaves iff the erasure committed, so an owner can never
        report an erasure a rollback undid.
        """
        from django.core.exceptions import ValidationError
        from django.db import transaction

        payload = getattr(event, "payload", None) or {}
        correlation_id = payload.get("correlation_id")
        subject_type = payload.get("subject_type")
        subject_key = payload.get("subject_key")
        if not correlation_id or not subject_type or not subject_key:
            # Dropped, not raised: a payload this shape will never parse, and
            # raising would redeliver it until the broker gives up.
            logger.error(
                "malformed %s for owner %s: %s",
                ERASURE_REQUESTED, owner, getattr(event, "event_id", "?"),
            )
            return
        if subject_type not in subject_types:
            # The orchestrator creates a part only for owners that claim the
            # type; receipting one that does not exist teaches it nothing.
            logger.debug(
                "%s for subject_type %r — not owned by %s",
                ERASURE_REQUESTED, subject_type, owner,
            )
            return

        subject_key = str(subject_key)
        try:
            with transaction.atomic():
                counts = erase(
                    subject_type, subject_key, payload.get("workspace_id")
                )
                if counts is None:
                    return
                _emit_receipt(
                    owner, str(correlation_id), subject_type, subject_key, counts
                )
        except (TypeError, ValueError, ValidationError):
            # An unparseable key names no row here. Receipting would claim an
            # erasure that never happened; raising would retry forever.
            logger.error(
                "%s with unusable %s key %r for owner %s [correlation=%s]",
                ERASURE_REQUESTED, subject_type, subject_key, owner,
                correlation_id, exc_info=True,
            )
            return
        logger.info(
            "%s erased %s %s: %s [correlation=%s]",
            owner, subject_type, subject_key, dict(counts), correlation_id,
        )

    def handle_owner_probe(event) -> None:
        """Answer the liveness probe with what this owner actually erases.

        Same module as the erasure handler by design: the answer is evidence
        that the subscriber above is consumed. The reported subject types are
        the ones registered here, not the ones a settings file hoped for — a
        probe that echoed the host's declaration back would confirm nothing.
        """
        from stapel_core.comm import emit

        payload = getattr(event, "payload", None) or {}
        answer = {"owner": owner, "subject_types": list(subject_types)}
        correlation_id = payload.get("correlation_id")
        if correlation_id:
            answer["correlation_id"] = str(correlation_id)
        emit(OWNER_ALIVE, answer, key=owner)

    def handle_user_deleted(event) -> None:
        """DEPRECATED account signal — same erase path, kept for one minor.

        Both signals land here for an account; the second finds nothing left
        to erase and receipts its zeroes, which is what an idempotent handler
        looks like. When this handler goes, no erasure logic goes with it.
        """
        from django.core.exceptions import ValidationError
        from django.db import transaction

        payload = getattr(event, "payload", None) or {}
        user_id = payload.get("user_id")
        if not user_id:
            logger.error(
                "%s without user_id for owner %s: %s",
                USER_DELETED, owner, getattr(event, "event_id", "?"),
            )
            return
        correlation_id = payload.get("correlation_id")
        user_id = str(user_id)
        try:
            with transaction.atomic():
                counts = erase("account", user_id, None)
                if counts is not None and correlation_id:
                    _emit_receipt(
                        owner, str(correlation_id), "account", user_id, counts,
                        user_id=user_id,
                    )
        except (TypeError, ValueError, ValidationError):
            logger.error(
                "%s with unusable account key %r for owner %s",
                USER_DELETED, user_id, owner, exc_info=True,
            )
            return
        logger.info(
            "%s erased account %s: %s", owner, user_id, dict(counts or {}),
        )

    for handler in (handle_erasure_requested, handle_owner_probe, handle_user_deleted):
        handler.stapel_handler_module = caller_module

    return GdprOwner(
        owner=owner,
        subject_types=subject_types,
        erase=erase,
        legacy_user_deleted=legacy_user_deleted,
        handle_erasure_requested=handle_erasure_requested,
        handle_owner_probe=handle_owner_probe,
        handle_user_deleted=(
            handle_user_deleted
            if legacy_user_deleted and "account" in subject_types
            else None
        ),
    )


__all__ = [
    "ERASURE_REQUESTED",
    "ERASURE_REQUESTED_SCHEMA",
    "OWNER_ALIVE",
    "OWNER_PROBE",
    "OWNER_PROBE_SCHEMA",
    "PSEUDONYM_PREFIX",
    "SECTION_ERASED",
    "USER_DELETED",
    "GdprOwner",
    "pseudonymize",
    "receipt_id",
    "register_gdpr_owner",
    "registered_gdpr_owners",
]
