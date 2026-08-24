"""Deactivation has to reach the peers, and a claim must not overrule it.

Measured on a deployed fleet at 0.41.0: `is_active=False` at the issuer, same
unexpired access token, immediately after — the issuer answered 401 and every
consumer-mode service answered 200. Two independent causes:

* the `is_active` claim was replayed onto the local shadow row BEFORE the
  `is_active` gate ran, so a token minted while the account was live carried
  `is_active: true`, reactivated the row, and passed the check it had just
  satisfied;
* nothing published the deactivation, so even with the ordering fixed a peer
  would learn about it only at the next mint — up to an access-token lifetime,
  or never.

0.43.0 makes the state change publish itself into the fleet-wide revocation
namespace and stops the claim from moving `is_active` at all. Unlike the
deletion tombstone, reactivation LIFTS the record: suspending and restoring an
account is an ordinary operation, not something an operator should have to
undo by hand.

Everything outside TestIssuerModeIsUnchanged fails on 0.41.0.
"""
import uuid
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

from stapel_core.core.revocation_store import reset_revocation_cache
from stapel_core.django.jwt.deactivation import (
    DEACTIVATION_PREFIX,
    connect_deactivation_broadcast,
    deactivate_user,
    deactivation_ttl,
    is_user_deactivated,
    lift_deactivation,
)
from stapel_core.django.jwt.utils import get_or_create_user_from_jwt

LOCMEM = "django.core.cache.backends.locmem.LocMemCache"
SHARED = "deactivation-shared-store"

#: Two services, one store, different cache prefixes — the 0.39.0 shape.
ISSUER = {"default": {"BACKEND": LOCMEM, "LOCATION": SHARED, "KEY_PREFIX": "auth"}}
CONSUMER = {
    "default": {"BACKEND": LOCMEM, "LOCATION": SHARED, "KEY_PREFIX": "stapel_profiles"}
}


@pytest.fixture(autouse=True)
def _clean_store():
    reset_revocation_cache()
    with override_settings(CACHES=ISSUER):
        from stapel_core.core.revocation_store import revocation_cache

        revocation_cache().clear()
    reset_revocation_cache()
    yield
    reset_revocation_cache()


@pytest.fixture
def consumer_mode():
    with override_settings(JWT_CREATE_USERS_FROM_TOKEN=True):
        yield


@pytest.fixture
def issuer_mode():
    with override_settings(JWT_CREATE_USERS_FROM_TOKEN=False):
        yield


def _claims(user):
    """What a token minted while the account was LIVE carries, forever."""
    return {
        "user_id": str(user.pk),
        "email": user.email,
        "username": user.username,
        "is_staff": False,
        "is_superuser": False,
        "is_active": True,
    }


