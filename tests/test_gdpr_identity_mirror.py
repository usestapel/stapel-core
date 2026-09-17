"""The identity mirror: the rows every service keeps about somebody else's users.

Found by a deliberate erasure drill on a live fleet, 2026-09-16. Nine owners
answered with receipts in under a second, the request reached `deleted` with
`completeness_waived=False` — and `iron-recordings` still held
`gdpr-drill@stapel.test` in its local users.User row, `is_active=True`.

Every module answered truthfully about its own data. Nobody answered for the
framework's copy of the identity, because it is not any module's data.
"""
import json
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model

from stapel_core.gdpr import identity

pytestmark = pytest.mark.django_db


def _mirrored(email="person@example.com", **extra):
    User = get_user_model()
    return User.objects.create(username=email, email=email, **extra)


class TestAnonymise:
    def test_it_overwrites_every_identifying_field(self):
        user = _mirrored()
        identity.anonymize_identity(user)
        user.refresh_from_db()
        assert user.email != "person@example.com"
        assert user.username != "person@example.com"
        assert user.email.endswith("@deleted.invalid")
        assert user.username.startswith("deleted-")

    def test_the_row_survives_because_other_tables_reference_it(self):
        """Deletion would take unrelated rows with it under CASCADE."""
        user = _mirrored()
        pk = user.pk
        identity.anonymize_identity(user)
        assert get_user_model().objects.filter(pk=pk).exists()

    def test_the_account_is_deactivated(self):
        user = _mirrored()
        identity.anonymize_identity(user)
        user.refresh_from_db()
        assert user.is_active is False

    def test_the_password_can_never_validate(self):
        user = _mirrored()
        user.set_password("hunter2")
        user.save()
        identity.anonymize_identity(user)
        user.refresh_from_db()
        assert not user.has_usable_password()

    # The "core and stapel-gdpr agree on what erased means" assertion lives in
    # stapel-gdpr's suite, not here, for two reasons. Importing stapel_gdpr
    # from core's tests registers ITS eighteen error keys into the shared
    # registry, and core's i18n gate then fails because core's catalogs do not
    # carry keys core does not own — a cross-library import with a
    # cross-library side effect. And the dependency runs gdpr -> core, so the
    # library that must not drift is the one that should assert it.


class TestEraseSubject:
    def test_it_anonymises_a_mirrored_account(self):
        user = _mirrored()
        result = identity.erase_subject("account", str(user.pk))
        assert result == {"identity_mirror": 1}
        user.refresh_from_db()
        assert user.email.endswith("@deleted.invalid")

    def test_a_subject_type_it_does_not_claim_is_none(self):
        """None receipts nothing — an erasure we are not asked for is not ours."""
        user = _mirrored()
        assert identity.erase_subject("workspace", str(user.pk)) is None
        user.refresh_from_db()
        assert user.email == "person@example.com"

    def test_a_user_never_mirrored_here_is_none(self):
        import uuid
        assert identity.erase_subject("account", str(uuid.uuid4())) is None

    def test_redelivery_reports_zero_not_a_second_pseudonym(self):
        """At-least-once delivery: the second run must not mint a new tombstone."""
        user = _mirrored()
        identity.erase_subject("account", str(user.pk))
        user.refresh_from_db()
        first = user.email
        assert identity.erase_subject("account", str(user.pk)) == {"identity_mirror": 0}
        user.refresh_from_db()
        assert user.email == first

    def test_a_silent_no_op_raises_rather_than_receipting(self, monkeypatch):
        """The defect this module exists for: reporting done while data lives."""
        user = _mirrored()
        monkeypatch.setattr(identity, "anonymize_identity", lambda u: [])
        with pytest.raises(RuntimeError) as exc:
            identity.erase_subject("account", str(user.pk))
        assert "not erased" in str(exc.value)


