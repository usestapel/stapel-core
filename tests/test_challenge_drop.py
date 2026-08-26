"""A delete that removed nothing must not look like a delete that worked.

The defect, as it actually shipped
----------------------------------
``stapel_core.verification`` could create a challenge, read it and complete
it, and could not *drop* one. So a consumer testing the expired-challenge path
reached around the module: stapel-auth 0.28.0 simulated expiry by deleting the
key through Django's plain ``django.core.cache.cache``.

That worked by coincidence until core 0.45.0 moved challenges, grants and
tokens onto the fleet-wide namespace (``core/fleet_cache.py``). From then on
the plain-cache delete computed ``<service>:1:stapel:verification:challenge:…``
while the module read ``stapel_verification:1:…`` — it removed nothing,
returned ``None``, and was indistinguishable from a delete that worked. The
test's setup silently did nothing and the assertion "verified" an emptiness
that was never created. The release died on it.

So the lesson under test here is not "there is a delete function now". It is
that the drop REPORTS, and that a no-op is visible in the report:
``TestTheDropIsNeverSilent`` is the test that would have caught the original
defect — it drops against a namespace the challenge was not written under and
asserts the report says ``NOT_FOUND``. On 0.45.0 this file does not import at
all — there is no primitive and no report — and the only delete available then
answers about a key the module never writes
(``test_a_plain_cache_delete_has_nothing_to_say`` keeps that call in the suite,
in the shape that shipped).
"""
import logging

import pytest
from django.core.cache import cache as plain_cache
from django.test import override_settings

from stapel_core.core.fleet_cache import reset_fleet_caches
from stapel_core.verification import (
    DropOutcome,
    DropReport,
    complete_challenge,
    create_challenge,
    drop_challenge,
    drop_verification_token,
    get_challenge,
    grant_verification,
    has_grant,
    revoke_grants,
)
from stapel_core.verification.factors import VerificationFactor, factor_registry
from stapel_core.verification.grants import CHALLENGE_KEY, GRANT_KEY

pytestmark = pytest.mark.django_db

SCOPE = "payout"
MAX_AGE = 300


class _DummyFactor(VerificationFactor):
    id = "dummy"

    def verify(self, user, challenge, payload):  # pragma: no cover - unused here
        return True


@pytest.fixture(autouse=True)
def _factor():
    factor_registry.register(_DummyFactor())
    yield
    factor_registry.clear()


@pytest.fixture(autouse=True)
def _fresh_connections():
    """The memoized fleet connection is keyed on (alias, namespace)."""
    reset_fleet_caches()
    yield
    reset_fleet_caches()


@pytest.fixture
def user(db):
    import uuid

    from stapel_core.django.users.models import User

    return User.objects.create(username=f"u_{uuid.uuid4().hex[:10]}")


def _challenge(user):
    return create_challenge(user, SCOPE, ["dummy"], MAX_AGE)


GRANTS_LOGGER = "stapel_core.verification.grants"


def _capture(level=logging.WARNING):
    """Collect records straight off the module's logger.

    Not ``caplog``: whether that sees anything depends on the host's LOGGING
    config, and a log assertion that silently observes nothing is the same
    genre of defect as the delete this file is about.
    """

    class _Ctx:
        def __enter__(self):
            self.records: list[logging.LogRecord] = []
            self.handler = logging.Handler()
            self.handler.emit = self.records.append
            self.logger = logging.getLogger(GRANTS_LOGGER)
            self.previous = self.logger.level
            self.logger.setLevel(level)
            self.logger.addHandler(self.handler)
            return self.records

        def __exit__(self, *exc):
            self.logger.removeHandler(self.handler)
            self.logger.setLevel(self.previous)
            return False

    return _Ctx()


# ---------------------------------------------------------------------------
# 1. The primitive itself.
# ---------------------------------------------------------------------------

