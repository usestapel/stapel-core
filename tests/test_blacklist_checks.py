"""The revocation escape hatch has to announce itself.

Both blacklists fail closed and both read one setting to be talked out of it.
A deployment that flipped that setting during an incident and never flipped it
back is indistinguishable, at runtime, from one that never had it — so the
boot smoke says it out loud.
"""
from django.test import override_settings

from stapel_core.django.blacklist_checks import (
    W001_BLACKLIST_FAIL_OPEN,
    check_blacklist_fail_open,
)


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