class TestRegistration:
    def test_a_mirroring_service_registers(self, settings):
        settings.JWT_CREATE_USERS_FROM_TOKEN = True
        assert identity.mirrors_identities() is True

    def test_the_identity_OWNER_does_not(self, settings):
        """auth mints tokens rather than consuming them, and stapel-gdpr's
        erase_identity is already the single writer of that row."""
        settings.JWT_CREATE_USERS_FROM_TOKEN = False
        assert identity.mirrors_identities() is False
        assert identity.register_identity_mirror_owner() is False

    def test_it_claims_only_account(self):
        """Claiming a subject type it cannot erase would make the liveness
        probe a lie about what this can do."""
        assert identity.SUBJECT_TYPES == ("account",)


@pytest.mark.django_db
class TestMerge:
    """`stapel_core.lifecycle.E001` caught this module the day it shipped:
    registering as a data owner subscribes `user.deleted`, and an app that
    knows deletion and not merge strands the merged account's rows."""

    def _event(self, **payload):
        import types

        return types.SimpleNamespace(payload=payload, event_id="evt-merge")

    def test_the_losing_row_is_anonymised_not_deleted(self):
        """Other tables still reference it by id; dropping it takes them
        with it under CASCADE or breaks them."""
        loser = _mirrored("loser@example.com")
        winner = _mirrored("winner@example.com")

        identity.reparent_on_merge(
            self._event(from_user_id=str(loser.pk), into_user_id=str(winner.pk))
        )

        loser.refresh_from_db()
        assert get_user_model().objects.filter(pk=loser.pk).exists()
        assert loser.email.endswith("@deleted.invalid")

    def test_the_surviving_account_is_untouched(self):
        loser = _mirrored("loser@example.com")
        winner = _mirrored("winner@example.com")

        identity.reparent_on_merge(
            self._event(from_user_id=str(loser.pk), into_user_id=str(winner.pk))
        )

        winner.refresh_from_db()
        assert winner.email == "winner@example.com"
        assert winner.is_active is True

    def test_a_redelivered_merge_does_nothing(self):
        loser = _mirrored("loser@example.com")
        identity.reparent_on_merge(self._event(from_user_id=str(loser.pk)))
        loser.refresh_from_db()
        first = loser.email
        identity.reparent_on_merge(self._event(from_user_id=str(loser.pk)))
        loser.refresh_from_db()
        assert loser.email == first

    def test_a_payload_without_from_user_id_is_refused_not_crashed(self):
        identity.reparent_on_merge(self._event(into_user_id="x"))

    def test_an_unknown_id_is_a_no_op(self):
        import uuid

        identity.reparent_on_merge(self._event(from_user_id=str(uuid.uuid4())))


@pytest.mark.django_db
class TestTheSweepIsHonestAboutWhatItDid:
    """Caught by running the sweep twice on a live fleet: the second run
    reported one row still to do, and would have claimed to anonymise it while
    `erase_subject` correctly did nothing. A command that reports work it did
    not do is worse than one that refuses — the number is what somebody signs
    off against.

    Same root cause as the guard in `erase_subject`: idempotency has to
    RECOGNISE the tombstone, because after an anonymisation the identity
    fields are not empty, they hold it.
    """

    def _run(self, tmp_path, ids, dry_run=False):
        """The Command class directly, not call_command.

        core's own test settings do not install `stapel_core.django` as an
        app, so Django's command discovery cannot see it here — which says
        nothing about the command and everything about the test harness.
        """
        from io import StringIO

        from stapel_core.django.management.commands.gdpr_sweep_identity_mirror import (
            Command,
        )

        path = tmp_path / "ids.txt"
        path.write_text("\n".join(str(i) for i in ids) + "\n")
        cmd = Command()
        cmd.stdout = StringIO()
        cmd.style = type("S", (), {
            "SUCCESS": staticmethod(lambda s: s),
            "WARNING": staticmethod(lambda s: s),
        })()
        cmd.handle(user_ids_file=str(path), dry_run=dry_run)
        return cmd.stdout.getvalue()

    def test_a_second_run_finds_nothing(self, tmp_path, settings):
        settings.JWT_CREATE_USERS_FROM_TOKEN = True
        user = _mirrored()

        first = self._run(tmp_path, [user.pk])
        assert "anonymised 1" in first

        second = self._run(tmp_path, [user.pk], dry_run=True)
        assert "still identifying   : 0" in second
        assert "nothing to sweep" in second

    def test_it_refuses_where_this_process_is_not_a_mirror(self, tmp_path, settings):
        """There the user table is the authoritative identity, and
        anonymising it from a list would erase accounts nobody asked about."""
        from django.core.management.base import CommandError

        settings.JWT_CREATE_USERS_FROM_TOKEN = False
        user = _mirrored()
        with pytest.raises(CommandError) as exc:
            self._run(tmp_path, [user.pk])
        assert "does not mirror identities" in str(exc.value)
        user.refresh_from_db()
        assert user.email == "person@example.com"