class TestDropChallenge:
    def test_it_drops(self, user):
        challenge = _challenge(user)
        cid = challenge["challenge_id"]
        assert get_challenge(cid) is not None

        report = drop_challenge(cid)

        assert report.outcome is DropOutcome.DROPPED
        assert get_challenge(cid) is None

    def test_the_report_is_truthy_only_when_something_was_dropped(self, user):
        """`assert drop_challenge(cid)` must be a real assertion.

        A bare ``str``-Enum would not do: every member of one is truthy, so the
        obvious one-liner would pass on ``NOT_FOUND`` — the exact outcome this
        whole primitive exists to make visible.
        """
        cid = _challenge(user)["challenge_id"]
        assert drop_challenge(cid)
        assert not drop_challenge(cid)

    def test_dropping_twice_reports_the_second_as_nothing_to_drop(self, user):
        cid = _challenge(user)["challenge_id"]
        drop_challenge(cid)

        report = drop_challenge(cid)

        assert report.outcome is DropOutcome.NOT_FOUND
        assert report.what == "challenge"
        assert report.key == CHALLENGE_KEY.format(challenge_id=cid)
        assert report.namespace == "stapel_verification"

    def test_an_id_that_never_existed_is_not_found_not_an_error(self):
        assert drop_challenge("chg_never").outcome is DropOutcome.NOT_FOUND

    def test_it_carries_the_namespace_the_key_was_computed_under(self, user):
        """The first thing to compare with the writer's when a drop misses."""
        cid = _challenge(user)["challenge_id"]
        assert drop_challenge(cid).namespace == "stapel_verification"

    def test_a_store_that_does_not_obey_is_not_reported_as_success(
        self, user, monkeypatch
    ):
        """The delete ran, the record is still readable. Never ``DROPPED``."""
        challenge = _challenge(user)

        class _DisobedientCache:
            def get(self, key, default=None):
                return challenge

            def set(self, *a, **kw):  # pragma: no cover - unused
                pass

            def delete(self, key):
                return False

        monkeypatch.setattr(
            "stapel_core.verification.grants._cache", lambda: _DisobedientCache()
        )
        with _capture(logging.ERROR) as records:
            report = drop_challenge(challenge["challenge_id"])

        assert report.outcome is DropOutcome.STILL_PRESENT
        assert not report
        assert "still readable" in records[0].getMessage()


# ---------------------------------------------------------------------------
# 2. The regression proper: a no-op is visible, not silent.
# ---------------------------------------------------------------------------

class TestTheDropIsNeverSilent:
    """The test that would have caught the stapel-auth 0.28.0 defect."""

    def test_a_drop_on_a_mismatched_namespace_reports_not_found(self, user):
        """Write under one fleet namespace, drop under another.

        This is the shape of the original defect reduced to its cause: the
        deleter computing a different key from the writer. A ``NOT_FOUND``
        report is the difference between a setup that failed loudly and one
        that quietly did nothing while the assertions went green.
        """
        with override_settings(STAPEL_VERIFICATION={"GRANT_NAMESPACE": "fleet-a"}):
            cid = _challenge(user)["challenge_id"]
            assert get_challenge(cid) is not None

        reset_fleet_caches()
        with override_settings(STAPEL_VERIFICATION={"GRANT_NAMESPACE": "fleet-b"}):
            report = drop_challenge(cid)

        assert report.outcome is DropOutcome.NOT_FOUND, (
            "a drop that removed nothing reported success — the exact defect"
        )
        assert not report
        assert report.namespace == "fleet-b"

        # And the record really is untouched: the drop missed, and says so.
        reset_fleet_caches()
        with override_settings(STAPEL_VERIFICATION={"GRANT_NAMESPACE": "fleet-a"}):
            assert get_challenge(cid) is not None

    def test_the_miss_is_logged_even_if_the_caller_ignores_the_report(self, user):
        """A caller who drops the return value still cannot get a quiet no-op."""
        with override_settings(STAPEL_VERIFICATION={"GRANT_NAMESPACE": "fleet-a"}):
            cid = _challenge(user)["challenge_id"]

        reset_fleet_caches()
        with override_settings(STAPEL_VERIFICATION={"GRANT_NAMESPACE": "fleet-b"}):
            with _capture() as records:
                drop_challenge(cid)

        assert records, "a drop that found nothing said nothing"
        assert "GRANT_NAMESPACE" in records[0].getMessage()

    def test_a_plain_cache_delete_has_nothing_to_say(self, user):
        """The call that shipped in stapel-auth 0.28.0, verbatim in shape.

        ``django.core.cache.cache`` keys under the *service's* ``KEY_PREFIX``;
        this module reads the fleet namespace. So the delete removes nothing —
        and its ``False`` is not the useful signal it looks like: it is a
        truthful answer about a key this module never writes. Nothing in it
        distinguishes "the challenge is gone" from "you deleted somewhere
        else", which is why the consumer's test "verified" an emptiness it had
        never created. Asserted here so the reason the public primitive exists
        stays visible in the suite.
        """
        cid = _challenge(user)["challenge_id"]

        assert plain_cache.delete(CHALLENGE_KEY.format(challenge_id=cid)) is False
        assert get_challenge(cid) is not None, (
            "the plain-cache delete removed the record after all — then the "
            "defect this file documents would not have been possible"
        )

        # The public primitive, on the same challenge, in the same process.
        assert drop_challenge(cid).outcome is DropOutcome.DROPPED
        assert get_challenge(cid) is None

    def test_the_expired_challenge_path_is_now_reachable_honestly(self, user):
        """What the consumer was trying to write, written the intended way."""
        cid = _challenge(user)["challenge_id"]
        assert drop_challenge(cid), "setup failed: nothing was dropped"
        assert get_challenge(cid) is None


