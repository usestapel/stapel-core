"""One-time codes as a TTL store, not a table.

A one-time code is a bearer credential with a lifetime measured in minutes.
Putting one in a relational table gets two things wrong at once.

**It rests in the clear.** A row holding the code verbatim authenticates as
its owner to anyone who can read it once — a dump, a backup, a support query,
an injection somewhere else entirely. Passwords in this fleet are hashed and
issued tokens are stored as digests (``gateway/tokens.py``); a login code is
the same kind of value and gets the same treatment here.

**It outlives its meaning.** A table of things that expire needs a sweeper,
and a sweeper is a job that can be forgotten, fall behind, or be dropped from
a host's beat schedule — after which dead credentials accumulate silently. A
store with a TTL cannot forget: the entry's lifetime *is* the code's lifetime.

So the code lives in the Django cache (Redis in production), hashed, under a
key that expires on its own. This module is the mechanism only. Lifetimes,
attempt budgets, code length and delivery are policy and belong to the caller
— stapel-auth passes them in on every call. That split mirrors
:mod:`stapel_core.verification.grants`, which already holds the challenge and
grant stores this way.

Absence and wrongness are different facts
-----------------------------------------
:meth:`OneTimeCodeStore.check` never collapses "no entry" into "wrong code".
``NOT_FOUND`` means the wait expired (or the code was already spent, or the
cache restarted) and the honest answer to the user is an invitation to start
over. ``MISMATCH`` means they typed the wrong digits and may try again.
Rendering the first as the second tells a user they made a mistake when the
system simply stopped waiting.

Redis is not durable, and that is fine here
-------------------------------------------
A cache restart drops every pending code. Nothing is lost that mattered: the
user re-requests a code, which is what they would do thirty seconds later
anyway. It matters that the *message* is right, and it is by construction — a
dropped entry is indistinguishable from an aged-out one, so a restart reads as
``NOT_FOUND``, the same "the wait expired, ask again" the clock produces.

The attempt budget lives inside the entry
-----------------------------------------
Not beside it. A code in one place and its attempt counter in another with a
different lifetime is a bug waiting for a coincidence: the counter outliving
the code re-blocks a fresh request, the code outliving the counter hands back
an unlimited guessing budget. One record, one TTL, one death. The block is
deliberately a *separate* key with its own lifetime, because a block must
survive the code it killed — otherwise burning the last attempt would clear
the penalty it earned.

Everything fails closed
-----------------------
A cache the store cannot reach yields ``UNAVAILABLE`` from
:meth:`~OneTimeCodeStore.check` and raises :class:`StoreUnavailable` from the
write paths. It never yields ``OK``, and the caller must not render the outage
as a rejection: "we could not ask" is not "you may not".

This store is SERVICE-LOCAL on purpose, and grants are not
-----------------------------------------------------------
0.45.0 moved challenges, grants and verification tokens onto the fleet-wide
namespace (``core/fleet_cache.py``) because a factor completed in the auth
service had to count in the peer that demanded it. This store stayed on
``django.core.cache.cache``, and that was a decision, recorded here in 0.47.0
rather than left as an accident of history.

**The two halves of a one-time code happen in one service.** The same service
issues the code, delivers it and checks it back; nothing else ever asks. The
record that must travel is the *outcome* — the grant — and that one does.
Sharing this store across the fleet would buy nothing and spend three things:
a hashed bearer credential would become readable from every service instead of
one, the per-identifier attempt budget and block would be shared state that a
peer could exhaust or clear, and the send-rate window would stop being a
property of the service that does the sending.

So the rule is the one the fleet cache module states in reverse: state whose
question and answer live in one service must NOT be fleet-shared. The practical
consequence is visible in the reports these verbs return — their
``namespace`` reads ``service-local:<KEY_PREFIX>``, and a
``NOT_FOUND`` from a peer's process is expected, not a defect to chase. If a
deployment ever splits issue and check across two services, this store is
wrong for it and the split is what needs revisiting first.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass
from enum import Enum

from stapel_core.core.drop import DropReport, measured_drop, service_namespace

logger = logging.getLogger(__name__)

#: Namespace for every key this module writes.
KEY_NAMESPACE = "stapel:verification"

#: Rolling window for the per-identifier send cap.
SEND_WINDOW = 3600


class StoreUnavailable(RuntimeError):
    """The code store could not be reached. Never treat as a verdict."""


class CodeOutcome(str, Enum):
    """What :meth:`OneTimeCodeStore.check` found.

    The four members are four different things to tell a user, and none of
    them may be folded into another.
    """

    #: The code matched. It has been spent and no longer exists.
    OK = "ok"
    #: Nothing is waiting: aged out, already spent, or the cache restarted.
    NOT_FOUND = "not_found"
    #: Something is waiting and this is not it.
    MISMATCH = "mismatch"
    #: The attempt budget is spent; the penalty has not elapsed.
    BLOCKED = "blocked"
    #: The store could not answer. Not a rejection, not an admission.
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class CodeCheck:
    """The verdict, plus what the caller needs to explain it."""

    outcome: CodeOutcome
    #: Guesses left before the block. Only set for ``MISMATCH``.
    attempts_remaining: int | None = None
    #: Seconds until the caller may try again. Only set for ``BLOCKED``.
    retry_after: int | None = None
    #: The device the code was issued to, when one was recorded.
    device_id: str | None = None


@dataclass(frozen=True)
class IssuedCode:
    """Receipt for a stored code. Carries no code — by design."""

    purpose: str
    #: Unix seconds at which the entry stops existing.
    expires_at: int
    ttl: int
    device_id: str | None = None


def _cache():
    from django.core.cache import cache

    return cache


def _key_material() -> bytes:
    from django.conf import settings

    key = settings.SECRET_KEY
    return key.encode("utf-8") if isinstance(key, str) else key


def _mac(*parts: str) -> str:
    """Keyed digest over ``parts``.

    Keyed, not bare: a six-digit code has a million preimages, so a plain
    digest of one is recovered by an offline sweep the moment a dump leaks —
    which would make the hashing decorative. Binding it to ``SECRET_KEY``
    means a reader of the store alone cannot run that sweep at all.
    """
    msg = "\x1f".join(parts).encode("utf-8")
    return hmac.new(_key_material(), msg, hashlib.sha256).hexdigest()


class OneTimeCodeStore:
    """TTL-scoped store for the one-time codes of a single *purpose*.

    *purpose* separates code families that must not satisfy each other
    ("otp_email", "otp_phone", a password-reset flow). Entries under different
    purposes never collide, even for the same identifier.
    """

    def __init__(self, purpose: str, *, namespace: str = KEY_NAMESPACE) -> None:
        self.purpose = purpose
        self.namespace = namespace

    # ── keys ────────────────────────────────────────────────────────────────

    def _slug(self, identifier: str) -> str:
        """Key fragment for *identifier* — a digest, not the value.

        Cache keys are readable to anything that can ``SCAN`` the instance,
        and in this fleet that instance is shared with sessions, throttles and
        grants. A plain key would publish who is signing in right now, and
        would break on identifiers carrying spaces or colons besides.
        """
        return _mac("id", self.purpose, identifier)[:32]

    def _code_key(self, identifier: str) -> str:
        return f"{self.namespace}:code:{self.purpose}:{self._slug(identifier)}"

    def _block_key(self, identifier: str) -> str:
        return f"{self.namespace}:block:{self.purpose}:{self._slug(identifier)}"

    def _cooldown_key(self, identifier: str) -> str:
        return f"{self.namespace}:cooldown:{self.purpose}:{self._slug(identifier)}"

    def _device_key(self, device_id: str) -> str:
        return f"{self.namespace}:cooldown-dev:{self.purpose}:{self._slug(device_id)}"

    def _sends_key(self, identifier: str) -> str:
        return f"{self.namespace}:sends:{self.purpose}:{self._slug(identifier)}"

    # ── cache access (every call fails closed) ──────────────────────────────

    def _get(self, key: str):
        try:
            return _cache().get(key)
        except Exception as exc:  # noqa: BLE001 — any backend error is an outage
            raise StoreUnavailable(str(exc)) from exc

    def _set(self, key: str, value, timeout: int) -> None:
        try:
            _cache().set(key, value, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            raise StoreUnavailable(str(exc)) from exc

    def _delete(self, key: str) -> None:
        try:
            _cache().delete(key)
        except Exception as exc:  # noqa: BLE001
            raise StoreUnavailable(str(exc)) from exc

    def _drop(self, key: str, what: str) -> DropReport:
        """Remove *key* and measure it, in the vocabulary the package shares.

        Goes through this class's own ``_get``/``_delete`` rather than the
        cache directly, so an outage arrives as :class:`StoreUnavailable` and
        is reported as ``UNAVAILABLE`` — never folded into "nothing was
        there".
        """
        return measured_drop(
            key=key,
            what=what,
            namespace=service_namespace(),
            exists=lambda: self._get(key) is not None,
            delete=lambda: self._delete(key),
            log=logger,
            hint=(
                "this store is deliberately SERVICE-LOCAL (see the module "
                "header), so a peer's copy of the same identifier is a "
                "different record and is not reachable from here"
            ),
        )

    # ── issue ───────────────────────────────────────────────────────────────

    def issue(
        self,
        identifier: str,
        code: str,
        *,
        ttl: int,
        max_attempts: int,
        device_id: str | None = None,
    ) -> IssuedCode:
        """Store *code* for *identifier*, live for *ttl* seconds.

        Replaces any pending code for the same identifier, attempt budget and
        all — a freshly requested code starts a fresh wait, and inheriting the
        old counter would let a resend be blocked by guesses against a code
        that no longer exists.

        Issuing is also what spends the send budget: the cooldown starts here
        and the hourly window counts this send. Callers therefore check
        :meth:`send_wait` *before* issuing, and a request that never reaches
        this method costs the user nothing.
        """
        now = int(time.time())
        ttl = max(int(ttl), 1)
        salt = secrets.token_hex(8)
        record = {
            "digest": _mac("code", self.purpose, identifier, salt, str(code)),
            "salt": salt,
            "attempts": 0,
            "max_attempts": max(int(max_attempts), 1),
            "issued_at": now,
            "expires_at": now + ttl,
            "device_id": device_id,
        }
        self._set(self._code_key(identifier), record, ttl)
        self._start_cooldown(identifier, device_id)
        self._spend_send_slot(identifier)
        return IssuedCode(
            purpose=self.purpose,
            expires_at=record["expires_at"],
            ttl=ttl,
            device_id=device_id,
        )

    # ── check ───────────────────────────────────────────────────────────────

    def check(
        self, identifier: str, code: str, *, block_seconds: int = 0
    ) -> CodeCheck:
        """Compare *code* against what is stored for *identifier*.

        A match spends the entry: the code cannot be replayed, and a second
        presentation is ``NOT_FOUND`` like any other absence.

        A mismatch bumps the attempt counter inside the entry, leaving its
        deadline alone — guessing must not extend the wait. When the budget
        runs out the entry is destroyed and, if *block_seconds* is given, a
        block is written that outlives it.
        """
        try:
            standing = self.blocked_for(identifier)
            if standing:
                return CodeCheck(CodeOutcome.BLOCKED, retry_after=standing)

            key = self._code_key(identifier)
            record = self._get(key)
            if not record:
                return CodeCheck(CodeOutcome.NOT_FOUND)

            expected = record.get("digest", "")
            actual = _mac(
                "code", self.purpose, identifier, record.get("salt", ""), str(code)
            )
            if hmac.compare_digest(expected, actual):
                self._delete(key)
                return CodeCheck(CodeOutcome.OK, device_id=record.get("device_id"))

            attempts = int(record.get("attempts", 0)) + 1
            budget = int(record.get("max_attempts", 1))
            if attempts >= budget:
                self._delete(key)
                retry_after = (
                    self.block(identifier, block_seconds) if block_seconds > 0 else 0
                )
                return CodeCheck(CodeOutcome.BLOCKED, retry_after=retry_after)

            record["attempts"] = attempts
            remaining_ttl = max(int(record.get("expires_at", 0)) - int(time.time()), 1)
            self._set(key, record, remaining_ttl)
            return CodeCheck(
                CodeOutcome.MISMATCH,
                attempts_remaining=budget - attempts,
                device_id=record.get("device_id"),
            )
        except StoreUnavailable:
            logger.error("verification code store unavailable for purpose=%s", self.purpose)
            return CodeCheck(CodeOutcome.UNAVAILABLE)

    def discard(self, identifier: str) -> DropReport:
        """Drop any pending code for *identifier* (erasure, or a spent flow).

        Reports what that did, as :meth:`check` reports what it found. Until
        0.47.0 this returned ``None`` **and swallowed**
        :class:`StoreUnavailable`, so "the store was down", "there was nothing
        to discard" and "dropped" were one value — in the module whose own
        header insists absence and wrongness are different facts. An erasure
        job that read that value as done had no way to learn it had not been.

        ``UNAVAILABLE`` is the outcome that was previously invisible: the code
        may well still be pending. The exception is still not raised — a
        discard is usually a best-effort tail of some other operation — but it
        is now in the return value and in the log, which is the difference
        between best-effort and unknowable.
        """
        return self._drop(self._code_key(identifier), "pending code")

    # ── block ───────────────────────────────────────────────────────────────

    def block(self, identifier: str, seconds: int) -> int:
        """Refuse this identifier for *seconds*. Returns the wait imposed."""
        seconds = max(int(seconds), 1)
        self._set(self._block_key(identifier), int(time.time()) + seconds, seconds)
        return seconds

    def blocked_for(self, identifier: str) -> int:
        """Seconds left on an active block, or ``0``."""
        until = self._get(self._block_key(identifier))
        if not until:
            return 0
        return max(int(until) - int(time.time()), 0)

    def unblock(self, identifier: str) -> DropReport:
        """Lift the penalty on *identifier*; reports what that did.

        The support-desk verb: someone who burned their attempts is on the
        phone. ``NOT_FOUND`` means there was no block to lift under this key
        — which for a *support* call is worth seeing, because the identifier
        the operator typed may not be the one the block was written for.
        """
        return self._drop(self._block_key(identifier), "code block")

    # ── send budget ─────────────────────────────────────────────────────────

    def send_wait(
        self,
        identifier: str,
        *,
        cooldown: int,
        hourly_limit: int,
        device_id: str | None = None,
    ) -> int:
        """Seconds the caller must wait before sending again; ``0`` when free.

        Read-only — :meth:`issue` is what spends the budget. Raises
        :class:`StoreUnavailable` rather than returning ``0``: a store that
        cannot answer must not be read as "no limit applies".
        """
        if cooldown > 0:
            remaining = self._cooldown_left(self._cooldown_key(identifier), cooldown)
            if remaining:
                return remaining
            if device_id:
                remaining = self._cooldown_left(self._device_key(device_id), cooldown)
                if remaining:
                    return remaining
        return self._hourly_wait(identifier, hourly_limit)

    def _cooldown_left(self, key: str, cooldown: int) -> int:
        """Seconds of *cooldown* still owed since the send recorded at *key*.

        The stored value is the send's timestamp, not a pre-computed deadline,
        so the caller's current setting is what decides — the table this
        replaced compared ``created_at`` against a cutoff derived from the
        live setting, and a deadline baked in at write time would instead
        freeze whatever the setting said when the code went out.
        """
        sent_at = self._get(key)
        if not sent_at:
            return 0
        return max(int(sent_at) + int(cooldown) - int(time.time()), 0)

    def _hourly_wait(self, identifier: str, limit: int) -> int:
        """Rolling-window cap: seconds until the oldest send ages out.

        A rolling window, not a fixed one, because the table this replaced
        counted rows by ``created_at`` and a fixed window would let a user
        spend a full quota twice across a boundary. ``limit <= 0`` disables it.
        """
        if limit <= 0:
            return 0
        now = int(time.time())
        sends = [t for t in (self._get(self._sends_key(identifier)) or []) if t > now - SEND_WINDOW]
        if len(sends) < limit:
            return 0
        return max(min(sends) + SEND_WINDOW - now, 1)

    def _spend_send_slot(self, identifier: str) -> None:
        key = self._sends_key(identifier)
        now = int(time.time())
        sends = [t for t in (self._get(key) or []) if t > now - SEND_WINDOW]
        sends.append(now)
        self._set(key, sends, SEND_WINDOW)

    def _start_cooldown(self, identifier: str, device_id: str | None) -> None:
        # The key holds when the send happened; SEND_WINDOW is only how long
        # that fact is worth keeping (see _cooldown_left).
        now = int(time.time())
        self._set(self._cooldown_key(identifier), now, SEND_WINDOW)
        if device_id:
            self._set(self._device_key(device_id), now, SEND_WINDOW)


__all__ = [
    "CodeCheck",
    "CodeOutcome",
    "IssuedCode",
    "OneTimeCodeStore",
    "StoreUnavailable",
    "KEY_NAMESPACE",
    "SEND_WINDOW",
]
