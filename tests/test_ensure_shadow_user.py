"""``ensure_shadow_user`` — the shadow row seeded from an EVENT, not a token.

The production shape this closes (iron-recordings, 2026-09-13): the
workspaces service bootstraps a personal workspace the instant an account is
created and publishes ``workspace.personal.created``; the recordings service
reacts by writing ``ZoomIngestSettings(user_id=…)`` — and it has no row for
that user, because its only writer of one is the JWT seam on the account's
first authenticated request HERE, which has not happened yet. Postgres
answered ``ForeignKeyViolation … Key (user_id)=(13484e5c-…) is not present in
table "users"``, three events retried twenty-two times and all three parked
in the DLQ.

Three services had already grown a private work-around for it
(``stapel_workspaces._mirror_user``, ``billing_ext.shadow.resolve_user``, and
recordings would have been the third). The tests below pin the parts that
made a shared one worth writing rather than a fourth copy: the privilege
silence, the untouched existing row, the lifecycle gates and the guest's
NULL email.
"""
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

from stapel_core.django.users import ensure_shadow_user

User = get_user_model()


def _uid():
    return str(uuid.uuid4())


@pytest.fixture
def consumer_mode():
    """Shadow-copy mode — what every downstream service in a fleet runs."""
    with override_settings(JWT_CREATE_USERS_FROM_TOKEN=True):
        yield


@pytest.mark.django_db
@pytest.mark.usefixtures("consumer_mode")
class TestItMaterialisesTheRow:
    def test_a_guest_event_creates_the_row_the_fk_needs(self):
        uid = _uid()
        assert not User.objects.filter(pk=uid).exists()

        user = ensure_shadow_user(uid, {"user_id": uid, "is_anonymous": True,
                                        "auth_type": "anonymous"})

        assert user is not None
        assert str(user.pk) == uid
        assert user.is_anonymous is True
        assert user.auth_type == "anonymous"

    def test_a_guest_gets_a_null_email_not_an_empty_string(self):
        """Two guests must not collide on ``email = ''`` (it is unique)."""
        first = ensure_shadow_user(_uid(), {"is_anonymous": True, "email": ""})
        second = ensure_shadow_user(_uid(), {"is_anonymous": True, "email": ""})

        assert first is not None and second is not None
        assert first.email is None
        assert second.email is None

    def test_an_email_on_a_guest_payload_is_not_written(self):
        """A guest has no email anchor; the signup that gives them one is
        the issuer's event to publish, not this one's to guess."""
        uid = _uid()
        user = ensure_shadow_user(uid, {"is_anonymous": True,
                                        "email": "guest@example.com"})
        assert user.email is None

    def test_a_named_account_keeps_its_identity_fields(self):
        uid = _uid()
        user = ensure_shadow_user(uid, {
            "email": "new@example.com", "username": "newbie",
            "auth_type": "email", "is_anonymous": False,
        })
        assert user.email == "new@example.com"
        assert user.username == "newbie"
        assert user.is_anonymous is False

    def test_the_username_is_derived_from_the_id_not_random(self):
        """Replayable: the same event proposes the same name twice."""
        uid = _uid()
        user = ensure_shadow_user(uid, {"is_anonymous": True})
        assert user.username == f"anon_{uuid.UUID(uid).hex[:12]}"

        named = ensure_shadow_user(_uid(), {})
        assert named.username.startswith("user_")

    def test_an_id_only_payload_is_enough(self):
        """``workspace.personal.created`` carries the id and nothing else."""
        uid = _uid()
        assert ensure_shadow_user(uid) is not None
        assert User.objects.filter(pk=uid).exists()

    def test_it_is_idempotent(self):
        uid = _uid()
        a = ensure_shadow_user(uid, {"is_anonymous": True})
        b = ensure_shadow_user(uid, {"is_anonymous": True})
        # str vs uuid.UUID: the first call's row carries the pk we passed in,
        # the second call's is re-read from the column. Comparing them raw is
        # the same `UUID(x) != str(x)` trap that burned eight accounts in the
        # re-key path (core 0.60.1) — compare as text on purpose.
        assert str(a.pk) == str(b.pk)
        assert User.objects.filter(pk=uid).count() == 1


