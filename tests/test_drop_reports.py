"""Every removal in this package measures what it did, in one vocabulary.

0.46.0 gave the verification stores three terminal verbs that report a
``DropReport`` instead of returning ``None``, and its audit named six more
removals with the same shape, deliberately left for their own release. This
file is that release's evidence.

The shape, precisely
--------------------
It is NOT "the function returned nothing". Django's ``cache.delete`` returns
``False``, not ``None`` — **so it was never the absence of a return value that
hid the defect. The return was a truthful answer about a key the module never
writes, which is no evidence at all about the record the caller meant.** A
truthful answer to the wrong question is worse than silence, because it looks
like information.

Four of the six said even less than that: ``lift_tombstone``,
``unblacklist_user``, ``remove_from_blacklist`` and ``clear_all`` returned
``True`` for "the call did not raise" — one value for "removed it", "there was
nothing there" and "it is still readable".

So every test below is written in the shape that made the old return useless:
a record written under ONE namespace and dropped under another (two services,
or two fleets, sharing one store), or a store that cannot answer at all. On
0.46.0 each of these calls returns ``True`` or ``None`` and the assertions
cannot be written — which is the point.

Log capture is done by attaching a handler to the module's own logger, not
with ``caplog``: whether caplog observes anything depends on the host's
LOGGING config, and a log assertion that silently observes nothing is the same
genre of defect as the delete under test. (The same reasoning, and the same
helper, as ``tests/test_challenge_drop.py``.)
"""
import logging
import uuid

import pytest
from django.core.cache import cache as plain_cache
from django.test import override_settings

from stapel_core.core.drop import DropOutcome, DropReport
from stapel_core.core.revocation_store import (
    reset_revocation_cache,
    revocation_cache,
)
from stapel_core.core.token_blacklist import TokenBlacklist
from stapel_core.django.jwt.authentication import (
    blacklist_user,
    is_user_blacklisted,
    unblacklist_user,
)
from stapel_core.django.jwt.tombstone import (
    is_user_tombstoned,
    lift_tombstone,
    tombstone_user,
)
from stapel_core.django.mandate import _cache_key, invalidate_mandate_cache
from stapel_core.django.workspaces import invalidate_membership_cache
from stapel_core.verification.codes import OneTimeCodeStore, StoreUnavailable
from stapel_core.verification.policy import POLICY_KEY, invalidate_policy_cache

LOCMEM = "django.core.cache.backends.locmem.LocMemCache"

#: One store, several services. LocMemCache keyed on LOCATION shares its
#: backing dict between instances, which is exactly the "one Redis" of the
#: defect these namespaces exist to survive.
SHARED_LOCATION = "drop-report-shared-store"


def _service(key_prefix):
    return {
        "default": {
            "BACKEND": LOCMEM,
            "LOCATION": SHARED_LOCATION,
            "KEY_PREFIX": key_prefix,
        }
    }


AUTH_SERVICE = _service("auth")
PEER_SERVICE = _service("stapel_profiles")


@pytest.fixture(autouse=True)
def _clean_shared_store():
    reset_revocation_cache()
    with override_settings(CACHES=AUTH_SERVICE):
        revocation_cache().clear()
    reset_revocation_cache()
    yield
    reset_revocation_cache()


def _capture(logger_name, level=logging.WARNING):
    """Collect records straight off a module's logger.

    Not ``caplog`` — see the module docstring.
    """

    class _Ctx:
        def __enter__(self):
            self.records: list[logging.LogRecord] = []
            self.handler = logging.Handler()
            self.handler.emit = self.records.append
            self.logger = logging.getLogger(logger_name)
            self.previous = self.logger.level
            self.logger.setLevel(level)
            self.logger.addHandler(self.handler)
            return self.records

        def __exit__(self, *exc):
            self.logger.removeHandler(self.handler)
            self.logger.setLevel(self.previous)
            return False

    return _Ctx()


class _DeadCache:
    """A backend that cannot answer anything. Not an empty one."""

    def get(self, key, default=None):
        raise RuntimeError("store down")

    def set(self, *a, **kw):
        raise RuntimeError("store down")

    def delete(self, key):
        raise RuntimeError("store down")

    def clear(self):
        raise RuntimeError("store down")