# ---------------------------------------------------------------------------
# The state change publishes itself.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestDeactivationPublishesItself:
    def test_saving_a_user_inactive_publishes(self, issuer_mode):
        connect_deactivation_broadcast()
        user = get_user_model().objects.create(username=f"d-{uuid.uuid4().hex[:8]}")
        assert is_user_deactivated(user.pk) is False

        user.is_active = False
        user.save()

        assert is_user_deactivated(user.pk) is True

    def test_reactivation_lifts_it(self, issuer_mode):
        """The deliberate difference from the deletion tombstone."""
        connect_deactivation_broadcast()
        user = get_user_model().objects.create(username=f"d-{uuid.uuid4().hex[:8]}")
        user.is_active = False
        user.save()
        assert is_user_deactivated(user.pk) is True

        user.is_active = True
        user.save()

        assert is_user_deactivated(user.pk) is False

    def test_a_targeted_save_elsewhere_does_not_touch_it(self, issuer_mode):
        """Stamping last_login runs on every login; it must stay off this path."""
        connect_deactivation_broadcast()
        user = get_user_model().objects.create(username=f"d-{uuid.uuid4().hex[:8]}")
        user.is_active = False
        user.save()

        with patch(
            "stapel_core.django.jwt.deactivation.lift_deactivation"
        ) as lift, patch(
            "stapel_core.django.jwt.deactivation.deactivate_user"
        ) as write:
            user.last_login = None
            user.save(update_fields=["last_login"])

        lift.assert_not_called()
        write.assert_not_called()
        assert is_user_deactivated(user.pk) is True

    def test_a_targeted_save_of_the_flag_does_publish(self, issuer_mode):
        connect_deactivation_broadcast()
        user = get_user_model().objects.create(username=f"d-{uuid.uuid4().hex[:8]}")
        user.is_active = False
        user.save(update_fields=["is_active"])
        assert is_user_deactivated(user.pk) is True

    def test_connecting_twice_publishes_once(self, issuer_mode):
        connect_deactivation_broadcast()
        connect_deactivation_broadcast()
        user = get_user_model().objects.create(username=f"d-{uuid.uuid4().hex[:8]}")
        with patch(
            "stapel_core.django.jwt.deactivation.deactivate_user"
        ) as write:
            user.is_active = False
            user.save()
        assert write.call_count == 1

    def test_a_consumer_never_publishes(self, consumer_mode):
        """A peer lifting the issuer's deactivation would invert the fix."""
        connect_deactivation_broadcast()
        user = get_user_model().objects.create(username=f"d-{uuid.uuid4().hex[:8]}")
        deactivate_user(user.pk)
        assert is_user_deactivated(user.pk) is True

        user.is_active = True
        user.save()

        assert is_user_deactivated(user.pk) is True

    def test_the_app_config_is_what_connects_it(self):
        """The wiring, not a caller, is what makes this non-optional."""
        import inspect

        from stapel_core.django.apps import CommonDjangoConfig

        assert "connect_deactivation_broadcast" in inspect.getsource(
            CommonDjangoConfig.ready
        )

    def test_its_key_space_is_its_own(self):
        assert DEACTIVATION_PREFIX == "user_deactivated:"
        assert DEACTIVATION_PREFIX not in ("jwt_blacklist:", "user_blacklisted:",
                                           "user_deleted:")

    def test_the_ttl_is_the_shared_revocation_ttl(self):
        from stapel_core.django.jwt.tombstone import tombstone_ttl

        assert deactivation_ttl() == tombstone_ttl()


# ---------------------------------------------------------------------------
# The consumer-mode verifier refuses. This is the reported defect.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestConsumerModeRefusesADeactivatedUser:
    def test_a_still_valid_token_is_refused_after_deactivation(self, consumer_mode):
        """The measured defect: the peer served this request at 0.41.0."""
        user = get_user_model().objects.create(username=f"d-{uuid.uuid4().hex[:8]}")
        claims = _claims(user)
        assert get_or_create_user_from_jwt(claims) is not None

        deactivate_user(user.pk)

        assert get_or_create_user_from_jwt(claims) is None

    def test_reactivation_restores_service(self, consumer_mode):
        user = get_user_model().objects.create(username=f"d-{uuid.uuid4().hex[:8]}")
        claims = _claims(user)
        deactivate_user(user.pk)
        assert get_or_create_user_from_jwt(claims) is None

        lift_deactivation(user.pk)

        assert get_or_create_user_from_jwt(claims) is not None

    def test_it_is_refused_across_cache_prefixes(self):
        """Issuer and consumer name their caches differently; one namespace."""
        user_pk = uuid.uuid4()
        with override_settings(CACHES=ISSUER, JWT_CREATE_USERS_FROM_TOKEN=False):
            reset_revocation_cache()
            deactivate_user(user_pk)
        reset_revocation_cache()
        with override_settings(CACHES=CONSUMER, JWT_CREATE_USERS_FROM_TOKEN=True):
            assert is_user_deactivated(user_pk) is True
        reset_revocation_cache()

    def test_an_untouched_user_is_still_served(self, consumer_mode):
        user = get_user_model().objects.create(username=f"d-{uuid.uuid4().hex[:8]}")
        assert get_or_create_user_from_jwt(_claims(user)) is not None

    def test_a_store_outage_refuses(self, consumer_mode):
        """Fails closed, like both blacklists and the tombstone."""
        with patch(
            "stapel_core.core.revocation_store.revocation_cache",
            side_effect=RuntimeError("redis down"),
        ):
            assert is_user_deactivated(uuid.uuid4()) is True

    @override_settings(STAPEL_BLACKLIST_FAIL_OPEN=True)
    def test_the_documented_hatch_flips_it(self):
        with patch(
            "stapel_core.core.revocation_store.revocation_cache",
            side_effect=RuntimeError("redis down"),
        ):
            assert is_user_deactivated(uuid.uuid4()) is False