class TestTheOwnerNameIsPerService:
    """One shared owner name means one ErasurePart and one receipt for N
    services, so the first to answer marks it done and a second service's
    FAILURE is invisible behind the first's success.

    Measured on a live fleet 2026-09-16: two services mirrored one subject,
    one anonymised and one rolled back, the receipt said done, and the address
    survived. The defect this module closes, one level down.
    """

    def test_it_is_prefixed_and_namespaced(self, settings):
        settings.STAPEL_SERVICE_NAME = "iron-workspaces"
        assert identity.owner_name() == "identity_mirror:iron-workspaces"

    def test_two_services_get_two_names(self, settings):
        settings.STAPEL_SERVICE_NAME = "iron-recordings"
        a = identity.owner_name()
        settings.STAPEL_SERVICE_NAME = "iron-workspaces"
        b = identity.owner_name()
        assert a != b

    def test_it_falls_back_to_service_name(self, settings):
        settings.STAPEL_SERVICE_NAME = ""
        settings.SERVICE_NAME = "Iron Recordings"
        assert identity.owner_name() == "identity_mirror:iron-recordings"

    def test_a_nameless_service_is_an_error_not_a_guess(self, settings, monkeypatch):
        """Guessing would put two services under one name, which is the bug."""
        settings.STAPEL_SERVICE_NAME = ""
        settings.SERVICE_NAME = ""
        monkeypatch.setattr(identity, "owner_name", identity.owner_name)
        with pytest.raises(RuntimeError) as exc:
            identity.owner_name(service="   ")
        assert "per service" in str(exc.value)


class TestNobodyShipsSomebodyElsesSchema:
    """A library shipping its own copy of a fact another library owns is how
    a stale five-property `gdpr.section.erased` refused core's eight-property
    receipt — inside the erasure's transaction, so the erasure rolled back
    while every receipt still reported success."""

    def test_core_ships_the_facts_it_emits(self):
        from pathlib import Path

        import stapel_core

        emits = Path(stapel_core.__file__).parent / "gdpr" / "schemas" / "emits"
        for action in ("gdpr.section.erased", "gdpr.owner.alive"):
            assert (emits / f"{action}.json").is_file(), action

    def test_the_shipped_schema_accepts_the_receipt_core_emits(self):
        import json
        from pathlib import Path

        import jsonschema

        import stapel_core

        path = (
            Path(stapel_core.__file__).parent
            / "gdpr" / "schemas" / "emits" / "gdpr.section.erased.json"
        )
        schema = json.loads(path.read_text())
        # The exact shape stapel_core.gdpr.owners emits, receipt_id included —
        # the property the stale copy rejected.
        jsonschema.validate(
            {
                "correlation_id": "c1",
                "owner": "identity_mirror:iron-recordings",
                "subject_type": "account",
                "subject_key": "u1",
                "receipt_id": "identity_mirror:account:u1:c1",
                "counts": {"identity_mirror": 1},
            },
            schema,
        )

    def test_the_check_names_both_packages(self, tmp_path):
        from stapel_core.comm import schema_ownership

        foreign = tmp_path / "stapel_someone_else" / "schemas" / "emits"
        foreign.mkdir(parents=True)
        (foreign / "gdpr.section.erased.json").write_text("{}")

        found = schema_ownership.foreign_schema_copies([str(tmp_path)])
        assert (
            "gdpr.section.erased",
            "stapel_someone_else",
            "stapel_core",
            True,  # divergent: `{}` is not core's schema
        ) in found

    def test_core_shipping_its_own_is_not_a_finding(self, tmp_path):
        from stapel_core.comm import schema_ownership

        mine = tmp_path / "stapel_core" / "schemas" / "emits"
        mine.mkdir(parents=True)
        (mine / "gdpr.section.erased.json").write_text("{}")
        assert schema_ownership.foreign_schema_copies([str(tmp_path)]) == []


