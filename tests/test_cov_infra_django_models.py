"""Coverage tests for django/models.py (abstract mixins) and django/users/models.py."""
from datetime import timedelta

import pytest
from django.db import connection, models
from django.test import override_settings
from django.utils import timezone

from stapel_core.django.models import (
    LoginAttempt,
    PhoneVerification,
    RefreshTokenTracker,
    RevisionMixin,
    ServiceAPIKey,
)
from stapel_core.django.users.models import User


# ---------------------------------------------------------------------------
# concrete test models (registered under the installed "users" app so the
# abstract bases can be exercised against a real DB table)
# ---------------------------------------------------------------------------


class CovRevisionItem(RevisionMixin):
    name = models.CharField(max_length=50, default="")

    class Meta:
        app_label = "users"


class CovPhoneVerification(PhoneVerification):
    class Meta:
        app_label = "users"


class CovServiceAPIKey(ServiceAPIKey):
    class Meta:
        app_label = "users"


class CovRefreshToken(RefreshTokenTracker):
    # DO_NOTHING: this model stays registered under the "users" app for the
    # whole session while its table only exists inside this module's
    # fixtures — a CASCADE here would make unrelated User.delete() calls in
    # other test modules query the missing table.
    user = models.ForeignKey(User, on_delete=models.DO_NOTHING, null=True)

    class Meta:
        app_label = "users"


class CovLoginAttempt(LoginAttempt):
    class Meta:
        app_label = "users"


@pytest.fixture
def revision_table(transactional_db):
    with connection.schema_editor() as editor:
        editor.create_model(CovRevisionItem)
    yield
    with connection.schema_editor() as editor:
        editor.delete_model(CovRevisionItem)


@pytest.fixture
def phone_table(transactional_db):
    with connection.schema_editor() as editor:
        editor.create_model(CovPhoneVerification)
    yield
    with connection.schema_editor() as editor:
        editor.delete_model(CovPhoneVerification)


# ---------------------------------------------------------------------------
# RevisionMixin
# ---------------------------------------------------------------------------


def test_revision_autoincrements_on_save(revision_table):
    a = CovRevisionItem(name="a")
    a.save()
    assert a.revision == 1
    b = CovRevisionItem(name="b")
    b.save()
    assert b.revision == 2
    a.save()  # re-saving takes the next max revision
    assert a.revision == 3


def test_soft_delete_and_restore(revision_table):
    item = CovRevisionItem(name="x")
    item.save()
    item.soft_delete()
    item.refresh_from_db()
    assert item.deleted is True
    item.restore()
    item.refresh_from_db()
    assert item.deleted is False


def test_get_max_revision(revision_table):
    assert CovRevisionItem.get_max_revision() == 0
    CovRevisionItem(name="a").save()
    assert CovRevisionItem.get_max_revision() == 1


def test_get_changes_since(revision_table):
    a = CovRevisionItem(name="a")
    a.save()  # rev 1
    b = CovRevisionItem(name="b")
    b.save()  # rev 2
    c = CovRevisionItem(name="c")
    c.save()  # rev 3
    b.soft_delete()  # rev 4

    all_changes = CovRevisionItem.get_changes_since()
    assert [o.name for o in all_changes] == ["a", "c", "b"]

    since_one = CovRevisionItem.get_changes_since(min_revision=1)
    assert {o.name for o in since_one} == {"b", "c"}

    bounded = CovRevisionItem.get_changes_since(min_revision=0, max_revision=3)
    assert {o.name for o in bounded} == {"a", "c"}

    live_only = CovRevisionItem.get_changes_since(include_deleted=False)
    assert {o.name for o in live_only} == {"a", "c"}


# ---------------------------------------------------------------------------
# PhoneVerification
# ---------------------------------------------------------------------------


def test_phone_verification_sets_expiry_on_save(phone_table):
    pv = CovPhoneVerification(phone="+79991234567", code="123456")
    pv.save()
    assert pv.expires_at is not None
    assert pv.expires_at > timezone.now() + timedelta(minutes=9)
    assert pv.is_expired() is False


def test_phone_verification_keeps_explicit_expiry(phone_table):
    past = timezone.now() - timedelta(minutes=1)
    pv = CovPhoneVerification(phone="+79991234567", code="1", expires_at=past)
    pv.save()
    assert pv.expires_at == past
    assert pv.is_expired() is True