# ---------------------------------------------------------------------------
# The claim can no longer satisfy the gate that judges it.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestTheClaimNoLongerMovesIsActive:
    def test_a_true_claim_does_not_reactivate_a_local_row(self, consumer_mode):
        """The ordering bug, in its own right.

        The row is locally inactive; the token asserts `is_active: true`
        because it was minted while the account was live. Before 0.43.0 the
        claim was written to the row first and then read back as the gate.
        """
        User = get_user_model()
        user = User.objects.create(username=f"d-{uuid.uuid4().hex[:8]}")
        User.objects.filter(pk=user.pk).update(is_active=False)

        assert get_or_create_user_from_jwt(_claims(user)) is None

        user.refresh_from_db()
        assert user.is_active is False, "the claim wrote itself onto the row"

    def test_a_false_claim_does_not_deactivate_a_local_row_either(self, consumer_mode):
        """Not synced in EITHER direction — the column is a local decision now.

        Fleet-wide lifecycle travels in the revocation namespace, which is
        checked before any claim is trusted, so the column has no second job.
        """
        user = get_user_model().objects.create(username=f"d-{uuid.uuid4().hex[:8]}")
        claims = _claims(user)
        claims["is_active"] = False

        assert get_or_create_user_from_jwt(claims) is not None

        user.refresh_from_db()
        assert user.is_active is True

    def test_the_local_gate_still_refuses_a_locally_disabled_row(self, consumer_mode):
        User = get_user_model()
        user = User.objects.create(username=f"d-{uuid.uuid4().hex[:8]}")
        User.objects.filter(pk=user.pk).update(is_active=False)
        claims = _claims(user)
        claims.pop("is_active")

        assert get_or_create_user_from_jwt(claims) is None

    def test_other_claims_still_sync(self, consumer_mode):
        """Only lifecycle stopped syncing; staff status is unchanged (AS-2)."""
        user = get_user_model().objects.create(username=f"d-{uuid.uuid4().hex[:8]}")
        claims = _claims(user)
        claims["is_staff"] = True

        resolved = get_or_create_user_from_jwt(claims)

        assert resolved is not None
        user.refresh_from_db()
        assert user.is_staff is True


# ---------------------------------------------------------------------------
# The issuer is untouched: its own database was always the authority.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestIssuerModeIsUnchanged:
    def test_the_local_flag_still_decides(self, issuer_mode):
        User = get_user_model()
        user = User.objects.create(username=f"d-{uuid.uuid4().hex[:8]}")
        assert get_or_create_user_from_jwt(_claims(user)) is not None

        User.objects.filter(pk=user.pk).update(is_active=False)
        assert get_or_create_user_from_jwt(_claims(user)) is None

    def test_it_pays_no_store_read(self, issuer_mode):
        """The local database is the account here — no cache round trip."""
        user = get_user_model().objects.create(username=f"d-{uuid.uuid4().hex[:8]}")
        with patch(
            "stapel_core.django.jwt.utils._deactivated"
        ) as check:
            get_or_create_user_from_jwt(_claims(user))
        check.assert_not_called()