# ---------------------------------------------------------------------------
# 1. lift_tombstone — the costliest of the six.
# ---------------------------------------------------------------------------

class TestLiftTombstoneMeasures:
    """The operator here is restoring a WRONGLY DELETED user.

    ``True`` meaning "did not raise" leaves that person locked out of every
    consumer-mode service in the fleet with nothing anywhere saying so.
    """

    def test_a_lift_against_the_wrong_fleet_namespace_reports_not_found(self):
        uid = str(uuid.uuid4())
        with override_settings(STAPEL_JWT_REVOCATION_NAMESPACE="fleet-a"):
            assert tombstone_user(uid) is True
            assert is_user_tombstoned(uid) is True

        reset_revocation_cache()
        with override_settings(STAPEL_JWT_REVOCATION_NAMESPACE="fleet-b"):
            report = lift_tombstone(uid)

        assert report.outcome is DropOutcome.NOT_FOUND, (
            "a lift that lifted nothing reported success — and the user is "
            "still deleted everywhere"
        )
        assert not report
        assert report.namespace == "fleet-b"
        assert report.what == "deletion tombstone"

        # The person really is still locked out. On 0.46.0 the operator was
        # told otherwise by a `True`.
        reset_revocation_cache()
        with override_settings(STAPEL_JWT_REVOCATION_NAMESPACE="fleet-a"):
            assert is_user_tombstoned(uid) is True

    def test_the_miss_is_logged_even_if_the_operator_ignores_the_report(self):
        uid = str(uuid.uuid4())
        with override_settings(STAPEL_JWT_REVOCATION_NAMESPACE="fleet-a"):
            tombstone_user(uid)

        reset_revocation_cache()
        with override_settings(STAPEL_JWT_REVOCATION_NAMESPACE="fleet-b"):
            with _capture("stapel_core.django.jwt.tombstone") as records:
                lift_tombstone(uid)

        assert records, "a lift that found nothing said nothing"
        assert "STAPEL_JWT_REVOCATION_NAMESPACE" in records[0].getMessage()

    def test_a_real_lift_is_dropped_and_truthy(self):
        uid = str(uuid.uuid4())
        tombstone_user(uid)
        report = lift_tombstone(uid)
        assert report and report.outcome is DropOutcome.DROPPED
        assert is_user_tombstoned(uid) is False

    def test_an_unreachable_store_is_unavailable_not_absent(self, monkeypatch):
        monkeypatch.setattr(
            "stapel_core.core.revocation_store.revocation_cache",
            lambda: _DeadCache(),
        )
        report = lift_tombstone("anyone")
        assert report.outcome is DropOutcome.UNAVAILABLE
        assert not report
        assert report.error


# ---------------------------------------------------------------------------
# 2. unblacklist_user — the concern blacklist_user documented, carried over.
# ---------------------------------------------------------------------------

class TestUnblacklistUserMeasures:
    def test_an_unban_in_the_wrong_namespace_reports_not_found(self):
        uid = str(uuid.uuid4())
        with override_settings(STAPEL_JWT_REVOCATION_NAMESPACE="fleet-a"):
            assert blacklist_user(uid, ttl=3600) is True

        reset_revocation_cache()
        with override_settings(STAPEL_JWT_REVOCATION_NAMESPACE="fleet-b"):
            report = unblacklist_user(uid)

        assert report.outcome is DropOutcome.NOT_FOUND
        assert not report
        assert report.what == "user ban"

        reset_revocation_cache()
        with override_settings(STAPEL_JWT_REVOCATION_NAMESPACE="fleet-a"):
            assert is_user_blacklisted(uid) is True, (
                "the ban is still in force while the operator was told it was "
                "lifted"
            )

    def test_a_real_unban_is_dropped(self):
        uid = str(uuid.uuid4())
        blacklist_user(uid, ttl=3600)
        assert unblacklist_user(uid)
        assert is_user_blacklisted(uid) is False


# ---------------------------------------------------------------------------
# 3. TokenBlacklist.remove_from_blacklist / clear_all.
# ---------------------------------------------------------------------------