# ---------------------------------------------------------------------------
# 3. The sibling verbs that were silent for the same reason.
# ---------------------------------------------------------------------------

class TestRevokeGrantsReports:
    def test_one_report_per_scope_in_order(self, user):
        grant_verification(user_id=str(user.pk), scope="a", max_age=MAX_AGE)

        reports = revoke_grants(str(user.pk), ["a", "b"])

        assert [r.outcome for r in reports] == [
            DropOutcome.DROPPED,
            DropOutcome.NOT_FOUND,
        ]
        assert all(isinstance(r, DropReport) for r in reports)
        assert reports[0].key == GRANT_KEY.format(user_id=str(user.pk), scope="a")
        assert reports[0].what == "grant"

    def test_revoking_nothing_is_visible(self, user):
        """"Log out everywhere" that revoked nothing is a security event."""
        [report] = revoke_grants(str(user.pk), [SCOPE])
        assert report.outcome is DropOutcome.NOT_FOUND
        assert not report

    def test_it_still_revokes(self, user):
        grant_verification(user_id=str(user.pk), scope=SCOPE, max_age=MAX_AGE)
        assert has_grant(user, SCOPE) is True
        assert all(revoke_grants(str(user.pk), [SCOPE]))
        assert has_grant(user, SCOPE) is False


class TestDropVerificationToken:
    def test_revoke_grants_does_not_reach_the_token(self, user):
        """Documented, not incidental — the token is keyed by itself."""
        token = complete_challenge(_challenge(user))
        revoke_grants(str(user.pk), [SCOPE])

        assert has_grant(user, SCOPE) is False
        assert has_grant(user, SCOPE, token=token) is True

    def test_dropping_the_token_closes_it(self, user):
        token = complete_challenge(_challenge(user))
        revoke_grants(str(user.pk), [SCOPE])

        report = drop_verification_token(token)

        assert report.outcome is DropOutcome.DROPPED
        assert report.what == "token"
        assert has_grant(user, SCOPE, token=token) is False

    def test_a_token_nobody_minted_is_not_found(self):
        assert drop_verification_token("vt_nope").outcome is DropOutcome.NOT_FOUND


# ---------------------------------------------------------------------------
# 4. The seam is public — that is the whole point.
# ---------------------------------------------------------------------------

def test_the_drop_verbs_are_on_the_package_surface():
    """A consumer must never have to import ``grants._cache`` again."""
    import stapel_core.verification as verification

    for name in (
        "drop_challenge",
        "drop_verification_token",
        "revoke_grants",
        "DropOutcome",
        "DropReport",
    ):
        assert name in verification.__all__
        assert hasattr(verification, name)


def test_every_record_this_module_creates_has_a_public_verb_that_removes_it():
    """The gap audit, as an assertion.

    ``create_challenge`` / ``complete_challenge`` write exactly three kinds of
    record. If a fourth is ever added, this list stops matching the key
    constants and the next consumer inherits the same private-only seam.
    """
    from stapel_core.verification import grants

    key_constants = {
        name for name in vars(grants) if name.endswith("_KEY") and name.isupper()
    }
    assert key_constants == {"CHALLENGE_KEY", "GRANT_KEY", "TOKEN_KEY"}
