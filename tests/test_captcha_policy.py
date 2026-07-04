"""Tests for stapel_core.captcha.policy — levels, matrix, overrides, seam."""
from unittest import mock

import pytest
from django.test import override_settings

from stapel_core.captcha.policy import (
    CHALLENGE_LEVELS,
    ChallengePolicy,
    DEFAULT_CHALLENGE_MATRIX,
    LEVEL_BLOCK,
    LEVEL_INTERACTIVE,
    LEVEL_INTERACTIVE_RATELIMIT,
    LEVEL_INVISIBLE,
    LEVEL_NONE,
    MatrixChallengePolicy,
    bump_level,
    get_challenge_policy,
    level_gte,
    level_index,
)
from stapel_core.netintel import IpProfile


class _Req:
    def __init__(self, ip="192.0.2.1"):
        self.META = {"REMOTE_ADDR": ip}


def _with_kind(kind):
    return mock.patch(
        "stapel_core.netintel.classify_ip",
        return_value=IpProfile(ip="192.0.2.1", kind=kind),
    )


# ---------------------------------------------------------------------------
# Level ordering helpers
# ---------------------------------------------------------------------------


def test_levels_are_ordered():
    assert CHALLENGE_LEVELS == (
        "none", "invisible", "interactive", "interactive+ratelimit", "block",
    )
    assert level_index(LEVEL_NONE) < level_index(LEVEL_INVISIBLE)
    assert level_index(LEVEL_INVISIBLE) < level_index(LEVEL_INTERACTIVE)
    assert level_index(LEVEL_INTERACTIVE) < level_index(LEVEL_INTERACTIVE_RATELIMIT)
    assert level_index(LEVEL_INTERACTIVE_RATELIMIT) < level_index(LEVEL_BLOCK)


def test_level_gte():
    assert level_gte(LEVEL_BLOCK, LEVEL_NONE)
    assert level_gte(LEVEL_INTERACTIVE, LEVEL_INTERACTIVE)
    assert not level_gte(LEVEL_INVISIBLE, LEVEL_INTERACTIVE)


def test_level_index_unknown_raises():
    with pytest.raises(ValueError, match="unknown challenge level"):
        level_index("extreme")


def test_bump_level_saturates_at_block():
    assert bump_level(LEVEL_INVISIBLE) == LEVEL_INTERACTIVE
    assert bump_level(LEVEL_INTERACTIVE_RATELIMIT) == LEVEL_BLOCK
    assert bump_level(LEVEL_BLOCK) == LEVEL_BLOCK
    assert bump_level(LEVEL_NONE, steps=10) == LEVEL_BLOCK


# ---------------------------------------------------------------------------
# MatrixChallengePolicy — default matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind,expected", [
    ("residential", LEVEL_INVISIBLE),
    ("unknown", LEVEL_INVISIBLE),
    ("datacenter", LEVEL_INTERACTIVE),
    ("vpn", LEVEL_INTERACTIVE),
    ("tor", LEVEL_INTERACTIVE_RATELIMIT),
])
def test_default_matrix(kind, expected):
    assert DEFAULT_CHALLENGE_MATRIX[kind] == expected
    with _with_kind(kind):
        assert MatrixChallengePolicy().level_for(_Req(), "default") == expected


def test_unconfigured_netintel_means_invisible():
    # NullProvider (the default) → unknown → invisible: pre-policy behavior.
    assert MatrixChallengePolicy().level_for(_Req(), "register") == LEVEL_INVISIBLE


def test_matrix_override_merges_over_defaults():
    with override_settings(STAPEL_CAPTCHA={
        "CHALLENGE_MATRIX": {"tor": LEVEL_BLOCK},
    }):
        with _with_kind("tor"):
            assert MatrixChallengePolicy().level_for(_Req(), "x") == LEVEL_BLOCK
        with _with_kind("datacenter"):  # untouched kinds keep their default
            assert MatrixChallengePolicy().level_for(_Req(), "x") == LEVEL_INTERACTIVE


def test_unlisted_kind_falls_back_to_unknown_row():
    with _with_kind("something-new"):
        assert MatrixChallengePolicy().level_for(_Req(), "x") == LEVEL_INVISIBLE


