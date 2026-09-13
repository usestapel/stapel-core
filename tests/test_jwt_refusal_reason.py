"""A refusal has to say WHICH gate refused.

The production hour this pins (iron-billing, 2026-09-13). A guest signed up;
``merge_anonymous_into`` folded their account into the one they had just
proved they owned and deleted the guest row, whose ``post_delete`` wrote the
fleet-wide ``user_deleted:<uid>`` tombstone. 111 ms later the browser's
in-flight request still carried the guest's old access cookie, so the
tombstone gate refused it — exactly as designed. What the log said was::

    JWT Auth Failed - User creation failed - user_id=285ea0cd-…,
    path=/billing/api/v1/subscription

Nothing was being created and nothing had failed. The DRF authentication
class printed "User creation failed" for every ``None`` the seam returns, so
the line named a cause it had never checked — and an alert routed on that
line sends the on-call to the shadow-row writer, which was working.

The reason existed one line above in the same log, in this module's own
WARNING. It just never reached the caller that writes the ERROR. So the
seam now hands it over.
"""
import logging
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

from stapel_core.django.jwt.utils import get_or_create_user_from_jwt, resolve_jwt_user

User = get_user_model()


def _uid():
    return str(uuid.uuid4())


def _claims(uid, **extra):
    data = {"user_id": uid, "username": f"u_{uuid.UUID(uid).hex[:8]}"}
    data.update(extra)
    return data


@pytest.fixture
def consumer_mode():
    with override_settings(JWT_CREATE_USERS_FROM_TOKEN=True):
        yield


@pytest.mark.django_db
class TestTheReasonIsCarried:
    @pytest.mark.usefixtures("consumer_mode")
    def test_a_merged_guest_reads_as_deleted_not_as_a_failed_creation(
        self, monkeypatch
    ):
        """The alert verbatim: the account is gone, not un-creatable."""
        uid = _uid()
        monkeypatch.setattr(
            "stapel_core.django.jwt.utils._tombstoned", lambda u: str(u) == uid
        )

        user, refusal = resolve_jwt_user(_claims(uid))

        assert user is None
        assert refusal == "deleted at the issuer"
        assert "creation" not in refusal

    @pytest.mark.usefixtures("consumer_mode")
    def test_a_deactivated_account_says_so(self, monkeypatch):
        uid = _uid()
        monkeypatch.setattr(
            "stapel_core.django.jwt.utils._deactivated", lambda u: str(u) == uid
        )
        assert resolve_jwt_user(_claims(uid))[1] == "deactivated at the issuer"

    def test_a_stale_token_in_authoritative_mode_says_so(self):
        with override_settings(JWT_CREATE_USERS_FROM_TOKEN=False):
            _, refusal = resolve_jwt_user(_claims(_uid()))
        assert "JWT_CREATE_USERS_FROM_TOKEN is off" in refusal

    @pytest.mark.usefixtures("consumer_mode")
    def test_an_inactive_local_row_says_so(self):
        uid = _uid()
        User.objects.create_user(pk=uid, username="closed", email="c@example.com",
                                 is_active=False)
        assert resolve_jwt_user(_claims(uid))[1] == "account is not active"

    def test_a_token_with_no_user_id_says_so(self):
        with override_settings(JWT_CREATE_USERS_FROM_TOKEN=True):
            assert "no user_id" in resolve_jwt_user({"username": "x"})[1]

    @pytest.mark.usefixtures("consumer_mode")
    def test_a_resolved_user_carries_no_reason(self):
        uid = _uid()
        user, refusal = resolve_jwt_user(_claims(uid))
        assert user is not None
        assert refusal is None


@pytest.mark.django_db
@pytest.mark.usefixtures("consumer_mode")
class TestTheOldEntryPointIsUnchanged:
    """Four call sites still ask only for the user; none of them moved."""

    def test_it_still_returns_the_user(self):
        uid = _uid()
        assert str(get_or_create_user_from_jwt(_claims(uid)).pk) == uid

    def test_it_still_returns_none_on_a_refusal(self, monkeypatch):
        uid = _uid()
        monkeypatch.setattr(
            "stapel_core.django.jwt.utils._tombstoned", lambda u: str(u) == uid
        )
        assert get_or_create_user_from_jwt(_claims(uid)) is None


@pytest.mark.django_db
@pytest.mark.usefixtures("consumer_mode")
class TestTheAuthenticationClassLogsIt:
    """The line an alert is routed on, asserted through the real class."""

    def test_the_error_line_names_the_gate_that_refused(
        self, monkeypatch, caplog
    ):
        from stapel_core.django.jwt.authentication import JWTCookieAuthentication

        uid = _uid()
        monkeypatch.setattr(
            "stapel_core.django.jwt.utils._tombstoned", lambda u: str(u) == uid
        )
        monkeypatch.setattr(
            "stapel_core.django.jwt.provider.jwt_provider.is_blacklisted",
            lambda token: False,
        )
        monkeypatch.setattr(
            "stapel_core.django.jwt.provider.jwt_provider.validate_token",
            lambda token: _claims(uid),
        )

        from django.test import RequestFactory

        request = RequestFactory().get("/billing/api/v1/subscription")
        request.META["HTTP_AUTHORIZATION"] = "Bearer " + ("t" * 40)

        with caplog.at_level(logging.ERROR,
                             logger="stapel_core.django.jwt.authentication"):
            assert JWTCookieAuthentication().authenticate(request) is None

        line = "\n".join(r.getMessage() for r in caplog.records)
        assert "deleted at the issuer" in line
        assert "User creation failed" not in line
