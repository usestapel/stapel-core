"""The identity mirror: the rows every service keeps about somebody else's users.

Found by a deliberate erasure drill on a live fleet, 2026-09-16. Nine owners
answered with receipts in under a second, the request reached `deleted` with
`completeness_waived=False` — and `iron-recordings` still held
`gdpr-drill@stapel.test` in its local users.User row, `is_active=True`.

Every module answered truthfully about its own data. Nobody answered for the
framework's copy of the identity, because it is not any module's data.
"""
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