def test_phone_verification_str():
    pv = CovPhoneVerification(phone="+7999", code="42")
    assert str(pv) == "+7999 - 42"


# ---------------------------------------------------------------------------
# ServiceAPIKey / RefreshTokenTracker / LoginAttempt (no DB needed)
# ---------------------------------------------------------------------------


def test_service_api_key_generate_key():
    key = ServiceAPIKey.generate_key()
    assert key.startswith("sk_")
    assert len(key) == 35
    assert key != CovServiceAPIKey.generate_key()


def test_service_api_key_str():
    active = CovServiceAPIKey(name="cdn", key="sk_x", is_active=True)
    assert str(active) == "cdn - Active"
    inactive = CovServiceAPIKey(name="cdn", key="sk_x", is_active=False)
    assert str(inactive) == "cdn - Inactive"


def test_refresh_token_tracker_str():
    row = CovRefreshToken(token="t1")
    assert str(row) == "None - None"


def test_login_attempt_str():
    row = CovLoginAttempt(
        identifier="a@b.c", attempt_type="failed", ip_address="1.2.3.4"
    )
    assert str(row) == "a@b.c - failed - None"


# ---------------------------------------------------------------------------
# users.User — save() normalisation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_user_save_normalizes_empty_email_and_phone():
    user = User(username="u1", email="", phone="")
    user.save()
    assert user.email is None
    assert user.phone is None


@pytest.mark.django_db
def test_user_save_normalizes_phone_to_e164():
    user = User(username="u2", phone="+7 999 123-45-67")
    user.save()
    assert user.phone == "+79991234567"


@pytest.mark.django_db
def test_user_save_keeps_unparseable_phone():
    user = User(username="u3", phone="not-a-phone")
    user.save()
    assert user.phone == "not-a-phone"


@pytest.mark.django_db
def test_user_save_sets_unusable_password_for_empty():
    user = User(username="u4", password="")
    user.save()
    assert user.password.startswith("!")
    assert user.has_usable_password() is False


@pytest.mark.django_db
def test_user_save_generates_username_when_missing():
    user = User(username="")
    user.save()
    assert user.username.startswith("user_")
    assert len(user.username) == len("user_") + 8


# ---------------------------------------------------------------------------
# users.User — __str__ / anonymous lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_user_str_variants():
    named = User(username="bob")
    assert str(named) == "bob"
    mailed = User(username="bob", email="b@example.com")
    assert str(mailed) == "b@example.com"
    phoned = User(username="bob", phone="+79991234567")
    assert str(phoned) == "+79991234567"
    anon = User(username="anon_1", is_anonymous=True)
    assert str(anon) == f"Anonymous User {anon.id}"


@pytest.mark.django_db
def test_create_anonymous_user():
    user = User.create_anonymous_user()
    assert user.pk is not None
    assert user.is_anonymous is True
    assert user.auth_type == "anonymous"
    assert user.is_active is True
    assert user.username.startswith("anon_")
    assert user.anonymous_created_at is not None


@pytest.mark.django_db
def test_is_anonymous_expired():
    regular = User.objects.create(username="reg1")
    assert regular.is_anonymous_expired() is False

    fresh = User.create_anonymous_user()
    assert fresh.is_anonymous_expired() is False

    stale = User.create_anonymous_user()
    stale.anonymous_created_at = timezone.now() - timedelta(days=31)
    assert stale.is_anonymous_expired() is True

    with override_settings(ANONYMOUS_USER_LIFETIME=timedelta(days=60)):
        assert stale.is_anonymous_expired() is False


@pytest.mark.django_db
def test_upgrade_username_from_anonymous():
    user = User.objects.create(username="anon_abcd1234")
    user.upgrade_username_from_anonymous()
    assert user.username == "user_abcd1234"


@pytest.mark.django_db
def test_upgrade_username_from_anonymous_collision():
    User.objects.create(username="user_abcd1234")
    user = User.objects.create(username="anon_abcd1234")
    user.upgrade_username_from_anonymous()
    assert user.username != "user_abcd1234"
    assert user.username.startswith("user_")


@pytest.mark.django_db
def test_upgrade_username_noop_for_non_anonymous():
    user = User.objects.create(username="regular_name")
    user.upgrade_username_from_anonymous()
    assert user.username == "regular_name"
