"""The retry ladder: bounded, exponential, jittered — and provably jittered.

The last property is the one that gets skipped, and it is the one that
matters when N workers fail against the same provider in the same second.
A test that only checks "the delay grows" passes just as happily on the
un-jittered ceiling everybody actually ships.
"""
import pytest

from stapel_core.comm.backoff import (
    DEFAULT_BASE_SECONDS,
    DEFAULT_CAP_SECONDS,
    retry_ceiling,
    retry_delay,
)


def test_ceiling_doubles_from_base():
    assert retry_ceiling(1, base=2, cap=1000) == 2
    assert retry_ceiling(2, base=2, cap=1000) == 4
    assert retry_ceiling(3, base=2, cap=1000) == 8
    assert retry_ceiling(4, base=2, cap=1000) == 16


def test_ceiling_is_capped():
    assert retry_ceiling(20, base=2, cap=300) == 300


def test_ceiling_never_overflows_on_a_nonsense_attempt_count():
    """`attempt` arrives from a DB column. 2 ** 10_000 is a real integer in
    Python and computing it before min() sees it is a real hang."""
    assert retry_ceiling(10_000, base=2, cap=300) == 300


def test_attempt_below_one_is_treated_as_the_first():
    assert retry_ceiling(0, base=5, cap=100) == 5
    assert retry_ceiling(-3, base=5, cap=100) == 5


def test_base_zero_disables_the_ladder():
    assert retry_ceiling(9, base=0) == 0.0
    assert retry_delay(9, base=0) == 0.0


def test_delay_stays_within_the_ceiling():
    for attempt in range(1, 8):
        ceiling = retry_ceiling(attempt, base=2, cap=300)
        for _ in range(50):
            assert 0.0 <= retry_delay(attempt, base=2, cap=300) <= ceiling


def test_delay_is_actually_jittered():
    """Two hundred draws from one attempt must not be one number.

    This is the assertion that fails against `time.sleep(2 ** retries)` —
    the shape duplicated in three bus backends and the one that keeps a
    failing herd synchronised.
    """
    draws = {retry_delay(3, base=2, cap=300) for _ in range(200)}
    assert len(draws) > 100, "delays are not spread — the herd stays in step"


def test_jitter_spans_the_range_rather_than_hugging_the_ceiling():
    draws = [retry_delay(4, base=2, cap=300) for _ in range(500)]
    ceiling = retry_ceiling(4, base=2, cap=300)
    assert min(draws) < ceiling * 0.2
    assert max(draws) > ceiling * 0.8
    # Full jitter's mean is half the ceiling; anything much higher means
    # somebody swapped it for "equal jitter" or for the bare ceiling.
    assert ceiling * 0.35 < (sum(draws) / len(draws)) < ceiling * 0.65


def test_defaults_are_sane():
    assert DEFAULT_BASE_SECONDS > 0
    assert DEFAULT_CAP_SECONDS >= DEFAULT_BASE_SECONDS
    assert retry_ceiling(1) == DEFAULT_BASE_SECONDS


@pytest.mark.django_db
def test_retry_delay_for_reads_the_deployment_ladder(settings):
    from stapel_core.comm.tasks import retry_delay_for

    settings.STAPEL_COMM = {
        **getattr(settings, "STAPEL_COMM", {}),
        "TASK_RETRY_BACKOFF_BASE": 10,
        "TASK_RETRY_BACKOFF_CAP": 40,
    }
    assert 0.0 <= retry_delay_for(1) <= 10
    assert 0.0 <= retry_delay_for(3) <= 40
    assert retry_delay_for(99) <= 40
