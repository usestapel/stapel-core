"""`staff_group sync` — membership follows the is_staff flag, both ways.

A group with permissions and no members is the same defect as a fixture
nobody imports: everything reads configured and nobody is granted anything.
A fleet audited 2026-09-16 had one service whose Staff group carried thirteen
permissions and had zero members, and another whose group had four members
and no permissions. Both were "set up".

These pin the mirror, because a top-up that never removes anybody is how two
lists appear and drift.
"""
import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

from stapel_core.django.groups import STAFF_GROUP_NAME, sync_staff_group


def _user(username, **flags):
    return get_user_model().objects.create_user(
        username=username, password="syncpass12345", **flags
    )


@pytest.mark.django_db
class TestSyncEnrolsEveryStaffAccount:
    def test_a_staff_account_is_added(self):
        user = _user("operator", is_staff=True)
        report = sync_staff_group()
        assert str(user.pk) in report["added"]
        assert user.groups.filter(name=STAFF_GROUP_NAME).exists()

    def test_a_superuser_is_added_too(self):
        """Deliberately unlike add_user_to_staff_group, which skips them.

        True that a superuser needs no permissions today. The moment one is
        demoted to plain staff — the usual way an account is wound down —
        they would silently hold nothing, and nobody would connect the two
        events.
        """
        root = _user("root", is_staff=True, is_superuser=True)
        sync_staff_group()
        assert root.groups.filter(name=STAFF_GROUP_NAME).exists()

    def test_a_non_staff_account_is_left_alone(self):
        customer = _user("customer")
        report = sync_staff_group()
        assert str(customer.pk) not in report["added"]
        assert not customer.groups.exists()


@pytest.mark.django_db
class TestSyncRemovesWhatNoLongerQualifies:
    def test_a_demoted_member_is_removed(self):
        user = _user("leaver", is_staff=True)
        sync_staff_group()
        assert user.groups.filter(name=STAFF_GROUP_NAME).exists()

        # The flag goes; the membership must follow it without anybody
        # remembering to do it.
        user.is_staff = False
        user.save(update_fields=["is_staff"])
        report = sync_staff_group()

        assert str(user.pk) in report["removed"]
        assert not user.groups.filter(name=STAFF_GROUP_NAME).exists()


@pytest.mark.django_db
class TestSyncIsIdempotentAndDryRunnable:
    def test_running_twice_changes_nothing_the_second_time(self):
        _user("operator", is_staff=True)
        first = sync_staff_group()
        second = sync_staff_group()
        assert first["added"] and not second["added"]
        assert not second["removed"]
        assert second["members_before"] == second["members_after"]

    def test_dry_run_reports_without_writing(self):
        user = _user("operator", is_staff=True)
        report = sync_staff_group(dry_run=True)

        assert report["dry_run"] is True
        assert str(user.pk) in report["added"]
        # The number it promises is the number a real run would produce...
        assert report["members_after"] == 1
        # ...and nothing moved.
        assert not user.groups.exists()
        assert Group.objects.filter(name=STAFF_GROUP_NAME).exists()

    def test_the_dry_run_prediction_matches_the_real_run(self):
        _user("a", is_staff=True)
        _user("b", is_staff=True, is_superuser=True)
        _user("c")
        predicted = sync_staff_group(dry_run=True)
        actual = sync_staff_group()
        assert predicted["added"] == actual["added"]
        assert predicted["members_after"] == actual["members_after"] == 2
