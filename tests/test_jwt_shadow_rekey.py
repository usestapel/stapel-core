"""A brand-new user must not be tombstoned by the code that mirrors them.

The defect, measured on a deployed stand on 2026-09-04
------------------------------------------------------
Consumer mode materialises a local row on first contact. Two first contacts
race constantly — one page fires ``GET`` and ``POST`` of the same resource in
a tick, an upload step fires two parallel POSTs — and both read
``User.DoesNotExist``. One created the row; the other then found that
brand-new row by an alternate unique key and ran the *shadow-row re-key*
path on it, a path written for a service database restored from before the
person's issuer id changed. That path deletes the row and creates it again.

Two things then went wrong at once:

* the id it was "fixing" was not wrong. ``pk`` arrives as a ``str`` off the
  JWT claim and ``existing_user.pk`` is a ``uuid.UUID``, so ``old_pk != pk``
  was True for the SAME id — the logs read ``updating PK from X to X``.
* the delete published a fleet-wide deletion tombstone, and
  ``get_or_create_user_from_jwt`` consults tombstones before any claim in
  EVERY service. So the account was locked out of the whole fleet, for the
  tombstone's TTL, with a perfectly valid session cookie in the browser.

Every test in the first two classes fails on 0.60.0.
"""
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError

from django.test import override_settings
from unittest.mock import patch

from stapel_core.core.revocation_store import reset_revocation_cache
from stapel_core.django.jwt.tombstone import (
    connect_deletion_tombstone,
    is_user_tombstoned,
    shadow_rekey,
    tombstone_user,
)
from stapel_core.django.jwt.utils import get_or_create_user_from_jwt
from stapel_core.django.management.commands.lift_tombstones import (
    Command as LiftTombstones,
)

LOCMEM = "django.core.cache.backends.locmem.LocMemCache"
STORE = {"default": {"BACKEND": LOCMEM, "LOCATION": "d320-store", "KEY_PREFIX": "svc"}}

CONSUMER = dict(JWT_CREATE_USERS_FROM_TOKEN=True, CACHES=STORE)
ISSUER = dict(JWT_CREATE_USERS_FROM_TOKEN=False, CACHES=STORE)


@pytest.fixture(autouse=True)
def _clean_store():
    reset_revocation_cache()
    with override_settings(CACHES=STORE):
        from stapel_core.core.revocation_store import revocation_cache

        revocation_cache().clear()
    reset_revocation_cache()
    connect_deletion_tombstone()
    yield
    reset_revocation_cache()


def _claim(uid, username, email=None):
    return {
        "user_id": str(uid),
        "username": username,
        "email": email or f"{username}@example.com",
        "is_active": True,
        "is_staff": False,
        "is_superuser": False,
    }


class _RacingManager:
    """The manager as the LOSER of a first-contact race sees it.

    ``get`` misses — that request read the table before the winner committed
    — while every other query sees the winner's row, which is exactly the
    interleaving that drove the destructive path in production.
    """

    def __init__(self, real, does_not_exist):
        self._real = real
        self._exc = does_not_exist

    def get(self, *_args, **_kwargs):
        raise self._exc

    def __getattr__(self, name):
        return getattr(self._real, name)


def _racing_user_model():
    User = get_user_model()

    class Racing:
        DoesNotExist = User.DoesNotExist
        objects = _RacingManager(User.objects, User.DoesNotExist("raced"))

    return Racing


@pytest.mark.django_db
class TestFirstContactRace:
    """The loser of the race returns the winner's row and destroys nothing."""

    @override_settings(**CONSUMER)
    def test_loser_of_the_race_does_not_delete_the_winners_row(self):
        User = get_user_model()
        uid = uuid.uuid4()
        winner = User.objects.create_user(
            pk=uid, username="racer", email="racer@example.com"
        )

        with patch(
            "stapel_core.django.jwt.utils._get_user_model",
            _racing_user_model,
        ):
            user = get_or_create_user_from_jwt(_claim(uid, "racer"))

        assert user is not None
        assert str(user.pk) == str(uid)
        assert User.objects.filter(pk=uid).exists()
        assert User.objects.count() == 1
        # The row was never deleted, so nothing published a deletion.
        assert is_user_tombstoned(uid) is False
        assert winner.pk == user.pk

    @override_settings(**CONSUMER)
    def test_the_same_id_as_str_and_as_uuid_is_the_same_id(self):
        """`UUID(x) != str(x)` is True in Python — and that was the bug."""
        User = get_user_model()
        uid = uuid.uuid4()
        User.objects.create_user(pk=uid, username="samey", email="s@example.com")

        with patch(
            "stapel_core.django.jwt.utils._get_user_model",
            _racing_user_model,
        ):
            user = get_or_create_user_from_jwt(_claim(uid, "samey"))

        assert user is not None
        # The next request must still be admitted: a tombstone here is
        # permanent for the account, in every service in the fleet.
        assert get_or_create_user_from_jwt(_claim(uid, "samey")) is not None

    @override_settings(**CONSUMER)
    def test_plain_first_contact_still_creates_the_row(self):
        User = get_user_model()
        uid = uuid.uuid4()
        user = get_or_create_user_from_jwt(_claim(uid, "fresh"))
        assert user is not None
        assert str(user.pk) == str(uid)
        assert User.objects.filter(pk=uid).exists()
        assert is_user_tombstoned(uid) is False