class TestTokenBlacklistRemovalMeasures:
    def test_removing_a_revocation_written_by_a_peer_fleet_reports_not_found(self):
        from datetime import timedelta

        jti = str(uuid.uuid4())
        with override_settings(STAPEL_JWT_REVOCATION_NAMESPACE="fleet-a"):
            assert TokenBlacklist().blacklist_token(jti, timedelta(hours=1)) is True

        reset_revocation_cache()
        with override_settings(STAPEL_JWT_REVOCATION_NAMESPACE="fleet-b"):
            report = TokenBlacklist().remove_from_blacklist(jti)

        assert report.outcome is DropOutcome.NOT_FOUND
        assert report.what == "token revocation"

        reset_revocation_cache()
        with override_settings(STAPEL_JWT_REVOCATION_NAMESPACE="fleet-a"):
            assert TokenBlacklist().is_blacklisted(jti) is True

    def test_a_real_removal_is_dropped(self):
        from datetime import timedelta

        jti = str(uuid.uuid4())
        bl = TokenBlacklist()
        bl.blacklist_token(jti, timedelta(hours=1))
        assert bl.remove_from_blacklist(jti)
        assert bl.is_blacklisted(jti) is False

    def test_clear_all_measures_the_clear_with_a_probe(self):
        from datetime import timedelta

        bl = TokenBlacklist()
        bl.blacklist_token(str(uuid.uuid4()), timedelta(hours=1))
        report = bl.clear_all()
        assert report and report.outcome is DropOutcome.DROPPED

    def test_a_clear_that_does_nothing_is_not_reported_as_success(self, monkeypatch):
        """The proof for ``clear_all``: a backend whose ``clear()`` is a no-op.

        There is no key to read back after a clear, so ``True`` for "did not
        raise" was the most comforting of the six lies — on this backend it
        was returned while nothing at all had been cleared. The probe is what
        makes the difference visible.
        """

        class _StubbornCache:
            def __init__(self):
                self.store = {}

            def get(self, key, default=None):
                return self.store.get(key, default)

            def set(self, key, value, *a, **kw):
                self.store[key] = value

            def delete(self, key):
                self.store.pop(key, None)

            def clear(self):
                pass  # accepted, ignored

        stubborn = _StubbornCache()
        monkeypatch.setattr(
            "stapel_core.core.token_blacklist.revocation_cache", lambda: stubborn
        )
        with _capture("stapel_core.core.token_blacklist", logging.ERROR) as records:
            report = TokenBlacklist().clear_all()

        assert report.outcome is DropOutcome.STILL_PRESENT
        assert not report
        assert "still readable" in records[0].getMessage()

    def test_a_store_that_does_not_retain_cannot_have_a_clear_measured(
        self, monkeypatch
    ):
        """A dummy backend: ``clear()`` cannot be verified there at all."""

        class _DummyCache:
            def get(self, key, default=None):
                return default

            def set(self, *a, **kw):
                pass

            def delete(self, key):
                pass

            def clear(self):
                pass

        monkeypatch.setattr(
            "stapel_core.core.token_blacklist.revocation_cache", lambda: _DummyCache()
        )
        report = TokenBlacklist().clear_all()
        assert report.outcome is DropOutcome.UNAVAILABLE
        assert not report


# ---------------------------------------------------------------------------
# 4. OneTimeCodeStore.discard / unblock — in the module that wrote the rule.
# ---------------------------------------------------------------------------

