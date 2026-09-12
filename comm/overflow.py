"""A payload that outgrows one broker message travels by REFERENCE.

A broker message is not a file. NATS caps a single message (8 MiB on the
fleet's brokers, 1 MiB by default) and refuses to send an oversized one, so a
Function whose answer grows with its input is one long input away from
losing completed work — measured, twice, on the owner's stand (2026-09-09:
``llm.transcribe`` over two 148-minute meetings, 8 637 982 bytes against
8 388 608, the transcription paid for at the provider and then thrown away).

Raising ``max_payload`` buys headroom, not a different answer. This module is
the different answer: when a frame does not fit, the sender writes the BYTES
to the object store both ends share and sends a small envelope naming them,

    {"$ref": {"store": "django", "key": "stapel/comm/overflow/reply/...",
              "bytes": 8637982, "sha256": "...", "expires_at": "..."}}

and the receiver resolves it — downloads, checks the length and the digest,
and hands its caller exactly the bytes the sender produced. Neither the
function nor its caller knows this happened: ``call()`` returns the same
object it always did, which is the whole point. A mechanism that required
every caller to learn a new result shape would be the same work, moved.

Configuration, both ends of the seam::

    STAPEL_COMM = {
        "FUNCTION_TRANSPORT": "nats",
        "LARGE_REPLY": {
            "STORE": "django",          # or a dotted path to a store object
            "THRESHOLD_BYTES": None,    # default: the broker's max_payload
            "TTL_SECONDS": 86400,       # 24h
            "PREFIX": "stapel/comm/overflow",
        },
    }

``STORE = "django"`` rides Django's configured ``default_storage``, which in
every fleet deployment is the shared S3/MinIO bucket the services already
read and write (the same one stapel-cdn and stapel-recordings reach through
their own seams). A deployment with no shared store leaves it unset and
nothing changes: an oversized reply still fails, loudly, naming this setting.

**The object is a postbox, not an artifact.** It is deleted as soon as it has
been read, because a second verbatim copy of a private payload — a meeting
transcript, say — under no row of any table is a copy no erasure sweep will
ever find. ``TTL_SECONDS`` is the backstop for the read that never comes: it
is carried in the reference (``expires_at``) and applied by the store if the
store can express it; for the ``django`` store, whose ``Storage`` API has no
notion of expiry, set a lifecycle rule on ``PREFIX`` in the bucket. Stated
plainly rather than implied, because a TTL nobody enforces is worse than
none: it makes an operator believe the cleanup is handled.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import comm_setting
from .exceptions import FunctionReferenceError

logger = logging.getLogger(__name__)

#: The single top-level key that marks a frame as a reference rather than a
#: result. Chosen for the same reason JSON Schema chose it: a dollar sign
#: cannot collide with the ``result`` / ``error`` keys of the wire envelope.
REFERENCE_FIELD = "$ref"

DEFAULT_TTL_SECONDS = 86400
DEFAULT_PREFIX = "stapel/comm/overflow"

#: Longest key we will fetch. A reference arrives from another service; every
#: field in it is input.
MAX_KEY_LENGTH = 1024

_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]")

_store: "OverflowStore | None" = None
_store_spec: Any = None
_store_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────────────────────────────

def overflow_settings() -> dict:
    """``STAPEL_COMM["LARGE_REPLY"]`` with defaults filled in."""
    configured = comm_setting("LARGE_REPLY", {}) or {}
    if not isinstance(configured, dict):
        raise FunctionReferenceError(
            'STAPEL_COMM["LARGE_REPLY"] must be a dict of '
            "STORE / THRESHOLD_BYTES / TTL_SECONDS / PREFIX, got "
            f"{type(configured).__name__}"
        )
    return {
        "STORE": configured.get("STORE"),
        "THRESHOLD_BYTES": configured.get("THRESHOLD_BYTES"),
        "TTL_SECONDS": int(configured.get("TTL_SECONDS") or DEFAULT_TTL_SECONDS),
        "PREFIX": str(configured.get("PREFIX") or DEFAULT_PREFIX).strip("/"),
    }


def threshold_bytes(max_payload: int) -> int:
    """Bytes above which a frame travels by reference, given the broker's cap.

    ``THRESHOLD_BYTES`` is an opt-in *lower* bound — a deployment that wants
    big answers off the broker before the broker refuses them. It can never
    raise the effective threshold above ``max_payload``: past that the
    message does not go out at all, so a larger number would only mean
    "fail instead of storing".

    ``max_payload`` of 0 means the broker announced no limit; then only an
    explicit threshold applies.
    """
    configured = overflow_settings()["THRESHOLD_BYTES"]
    limit = int(configured) if configured else 0
    if max_payload and (not limit or limit > max_payload):
        limit = int(max_payload)
    return limit


# ─────────────────────────────────────────────────────────────────────
# Stores
# ─────────────────────────────────────────────────────────────────────

class OverflowStore(ABC):
    """Where an oversized frame's bytes wait between the two services.

    Deliberately tiny: put / get / delete over a byte string at a key. This
    is not a media API and must not grow into one — the object exists for
    seconds, is read exactly once, and belongs to neither service.
    """

    #: Stamped into the reference and checked by the reader. Two ends
    #: configured with different stores is a wiring error that would
    #: otherwise surface as a missing key.
    name: str = "custom"

    @abstractmethod
    def put(self, key: str, data: bytes, *, ttl_seconds: int) -> str:
        """Write *data* at *key*; return the key it actually landed on."""

    @abstractmethod
    def get(self, key: str) -> bytes:
        """Read the object back. Raises if it is not there."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Remove the object. Best effort — callers never fail on this."""