@pytest.mark.django_db
class TestRekeyIsNotADeletion:
    """A shadow row moved onto a new id is bookkeeping, not a deletion."""

    @override_settings(**CONSUMER)
    def test_rekey_does_not_tombstone_anyone(self):
        User = get_user_model()
        old_uid, new_uid = uuid.uuid4(), uuid.uuid4()
        User.objects.create_user(
            pk=old_uid, username="moved", email="moved@example.com"
        )

        user = get_or_create_user_from_jwt(_claim(new_uid, "moved"))

        assert user is not None
        assert str(user.pk) == str(new_uid)
        assert not User.objects.filter(pk=old_uid).exists()
        # Neither id may be published as deleted: the account is alive, and
        # it is the account that was just re-created.
        assert is_user_tombstoned(new_uid) is False
        assert is_user_tombstoned(old_uid) is False

    def test_shadow_rekey_is_scoped_to_one_uid(self):
        """A cascade reaching another user's row must still tombstone it."""
        mine, theirs = uuid.uuid4(), uuid.uuid4()
        with shadow_rekey(mine):
            tombstoned_inside = _delete_and_report(theirs)
        assert tombstoned_inside is True

    def test_a_real_deletion_still_tombstones(self):
        User = get_user_model()
        uid = uuid.uuid4()
        pytest.importorskip("django")
        with override_settings(**CONSUMER):
            User.objects.create_user(
                pk=uid, username="gone", email="gone@example.com"
            ).delete()
            assert is_user_tombstoned(uid) is True

    def test_shadow_rekey_restores_the_previous_state(self):
        uid = uuid.uuid4()
        with shadow_rekey(uid):
            with shadow_rekey(uid):
                pass
            # The inner block must not have lifted the outer suppression.
            from stapel_core.django.jwt.tombstone import _is_shadow_rekey

            assert _is_shadow_rekey(uid) is True
        from stapel_core.django.jwt.tombstone import _is_shadow_rekey

        assert _is_shadow_rekey(uid) is False


def _delete_and_report(uid) -> bool:
    """Delete a row for *uid* and report whether a tombstone was written."""
    User = get_user_model()
    with override_settings(**CONSUMER):
        User.objects.create_user(
            pk=uid, username=f"u{str(uid)[:8]}", email=f"{str(uid)[:8]}@example.com"
        ).delete()
        return is_user_tombstoned(uid)


@pytest.mark.django_db
class TestLiftTombstonesCommand:
    """Cleaning up after the versions that had the defect."""

    @override_settings(**ISSUER)
    def test_issuer_check_is_required(self):
        with pytest.raises(CommandError, match="--issuer-check is required"):
            call_command(LiftTombstones())

    @override_settings(**CONSUMER)
    def test_refuses_to_run_at_a_consumer(self):
        with pytest.raises(CommandError, match="CONSUMER"):
            call_command(
                LiftTombstones(), "--issuer-check", "--uid", str(uuid.uuid4())
            )

    @override_settings(**ISSUER)
    def test_lifts_only_uids_the_issuer_still_holds_as_active(self, capsys):
        User = get_user_model()
        alive = uuid.uuid4()
        really_deleted = uuid.uuid4()
        User.objects.create_user(pk=alive, username="alive", email="a@example.com")
        tombstone_user(alive)
        tombstone_user(really_deleted)

        call_command(
            LiftTombstones(),
            "--issuer-check",
            "--apply",
            "--uid",
            str(alive),
            "--uid",
            str(really_deleted),
        )
        out = capsys.readouterr().out
        assert str(alive) in out
        assert "KEPT" in out
        assert is_user_tombstoned(alive) is False
        assert is_user_tombstoned(really_deleted) is True

    @override_settings(**ISSUER)
    def test_reports_without_apply(self, capsys):
        User = get_user_model()
        alive = uuid.uuid4()
        User.objects.create_user(pk=alive, username="dry", email="d@example.com")
        tombstone_user(alive)

        call_command(LiftTombstones(), "--issuer-check", "--uid", str(alive))
        out = capsys.readouterr().out
        assert "WOULD_LIFT" in out
        assert "re-run with --apply" in out
        assert is_user_tombstoned(alive) is True
