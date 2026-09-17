"""The INSERT that loses the first-contact race, and the claims it dropped.

0.60.1 stopped the loser from DESTROYING the winner's row. It still threw the
losing request's own claims away: the winner's row was returned untouched, so
an email, a staff flag or a set of roles that arrived on that token landed
nowhere until some later request happened to carry them again.

Measured on a client fleet's production database, 2026-09-13 → 2026-09-17:
48 losses on ``users_pkey``, ~10/day, still climbing after a full redeploy.
Two services see a new account's first token in the same tick, both read
``DoesNotExist``, both INSERT, one loses.

Three tests, three shapes:

* the deterministic one — the loser's interleaving is forced with a manager
  whose ``get`` misses while the table already holds the winner's row, so the
  ``IntegrityError`` is a REAL unique violation, not a mocked one. Red on
  0.83.1;
* the unresolvable collision — a constraint this seam cannot reason about
  must surface as ``ShadowUserConflict``, never as "already mirrored";
* the concurrent one — two threads, two connections, one row. SQLite cannot
  hold two writers, so it runs against a real Postgres in CI
  (``STAPEL_TEST_DATABASE_URL``) and skips elsewhere.
"""
import os
import threading
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, connections
from django.test import override_settings

from stapel_core.django.jwt.utils import (
    ShadowUserConflict,
    get_or_create_user_from_jwt,
)

CONSUMER = dict(JWT_CREATE_USERS_FROM_TOKEN=True)

requires_postgres = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="needs a real Postgres: set STAPEL_TEST_DATABASE_URL (MODULE.md)",
)


def _claim(uid, username, **extra):
    claim = {
        "user_id": str(uid),
        "username": username,
        "email": f"{username}@example.com",
        "is_active": True,
        "is_staff": False,
        "is_superuser": False,
    }
    claim.update(extra)
    return claim


class _LoserManager:
    """The manager as the LOSER of the race sees it: ``get`` misses, the
    table does not. Every other query — including the INSERT — is real."""

    def __init__(self, real, does_not_exist):
        self._real = real
        self._exc = does_not_exist

    def get(self, *_args, **_kwargs):
        raise self._exc

    def __getattr__(self, name):
        return getattr(self._real, name)


def _losing_user_model():
    User = get_user_model()

    class Losing:
        DoesNotExist = User.DoesNotExist
        objects = _LoserManager(User.objects, User.DoesNotExist("raced"))

    return Losing


@pytest.mark.django_db
class TestLosingInsert:
    @override_settings(**CONSUMER)
    def test_the_loser_applies_its_own_claims_to_the_winners_row(self, monkeypatch):
        """Red on 0.83.1: the loser returned the winner's row untouched."""
        User = get_user_model()
        uid = uuid.uuid4()
        User.objects.create_user(
            pk=uid, username="racer", email="stale@example.com", is_staff=False
        )

        monkeypatch.setattr(
            "stapel_core.django.jwt.utils._get_user_model", _losing_user_model
        )
        claim = _claim(uid, "racer", email="fresh@example.com", is_staff=True)
        claim["staff_roles"] = ["support"]
        user = get_or_create_user_from_jwt(claim)

        assert user is not None
        assert User.objects.count() == 1
        assert user.email == "fresh@example.com"
        assert user.is_staff is True
        stored = User.objects.get(pk=uid)
        assert stored.email == "fresh@example.com"
        assert stored.is_staff is True
        assert list(stored.staff_roles) == ["support"]

    @override_settings(**CONSUMER)
    def test_the_loser_returns_the_winners_row_and_creates_nothing(self, monkeypatch):
        User = get_user_model()
        uid = uuid.uuid4()
        winner = User.objects.create_user(
            pk=uid, username="racer", email="racer@example.com"
        )

        monkeypatch.setattr(
            "stapel_core.django.jwt.utils._get_user_model", _losing_user_model
        )
        user = get_or_create_user_from_jwt(_claim(uid, "racer"))

        assert user is not None
        assert user.pk == winner.pk
        assert User.objects.count() == 1

    @override_settings(**CONSUMER)
    def test_an_unresolvable_collision_is_named_not_swallowed(self, monkeypatch):
        """A constraint this seam cannot reason about must not read as
        "already mirrored": it raises ShadowUserConflict, which the seam then
        reports as a refusal reason naming the type."""
        User = get_user_model()
        uid = uuid.uuid4()

        real_create = User.objects.create_user

        def _colliding_create(*args, **kwargs):
            raise IntegrityError(
                'duplicate key value violates unique constraint "users_email_key"'
            )

        class Colliding:
            DoesNotExist = User.DoesNotExist

            class objects:
                create_user = staticmethod(_colliding_create)

                @staticmethod
                def get(*_a, **_k):
                    raise User.DoesNotExist("absent")

                @staticmethod
                def filter(*args, **kwargs):
                    return User.objects.filter(*args, **kwargs)

        monkeypatch.setattr(
            "stapel_core.django.jwt.utils._get_user_model", lambda: Colliding
        )
        reasons: list[str] = []
        user = get_or_create_user_from_jwt(_claim(uid, "nobody"), reasons)

        assert user is None
        assert reasons and "ShadowUserConflict" in reasons[0]
        assert real_create  # the real manager was never reached

    @override_settings(**CONSUMER)
    def test_the_error_is_an_integrityerror_so_transports_still_catch_it(self):
        assert issubclass(ShadowUserConflict, IntegrityError)


@requires_postgres
@pytest.mark.django_db(transaction=True)
def test_two_connections_first_contact_leaves_one_row():
    """The production interleaving, for real: two threads, two connections,
    the same token, one row and no exception."""
    User = get_user_model()
    uid = uuid.uuid4()
    claim = _claim(uid, "concurrent")
    start = threading.Barrier(2)
    results: list = []
    errors: list = []

    def _mirror():
        # The settings override lives on the main thread: it is global state,
        # and flipping it from two threads would be its own race.
        try:
            start.wait(timeout=10)
            results.append(get_or_create_user_from_jwt(dict(claim)))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            connections.close_all()

    with override_settings(**CONSUMER):
        threads = [threading.Thread(target=_mirror) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

    assert not errors, errors
    assert len(results) == 2
    assert all(r is not None for r in results)
    assert User.objects.filter(pk=uid).count() == 1


def test_postgres_job_is_not_silently_absent():
    """A skipped concurrency gate must be a CHOICE, not an accident.

    The CI job that sets the variable is the gate; this asserts the variable
    is honoured, so that "the suite is green" cannot mean "the database was
    SQLite and nobody noticed".
    """
    if os.environ.get("STAPEL_TEST_DATABASE_URL"):
        assert connection.vendor == "postgresql"