class DjangoStorageOverflowStore(OverflowStore):
    """The default: Django's ``default_storage``.

    In a fleet deployment that is the shared object bucket (django-storages
    over S3/MinIO); in a monolith or a test it is the filesystem, which is
    equally correct because a monolith has no broker to overflow.

    ``Storage`` has no expiry concept, so ``ttl_seconds`` is honoured by the
    reference's ``expires_at`` plus delete-on-read, and the bucket's own
    lifecycle rule on the prefix is the backstop. See the module docstring.
    """

    name = "django"

    def _storage(self):
        from django.core.files.storage import default_storage

        return default_storage

    def put(self, key: str, data: bytes, *, ttl_seconds: int) -> str:
        from django.core.files.base import ContentFile

        # save() may return a different name (a backend that refuses to
        # clobber appends a suffix), and the reference must name the object
        # that actually exists — not the one we asked for.
        return self._storage().save(key, ContentFile(data))

    def get(self, key: str) -> bytes:
        with self._storage().open(key, "rb") as fh:
            return fh.read()

    def delete(self, key: str) -> None:
        self._storage().delete(key)


def get_store() -> OverflowStore | None:
    """The configured store, or None when this deployment has none."""
    global _store, _store_spec

    spec = overflow_settings()["STORE"]
    if not spec:
        return None
    if _store is not None and _store_spec == spec:
        return _store
    with _store_lock:
        store = _resolve_store(spec)
        _store, _store_spec = store, spec
    return store


def _resolve_store(spec: Any) -> OverflowStore:
    if isinstance(spec, OverflowStore):
        return spec
    if spec == "django":
        return DjangoStorageOverflowStore()
    if isinstance(spec, str):
        from django.utils.module_loading import import_string

        try:
            obj = import_string(spec)
        except ImportError as exc:
            raise FunctionReferenceError(
                f'STAPEL_COMM["LARGE_REPLY"]["STORE"] = {spec!r} cannot be '
                f"imported ({exc}). Expected \"django\" (Django's "
                "default_storage) or a dotted path to an OverflowStore."
            ) from exc
        if isinstance(obj, type):
            obj = obj()
        return obj
    raise FunctionReferenceError(
        'STAPEL_COMM["LARGE_REPLY"]["STORE"] must be "django", a dotted path '
        f"or an OverflowStore instance, got {type(spec).__name__}"
    )


def reset_store() -> None:
    """Tests / settings-change hook."""
    global _store, _store_spec
    with _store_lock:
        _store, _store_spec = None, None


# ─────────────────────────────────────────────────────────────────────
# Writing a reference
# ─────────────────────────────────────────────────────────────────────

def store_frame(data: bytes, *, function: str, direction: str) -> dict:
    """Put *data* in the store and return the reference envelope for it.

    Raises :class:`FunctionReferenceError` when no store is configured —
    the caller decides what to do about it (the server turns it into the
    too-large marker; the client raises FunctionPayloadTooLarge), and either
    way the operator is told which setting is missing.
    """
    store = get_store()
    if store is None:
        raise FunctionReferenceError(no_store_hint(function, len(data), direction))

    settings_ = overflow_settings()
    ttl = settings_["TTL_SECONDS"]
    key = _new_key(settings_["PREFIX"], function, direction)
    digest = hashlib.sha256(data).hexdigest()
    actual = store.put(key, data, ttl_seconds=ttl) or key
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)
    return {
        "store": getattr(store, "name", "custom"),
        "key": actual,
        "bytes": len(data),
        "sha256": digest,
        "expires_at": expires_at.isoformat(),
        "function": function,
        "direction": direction,
    }


def encode_reference(ref: dict) -> bytes:
    """The small frame that goes on the wire in place of the bulk."""
    return json.dumps({REFERENCE_FIELD: ref}).encode()