class TestOneTimeCodeDropsMeasure:
    """"Absence and wrongness are different facts" — the module's own header.

    Its drop verbs collapsed three facts into ``None``, and ``discard()``
    swallowed :class:`StoreUnavailable` besides.
    """

    def test_a_discard_against_a_dead_store_is_unavailable_not_done(
        self, monkeypatch
    ):
        store = OneTimeCodeStore("otp_email")
        store.issue("a@example.com", "123456", ttl=300, max_attempts=3)

        monkeypatch.setattr(
            "stapel_core.verification.codes._cache", lambda: _DeadCache()
        )
        with _capture("stapel_core.verification.codes", logging.ERROR) as records:
            report = store.discard("a@example.com")

        assert report.outcome is DropOutcome.UNAVAILABLE, (
            "an erasure that could not reach the store reported the same "
            "value as one that worked"
        )
        assert not report
        assert records

        # And the code really is still pending: the discard did nothing.
        monkeypatch.undo()
        assert store.check("a@example.com", "123456").outcome.value == "ok"

    def test_the_swallowed_outage_no_longer_looks_like_success(self, monkeypatch):
        """``discard`` still does not raise — but the outage is in the value."""
        store = OneTimeCodeStore("otp_email")
        monkeypatch.setattr(
            "stapel_core.verification.codes._cache", lambda: _DeadCache()
        )
        report = store.discard("nobody@example.com")  # must not raise
        assert report.outcome is DropOutcome.UNAVAILABLE
        with pytest.raises(StoreUnavailable):
            store._get("anything")  # the underlying fact, unchanged

    def test_discarding_a_code_that_is_not_there_says_so(self):
        store = OneTimeCodeStore("otp_email")
        report = store.discard("never@example.com")
        assert report.outcome is DropOutcome.NOT_FOUND
        assert report.what == "pending code"

    def test_a_real_discard_is_dropped(self):
        store = OneTimeCodeStore("otp_email")
        store.issue("b@example.com", "654321", ttl=300, max_attempts=3)
        assert store.discard("b@example.com")
        assert store.check("b@example.com", "654321").outcome.value == "not_found"

    def test_unblock_reports_whether_a_block_was_lifted(self):
        store = OneTimeCodeStore("otp_email")
        assert store.unblock("c@example.com").outcome is DropOutcome.NOT_FOUND
        store.block("c@example.com", 60)
        report = store.unblock("c@example.com")
        assert report and report.what == "code block"
        assert store.blocked_for("c@example.com") == 0

    def test_the_report_names_the_store_as_service_local(self):
        """The documented decision, asserted: this store is not fleet-shared."""
        store = OneTimeCodeStore("otp_email")
        with override_settings(CACHES=AUTH_SERVICE):
            report = store.discard("d@example.com")
        assert report.namespace == "service-local:auth"


# ---------------------------------------------------------------------------
# 5. invalidate_mandate_cache — a cache over an AUTHORIZATION answer.
# ---------------------------------------------------------------------------

class TestMandateInvalidationMeasures:
    def test_a_peer_that_never_cached_the_answer_reports_not_found(self):
        """Two services, one store: each holds its own copy under its prefix.

        The invalidation reaches every peer as a broadcast, and each peer drops
        its OWN entry — so a ``NOT_FOUND`` in one is normal and a ``DROPPED``
        in it says nothing about the others. That scope was invisible while
        this returned ``None``.
        """
        user_id = str(uuid.uuid4())
        with override_settings(CACHES=AUTH_SERVICE):
            plain_cache.set(_cache_key(user_id), True, 30)
            assert plain_cache.get(_cache_key(user_id)) is True

        with override_settings(CACHES=PEER_SERVICE):
            report = invalidate_mandate_cache(user_id)

        assert report.outcome is DropOutcome.NOT_FOUND
        assert report.namespace == "service-local:stapel_profiles"
        assert report.what == "cached mandate answer"

        with override_settings(CACHES=AUTH_SERVICE):
            assert plain_cache.get(_cache_key(user_id)) is True, (
                "one service's invalidation is not another's"
            )
            assert invalidate_mandate_cache(user_id)

    def test_the_broadcast_path_keeps_an_ordinary_absence_quiet(self):
        """A warning per revocation event teaches the reader to ignore it."""
        with _capture("stapel_core.django.mandate") as records:
            invalidate_mandate_cache(str(uuid.uuid4()), absence_is_normal=True)
        assert records == []

        with _capture("stapel_core.django.mandate") as records:
            invalidate_mandate_cache(str(uuid.uuid4()))
        assert records, "an operator's invalidation that found nothing was silent"


# ---------------------------------------------------------------------------
# 6. invalidate_policy_cache — the one whose namespace move is DEFERRED.
# ---------------------------------------------------------------------------