class TestSchemaOwnershipSeverity:
    """A copy that cannot yet misbehave must not refuse the boot.

    Shipping the E010 check as an Error while eight of our own libraries still
    vendored the two schemas made the check an outage generator: a fleet's cdn
    family went to ``Restarting`` on a byte-identical copy that had never
    refused a payload and could not. The damage E010 exists to stop is a copy
    that DIFFERS from the owner's — that is the one that rejects the owner's
    receipt and rolls an erasure back. An identical copy is the state that
    becomes divergent later, so it is still reported, at Warning.
    """

    def _core_copy_of(self, action):
        import stapel_core

        return (
            Path(stapel_core.__file__).parent
            / "gdpr"
            / "schemas"
            / "emits"
            / f"{action}.json"
        ).read_text()

    def test_identical_copy_is_a_warning(self, tmp_path):
        from stapel_core.comm import schema_ownership

        foreign = tmp_path / "stapel_lookalike" / "schemas" / "emits"
        foreign.mkdir(parents=True)
        (foreign / "gdpr.owner.alive.json").write_text(
            self._core_copy_of("gdpr.owner.alive")
        )

        problems = schema_ownership.check_schema_ownership(search_roots=[str(tmp_path)])
        assert [p.id for p in problems] == [schema_ownership.W010]
        assert "stapel_lookalike" in problems[0].msg

    def test_divergent_copy_is_an_error(self, tmp_path):
        from stapel_core.comm import schema_ownership

        foreign = tmp_path / "stapel_stale" / "schemas" / "emits"
        foreign.mkdir(parents=True)
        (foreign / "gdpr.section.erased.json").write_text(
            json.dumps({"type": "object", "additionalProperties": False})
        )

        problems = schema_ownership.check_schema_ownership(search_roots=[str(tmp_path)])
        assert [p.id for p in problems] == [schema_ownership.E010]

    def test_whitespace_only_difference_is_not_divergence(self, tmp_path):
        from stapel_core.comm import schema_ownership

        foreign = tmp_path / "stapel_reformatted" / "schemas" / "emits"
        foreign.mkdir(parents=True)
        reflowed = json.dumps(
            json.loads(self._core_copy_of("gdpr.owner.alive")), indent=4
        )
        (foreign / "gdpr.owner.alive.json").write_text(reflowed)

        problems = schema_ownership.check_schema_ownership(search_roots=[str(tmp_path)])
        assert [p.id for p in problems] == [schema_ownership.W010]

    def test_unparseable_copy_is_an_error(self, tmp_path):
        """A copy we cannot compare is not a copy we can call harmless."""
        from stapel_core.comm import schema_ownership

        foreign = tmp_path / "stapel_broken" / "schemas" / "emits"
        foreign.mkdir(parents=True)
        (foreign / "gdpr.owner.alive.json").write_text("{not json")

        problems = schema_ownership.check_schema_ownership(search_roots=[str(tmp_path)])
        assert [p.id for p in problems] == [schema_ownership.E010]

    def test_divergence_is_reported_per_copy(self, tmp_path):
        from stapel_core.comm import schema_ownership

        (tmp_path / "stapel_ok" / "schemas" / "emits").mkdir(parents=True)
        (
            tmp_path / "stapel_ok" / "schemas" / "emits" / "gdpr.owner.alive.json"
        ).write_text(self._core_copy_of("gdpr.owner.alive"))
        (tmp_path / "stapel_bad" / "schemas" / "emits").mkdir(parents=True)
        (
            tmp_path / "stapel_bad" / "schemas" / "emits" / "gdpr.owner.alive.json"
        ).write_text("{}")

        by_package = {
            p.obj: p.id
            for p in schema_ownership.check_schema_ownership(
                search_roots=[str(tmp_path)]
            )
        }
        assert by_package["stapel_ok:gdpr.owner.alive"] == schema_ownership.W010
        assert by_package["stapel_bad:gdpr.owner.alive"] == schema_ownership.E010


