"""The revocation escape hatch has to announce itself.

Both blacklists fail closed and both read one setting to be talked out of it.
A deployment that flipped that setting during an incident and never flipped it
back is indistinguishable, at runtime, from one that never had it — so the
boot smoke says it out loud.
"""
from django.test import override_settings

from stapel_core.django.blacklist_checks import (
    W001_BLACKLIST_FAIL_OPEN,
    W002_BLACKLIST_LOCMEM,
    check_blacklist_fail_open,
    check_blacklist_store_is_shared,
)

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
REDIS = {"default": {"BACKEND": "django_redis.cache.RedisCache",
                     "LOCATION": "redis://cache:6379/0"}}


def test_default_is_silent():
    """Failing closed is the default, and defaults do not need announcing."""
    assert check_blacklist_fail_open() == []


@override_settings(STAPEL_BLACKLIST_FAIL_OPEN=False)
def test_explicitly_closed_is_silent():
    assert check_blacklist_fail_open() == []


@override_settings(STAPEL_BLACKLIST_FAIL_OPEN=True)
def test_fail_open_is_reported():
    errors = check_blacklist_fail_open()
    assert [e.id for e in errors] == [W001_BLACKLIST_FAIL_OPEN]
    assert errors[0].level < 40  # Warning, not Error: it is a legitimate stance


# ---------------------------------------------------------------------------
# W002 — a per-process store makes revocation per-process.
# ---------------------------------------------------------------------------
#
# Revocation now runs inside the validation seam (0.25.0), so this stopped
# being trivia: on LocMem, a logout revokes the token in the worker that
# served it and nowhere else. Every other worker keeps admitting it.


@override_settings(CACHES=LOCMEM, DEBUG=False)
def test_locmem_outside_debug_is_reported():
    warnings = check_blacklist_store_is_shared()
    assert [w.id for w in warnings] == [W002_BLACKLIST_LOCMEM]
    assert warnings[0].level < 40  # Warning: a single-worker box may mean it


@override_settings(CACHES=LOCMEM, DEBUG=True)
def test_locmem_under_debug_is_silent():
    """Development and this very test suite run on LocMem legitimately.

    A finding that fires on every dev boot is a finding people learn to
    scroll past — which is how the real one gets missed.
    """
    assert check_blacklist_store_is_shared() == []


@override_settings(CACHES=REDIS, DEBUG=False)
def test_shared_store_is_silent():
    assert check_blacklist_store_is_shared() == []


@override_settings(CACHES={}, DEBUG=False)
def test_no_cache_configured_is_silent():
    """Django's own default is LocMem, but an absent CACHES says nothing.

    The check reports a stated backend it can read, never a guess — a gate
    that guesses is a gate that lies about its cause.
    """
    assert check_blacklist_store_is_shared() == []
