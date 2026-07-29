"""The canonicalization contract of ``stapel_core.hashing``.

Every test here pins one degree of freedom that ``json.dumps`` would otherwise
leave open. They read as trivia until you remember what they buy: a digest that
survives being computed by a different process, on a different machine, months
apart. A "fix" that loosens any of them does not fail loudly — it silently
invalidates every version key already stored.
"""

import pytest

from stapel_core.hashing import (
    HASH_PREFIX,
    canonical_hash,
    canonical_json,
    is_canonical_hash,
)


def test_key_order_is_not_content():
    """Two dicts built in different orders are the same content."""
    assert canonical_hash({"a": 1, "b": 2}) == canonical_hash({"b": 2, "a": 1})


def test_nesting_is_sorted_too():
    """sort_keys applies all the way down, not just at the top level."""
    assert canonical_hash({"x": {"a": 1, "b": 2}}) == canonical_hash({"x": {"b": 2, "a": 1}})


def test_list_order_is_content():
    """Sequence order is meaning — a reordered transcript is a different one."""
    assert canonical_hash([1, 2]) != canonical_hash([2, 1])


def test_no_incidental_whitespace():
    assert canonical_json({"a": 1, "b": [1, 2]}) == '{"a":1,"b":[1,2]}'


def test_non_ascii_is_not_escaped():
    """`ensure_ascii=True` would hash the escape sequence, not the text."""
    assert canonical_json({"k": "привет"}) == '{"k":"привет"}'


def test_digest_carries_its_algorithm():
    digest = canonical_hash({})
    assert digest.startswith(HASH_PREFIX)
    assert len(digest) == len(HASH_PREFIX) + 64


def test_frozen_vector():
    """A stored digest for a fixed payload.

    If this changes, every version key written by an earlier release stopped
    matching its own content — which downstream reads as "the source changed"
    for artifacts that never moved.
    """
    assert canonical_hash({"b": [1, 2], "a": "привет"}) == (
        "sha256:9a9c694cbebdf0552491b1be6691b0c49e4e539f6fe19c69bf499dea561fcf29"
    )


def test_unserializable_input_fails_loudly():
    """A `default=str` fallback here would hash repr output.

    Then a value whose repr changed (but whose meaning did not) would look
    like changed content, and the version key would lie in the direction that
    is hardest to notice: falsely stale.
    """
    from uuid import uuid4

    with pytest.raises(TypeError):
        canonical_hash({"id": uuid4()})


@pytest.mark.parametrize(
    "value",
    [
        "",
        None,
        "deadbeef",
        "sha256:",
        "sha256:not-hex-not-hex-not-hex-not-hex-not-hex-not-hex-not-hex-not-hexx",
        "md5:" + "0" * 64,
        "sha256:" + "0" * 63,
        "sha256:" + "A" * 64,  # upper-case hex is not what we emit
    ],
)
def test_rejects_things_that_are_not_our_digests(value):
    assert not is_canonical_hash(value)


def test_accepts_its_own_output():
    assert is_canonical_hash(canonical_hash({"any": "payload"}))