class TestDivergenceIsValidationShape:
    """Prose is not a contract. `const` is.

    Six of the eight vendored copies found on 2026-09-17 differed from core's
    only in `description` text and key order — they validate identically and
    can refuse nothing. Two differed in the way that bites: stapel-profiles
    pinned `owner` to `{"const": "profile"}` and required four more fields, so
    a service loading that copy rejects a receipt from
    `identity_mirror:iron-profiles` — the exact refusal-inside-the-erasure's-
    transaction this module exists to stop, still live in the fleet.

    Calling all eight Errors would refuse six boots over a docstring. Calling
    all eight Warnings would let the two real ones through. The line is the
    validating shape.
    """

    def _core_copy_of(self, action):
        import stapel_core

        return (
            Path(stapel_core.__file__).parent
            / "gdpr"
            / "schemas"
            / "emits"
            / f"{action}.json"
        ).read_text()

    def test_different_description_is_only_a_warning(self, tmp_path):
        from stapel_core.comm import schema_ownership

        schema = json.loads(self._core_copy_of("gdpr.section.erased"))
        schema["description"] = "This module sends {owner: 'auth', ...}"
        schema["properties"]["counts"]["description"] = "per-model tally"
        foreign = tmp_path / "stapel_prose" / "schemas" / "emits"
        foreign.mkdir(parents=True)
        (foreign / "gdpr.section.erased.json").write_text(json.dumps(schema))

        problems = schema_ownership.check_schema_ownership(search_roots=[str(tmp_path)])
        assert [p.id for p in problems] == [schema_ownership.W010]

    def test_owner_pinned_to_a_const_is_an_error(self, tmp_path):
        """The live one: a copy that refuses every owner but its own."""
        from stapel_core.comm import schema_ownership

        schema = json.loads(self._core_copy_of("gdpr.section.erased"))
        schema["properties"]["owner"] = {"type": "string", "const": "profile"}
        foreign = tmp_path / "stapel_pinned" / "schemas" / "emits"
        foreign.mkdir(parents=True)
        (foreign / "gdpr.section.erased.json").write_text(json.dumps(schema))

        problems = schema_ownership.check_schema_ownership(search_roots=[str(tmp_path)])
        assert [p.id for p in problems] == [schema_ownership.E010]

    def test_extra_required_fields_are_an_error(self, tmp_path):
        from stapel_core.comm import schema_ownership

        schema = json.loads(self._core_copy_of("gdpr.section.erased"))
        schema["required"] = ["correlation_id", "owner", "subject_type", "counts"]
        foreign = tmp_path / "stapel_strict" / "schemas" / "emits"
        foreign.mkdir(parents=True)
        (foreign / "gdpr.section.erased.json").write_text(json.dumps(schema))

        problems = schema_ownership.check_schema_ownership(search_roots=[str(tmp_path)])
        assert [p.id for p in problems] == [schema_ownership.E010]