def test_invalid_matrix_value_falls_back_to_invisible(caplog):
    with override_settings(STAPEL_CAPTCHA={
        "CHALLENGE_MATRIX": {"datacenter": "nuclear"},
    }):
        with _with_kind("datacenter"):
            with caplog.at_level("WARNING", logger="stapel_core.captcha.policy"):
                assert MatrixChallengePolicy().level_for(_Req(), "x") == LEVEL_INVISIBLE
    assert "unknown level" in caplog.text


# ---------------------------------------------------------------------------
# ACTION_OVERRIDES
# ---------------------------------------------------------------------------


def test_action_override_plus_one_bumps_one_level():
    with override_settings(STAPEL_CAPTCHA={"ACTION_OVERRIDES": {"register": "+1"}}):
        policy = MatrixChallengePolicy()
        with _with_kind("residential"):
            assert policy.level_for(_Req(), "register") == LEVEL_INTERACTIVE
            assert policy.level_for(_Req(), "login") == LEVEL_INVISIBLE
        with _with_kind("tor"):  # bump saturates the ladder correctly
            assert policy.level_for(_Req(), "register") == LEVEL_BLOCK


def test_action_override_kind_map():
    with override_settings(STAPEL_CAPTCHA={
        "ACTION_OVERRIDES": {"payout": {"vpn": LEVEL_BLOCK, "residential": LEVEL_NONE}},
    }):
        policy = MatrixChallengePolicy()
        with _with_kind("vpn"):
            assert policy.level_for(_Req(), "payout") == LEVEL_BLOCK
        with _with_kind("residential"):
            assert policy.level_for(_Req(), "payout") == LEVEL_NONE
        with _with_kind("datacenter"):  # kind not in the map → matrix level
            assert policy.level_for(_Req(), "payout") == LEVEL_INTERACTIVE


def test_action_override_kind_map_plus_one():
    with override_settings(STAPEL_CAPTCHA={
        "ACTION_OVERRIDES": {"register": {"datacenter": "+1"}},
    }):
        with _with_kind("datacenter"):
            assert MatrixChallengePolicy().level_for(_Req(), "register") == \
                LEVEL_INTERACTIVE_RATELIMIT


# ---------------------------------------------------------------------------
# CHALLENGE_POLICY seam
# ---------------------------------------------------------------------------


class _EverythingBlocked(ChallengePolicy):
    def level_for(self, request, action):
        return LEVEL_BLOCK


def test_default_policy_is_matrix():
    assert isinstance(get_challenge_policy(), MatrixChallengePolicy)


def test_policy_swap_via_class():
    with override_settings(STAPEL_CAPTCHA={"CHALLENGE_POLICY": _EverythingBlocked}):
        policy = get_challenge_policy()
    assert isinstance(policy, _EverythingBlocked)
    assert policy.level_for(_Req(), "any") == LEVEL_BLOCK


def test_policy_swap_via_instance():
    instance = _EverythingBlocked()
    with override_settings(STAPEL_CAPTCHA={"CHALLENGE_POLICY": instance}):
        assert get_challenge_policy() is instance


def test_policy_swap_via_dotted_path():
    with override_settings(STAPEL_CAPTCHA={
        "CHALLENGE_POLICY": "stapel_core.captcha.policy.MatrixChallengePolicy",
    }):
        assert isinstance(get_challenge_policy(), MatrixChallengePolicy)


def test_broken_policy_path_fails_open_to_matrix(caplog):
    with override_settings(STAPEL_CAPTCHA={"CHALLENGE_POLICY": "no.such.Policy"}):
        with caplog.at_level("WARNING", logger="stapel_core.captcha.policy"):
            policy = get_challenge_policy()
    assert isinstance(policy, MatrixChallengePolicy)
    assert "falling back to MatrixChallengePolicy" in caplog.text


def test_non_policy_value_fails_open_to_matrix():
    with override_settings(STAPEL_CAPTCHA={"CHALLENGE_POLICY": object()}):
        assert isinstance(get_challenge_policy(), MatrixChallengePolicy)