def reference_in(frame: Any) -> dict | None:
    """The reference carried by a decoded wire frame, or None."""
    if isinstance(frame, dict):
        ref = frame.get(REFERENCE_FIELD)
        if isinstance(ref, dict):
            return ref
    return None


def _new_key(prefix: str, function: str, direction: str) -> str:
    safe = _SAFE_SEGMENT.sub("_", function or "unknown")[:80]
    return f"{prefix}/{direction}/{safe}/{uuid.uuid4().hex}.json"


def no_store_hint(function: str, size: int, direction: str) -> str:
    return (
        f"function '{function}': the {direction} is {size} bytes, too large "
        "for one broker message, and this deployment has no overflow store "
        'to send it by reference. Set STAPEL_COMM["LARGE_REPLY"]["STORE"] '
        '(e.g. "django", the shared object bucket) on BOTH ends of the seam.'
    )


# ─────────────────────────────────────────────────────────────────────
# Reading a reference
# ─────────────────────────────────────────────────────────────────────

def dereference(ref: dict, *, function: str = "", consume: bool = True) -> bytes:
    """Fetch, verify and (by default) consume the object a reference names.

    Every field of *ref* came from another service, so every field is
    checked: the store it names must be the one configured here, the key
    must live under this deployment's prefix and must not climb out of it,
    and the bytes must match both the length and the digest the sender
    recorded. A short read is the failure this shape would otherwise hide —
    it looks like a transcript that is merely missing its last hour.
    """
    store = get_store()
    name = function or str(ref.get("function") or "")
    if store is None:
        raise FunctionReferenceError(
            f"function '{name}': the answer came back as a reference to "
            f"{ref.get('bytes')} bytes in the '{ref.get('store')}' store, and "
            "this process has no store configured to resolve it. Set "
            'STAPEL_COMM["LARGE_REPLY"]["STORE"] to the same store the '
            "provider uses."
        )

    local = getattr(store, "name", "custom")
    remote = str(ref.get("store") or "")
    if remote and remote != local:
        raise FunctionReferenceError(
            f"function '{name}': the reference names the '{remote}' store but "
            f'this process is configured for {local!r}. STAPEL_COMM'
            '["LARGE_REPLY"]["STORE"] must be the same store on both ends.'
        )

    key = _checked_key(ref.get("key"), name)
    try:
        data = store.get(key)
    except FunctionReferenceError:
        raise
    except Exception as exc:
        raise FunctionReferenceError(
            f"function '{name}': the reply is stored at {key!r} and could not "
            f"be read back ({exc!r}). The object may have expired — "
            'STAPEL_COMM["LARGE_REPLY"]["TTL_SECONDS"] is '
            f'{overflow_settings()["TTL_SECONDS"]}s.'
        ) from exc

    expected = ref.get("bytes")
    if isinstance(expected, int) and len(data) != expected:
        raise FunctionReferenceError(
            f"function '{name}': read {len(data)} bytes from {key!r}, the "
            f"provider wrote {expected}. Truncated or overwritten — the "
            "payload is NOT being delivered rather than delivered short."
        )
    digest = str(ref.get("sha256") or "")
    if digest:
        actual = hashlib.sha256(data).hexdigest()
        if actual != digest:
            raise FunctionReferenceError(
                f"function '{name}': the object at {key!r} does not match the "
                f"digest the provider recorded (sha256 {actual} != {digest}). "
                "Refusing to hand a caller bytes it did not produce."
            )

    # A POSTBOX, NOT AN ARTIFACT: read once, then gone. Anything left behind
    # is a second verbatim copy of a private payload under no row of any
    # table, which no erasure sweep would ever find. ``consume=False`` is for
    # the one reader that legitimately comes back — a task checkpoint, which
    # every retry must be able to read again.
    if not consume:
        return data
    try:
        store.delete(key)
    except Exception:
        logger.warning(
            "comm: could not discard the overflow object %s; the bucket's "
            "lifecycle rule on the prefix is the backstop", key, exc_info=True,
        )
    return data


def _checked_key(key: Any, function: str) -> str:
    key = str(key or "")
    prefix = overflow_settings()["PREFIX"]
    bad = (
        not key
        or len(key) > MAX_KEY_LENGTH
        or key.startswith("/")
        or ".." in key
        or "://" in key
        or "\\" in key
        or not key.startswith(f"{prefix}/")
    )
    if bad:
        raise FunctionReferenceError(
            f"function '{function}': refusing to resolve the reference key "
            f"{key[:120]!r} — a key must sit under this deployment's "
            f'STAPEL_COMM["LARGE_REPLY"]["PREFIX"] ({prefix!r}) and must not '
            "climb out of it. A provider cannot name an arbitrary object in "
            "our bucket."
        )
    return key