class TestPolicyInvalidationMeasures:
    def test_the_report_shows_the_reach_this_call_actually_has(self):
        """The known gap, made visible instead of implied.

        Every other part of ``stapel_core.verification`` moved onto the
        fleet-wide namespace in 0.45.0; the policy cache did not. So the auth
        service invalidating a policy leaves the peer that ENFORCES it serving
        the stale answer for ``POLICY_CACHE_TTL``. Moving it is a wire format
        between peers, exactly as ``GRANT_NAMESPACE`` was, so it ships in its
        own release — but the scope stops being invisible here.
        """
        user_id = str(uuid.uuid4())
        key = POLICY_KEY.format(user_id=user_id)
        policy = {"disabled_scopes": ["payout"], "enabled_scopes": []}

        with override_settings(CACHES=PEER_SERVICE):
            plain_cache.set(key, policy, 60)

        with override_settings(CACHES=AUTH_SERVICE):
            report = invalidate_policy_cache(user_id)

        assert report.outcome is DropOutcome.NOT_FOUND
        assert report.namespace == "service-local:auth"
        assert report.what == "cached verification policy"

        with override_settings(CACHES=PEER_SERVICE):
            assert plain_cache.get(key) == policy, (
                "the enforcing peer still holds the stale policy — which is "
                "the deferred defect, now visible in the report"
            )

    def test_it_still_invalidates_locally_and_says_it_did(self):
        user_id = str(uuid.uuid4())
        key = POLICY_KEY.format(user_id=user_id)
        plain_cache.set(key, {"disabled_scopes": [], "enabled_scopes": []}, 60)
        assert invalidate_policy_cache(user_id)
        assert plain_cache.get(key) is None


# ---------------------------------------------------------------------------
# 7. The same shape found in the workspaces client during this sweep.
# ---------------------------------------------------------------------------

class TestMembershipInvalidationMeasures:
    def test_it_reports_the_per_service_scope_of_what_it_dropped(self):
        ws_id, user_id = uuid.uuid4(), uuid.uuid4()
        from stapel_core.django.workspaces import _cache_key as membership_key

        with override_settings(CACHES=AUTH_SERVICE):
            plain_cache.set(membership_key(ws_id, user_id), "admin", 30)
            report = invalidate_membership_cache(ws_id, user_id)
            assert report and report.namespace == "service-local:auth"
            assert plain_cache.get(membership_key(ws_id, user_id)) is None

        with override_settings(CACHES=PEER_SERVICE):
            assert (
                invalidate_membership_cache(ws_id, user_id).outcome
                is DropOutcome.NOT_FOUND
            )


# ---------------------------------------------------------------------------
# One vocabulary, not one per module.
# ---------------------------------------------------------------------------

def test_every_removal_verb_speaks_the_same_drop_vocabulary():
    """A second enum with overlapping members would fold the facts again.

    ``stapel_core.verification`` re-exports the same classes rather than
    defining its own, so a consumer that learned ``DropOutcome`` from the
    0.46.0 verbs can use it on all of them.
    """
    import stapel_core.core.drop as canon
    import stapel_core.verification as verification

    assert verification.DropOutcome is canon.DropOutcome
    assert verification.DropReport is canon.DropReport

    store = OneTimeCodeStore("otp_email")
    reports = [
        lift_tombstone("nobody"),
        unblacklist_user("nobody"),
        TokenBlacklist().remove_from_blacklist("nothing"),
        TokenBlacklist().clear_all(),
        store.discard("nobody@example.com"),
        store.unblock("nobody@example.com"),
        invalidate_mandate_cache("nobody"),
        invalidate_policy_cache("nobody"),
        invalidate_membership_cache(uuid.uuid4(), uuid.uuid4()),
    ]
    assert all(isinstance(r, DropReport) for r in reports)
    assert all(isinstance(r.outcome, canon.DropOutcome) for r in reports)


def test_a_drop_report_is_falsy_unless_something_was_dropped():
    """``assert lift_tombstone(uid)`` has to be a real assertion.

    A bare ``str``-Enum is truthy on every member, including the ones these
    verbs exist to expose.
    """
    for outcome in DropOutcome:
        report = DropReport(outcome, "thing", "k", "ns")
        assert bool(report) is (outcome is DropOutcome.DROPPED)