@pytest.mark.django_db
@pytest.mark.usefixtures("consumer_mode")
class TestAnEventIsNotAToken:
    def test_no_privileges_are_taken_from_the_payload(self):
        uid = _uid()
        user = ensure_shadow_user(uid, {
            "is_staff": True, "is_superuser": True, "staff_roles": ["root"],
        })
        assert user.is_staff is False
        assert user.is_superuser is False
        assert list(getattr(user, "staff_roles", []) or []) == []

    def test_an_existing_staff_row_is_not_demoted(self):
        """The regression a naive delegation to the JWT seam would cause.

        ``get_or_create_user_from_jwt`` REPLACES staff status from the claims
        in consumer mode. An event payload carries none — by the rule above —
        so passing it through reads as ``is_staff=False`` and strips a staff
        shadow row, from a handler whose only business was a foreign key.
        """
        uid = _uid()
        User.objects.create_user(pk=uid, username="admin_shadow",
                                 email="admin@example.com", is_staff=True,
                                 is_superuser=True)

        returned = ensure_shadow_user(uid, {"user_id": uid})

        returned.refresh_from_db()
        assert returned.is_staff is True
        assert returned.is_superuser is True

    def test_an_existing_row_keeps_its_email(self):
        """The row is returned untouched, not re-synced from the event."""
        uid = _uid()
        User.objects.create_user(pk=uid, username="known",
                                 email="known@example.com")

        ensure_shadow_user(uid, {"email": "stale@example.com"})

        assert User.objects.get(pk=uid).email == "known@example.com"


@pytest.mark.django_db
@pytest.mark.usefixtures("consumer_mode")
class TestTheLifecycleGatesHold:
    def test_a_deleted_account_is_not_revived_by_an_event(self, monkeypatch):
        uid = _uid()
        monkeypatch.setattr(
            "stapel_core.django.jwt.utils._tombstoned", lambda u: str(u) == uid
        )
        assert ensure_shadow_user(uid, {"is_anonymous": True}) is None
        assert not User.objects.filter(pk=uid).exists()

    def test_a_deleted_account_that_still_has_a_local_row_is_refused(
        self, monkeypatch
    ):
        uid = _uid()
        User.objects.create_user(pk=uid, username="ghost", email="g@example.com")
        monkeypatch.setattr(
            "stapel_core.django.jwt.utils._tombstoned", lambda u: str(u) == uid
        )
        assert ensure_shadow_user(uid, {}) is None

    def test_a_deactivated_account_is_refused(self, monkeypatch):
        uid = _uid()
        monkeypatch.setattr(
            "stapel_core.django.jwt.utils._deactivated", lambda u: str(u) == uid
        )
        assert ensure_shadow_user(uid, {}) is None
        assert not User.objects.filter(pk=uid).exists()


@pytest.mark.django_db
class TestAuthoritativeModeCreatesNothing:
    """``JWT_CREATE_USERS_FROM_TOKEN=False`` — the local table decides."""

    def test_an_unknown_id_is_not_created(self):
        uid = _uid()
        with override_settings(JWT_CREATE_USERS_FROM_TOKEN=False):
            assert ensure_shadow_user(uid, {"is_anonymous": True}) is None
        assert not User.objects.filter(pk=uid).exists()

    def test_a_known_id_is_still_answered(self):
        uid = _uid()
        User.objects.create_user(pk=uid, username="local", email="l@example.com")
        with override_settings(JWT_CREATE_USERS_FROM_TOKEN=False):
            assert ensure_shadow_user(uid, {}) is not None

    def test_the_default_is_authoritative(self):
        """Absent setting = the trusting mode is NOT the default."""
        uid = _uid()
        assert ensure_shadow_user(uid, {}) is None


@pytest.mark.django_db
@pytest.mark.usefixtures("consumer_mode")
class TestNothingToDo:
    @pytest.mark.parametrize("bad", [None, "", "   ", "None"])
    def test_a_missing_id_is_none_not_a_crash(self, bad):
        assert ensure_shadow_user(bad, {"is_anonymous": True}) is None

    def test_an_unusable_id_is_none_not_a_crash(self):
        assert ensure_shadow_user("not-a-uuid", {}) is None
