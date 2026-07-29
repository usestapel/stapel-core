"""Canonical content hashing — a stable version key for a JSON-able artifact.

The problem this solves is *version identity*: two parts of a system hold
derived work (a summary, an extraction, a user's edits) and each needs to say
which version of the source it was built from. A timestamp cannot answer that
(it moves when nothing changed), and an incrementing revision cannot either
(two writers assign the same number to different content). A hash of the
content itself can.

The canonicalization is the whole point. ``json.dumps`` has several degrees of
freedom — key order, whitespace, and whether non-ASCII is escaped — and every
one of them changes the bytes without changing the meaning. Two processes that
disagree on any of them produce different digests for identical content, which
reads downstream as "the source changed" when it did not. So the dump is
pinned:

    sort_keys=True          key order is a dict artefact, not content
    separators=(",", ":")   no incidental whitespace
    ensure_ascii=False      "привет" hashes as itself, not as \\u0440...

Digests carry their algorithm as a prefix (``sha256:<hex>``) rather than being
a bare hex string. A bare digest is un-migratable: the day another algorithm is
needed, nothing can tell an old value from a new one, and every stored key has
to be thrown away. With the prefix, both live side by side and a reader knows
which is which.

Inputs must already be JSON primitives. UUIDs, datetimes and Decimals raise
``TypeError`` here deliberately — a ``default=str`` fallback would quietly hash
``repr`` output, so a value that changed representation (but not meaning) would
look like changed content. Convert at the boundary you control: pydantic's
``model_dump(mode="json")``, ``dataclasses.asdict`` plus explicit coercion, or
a DRF serializer.

Verified interoperable: this recipe reproduces the digest recorded in an
external artifact produced by an independent implementation
(``sha256:798782c1c585d5ac0b8ad877c106f2d92a0e6f4727e538ab6583eba9db22fefb``
over a 107-segment transcript, checked 2026-07-29). The dump options above are
therefore a compatibility contract, not a preference — changing any one of them
silently invalidates every previously stored key.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

#: The digest this module produces. Part of every value it returns.
HASH_ALGORITHM = "sha256"

#: Prefix that makes the algorithm readable from the value itself.
HASH_PREFIX = f"{HASH_ALGORITHM}:"


def canonical_json(payload: Any) -> str:
    """Serialize ``payload`` to the one JSON form this module hashes.

    Exposed separately from :func:`canonical_hash` so a caller can diff two
    payloads that hash differently and see *what* differs — a digest alone
    tells you that something changed and nothing about what.

    Raises ``TypeError`` for values JSON cannot represent; see the module
    docstring for why that is not softened.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def canonical_hash(payload: Any) -> str:
    """Return ``sha256:<hex>`` over the canonical JSON form of ``payload``.

    Equal digests mean the payloads are equal as JSON content. Different
    digests mean they differ somewhere — which is exactly the guarantee a
    version key needs, and no more than that (it says nothing about *where*).
    """
    return HASH_PREFIX + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def is_canonical_hash(value: object) -> bool:
    """True when ``value`` looks like a digest this module produced.

    For validating stored keys at a trust boundary: a version key that arrives
    as ``""`` or as a bare hex string is a bug in the writer, and catching it
    on read beats comparing it against a fresh digest and concluding — wrongly,
    and silently — that the content changed.
    """
    if not isinstance(value, str) or not value.startswith(HASH_PREFIX):
        return False
    digest = value[len(HASH_PREFIX):]
    return len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
