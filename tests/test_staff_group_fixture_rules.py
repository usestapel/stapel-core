"""The two rules a group fixture must obey, as mechanisms rather than prose.

Both come from one incident, 2026-09-16/17, and neither was visible in review.

1. THE GROUP IS EVERY STAFF MEMBER. `_ensure_user_in_staff_group` enrols every
   mirrored non-superuser is_staff account into the Staff group on every JWT
   request, so a permission placed in a group fixture is held by all of them.
   A fleet that had just built a deliberate split between "may look at
   wallets" and "may grant credits" put the new `grant_credits` into its
   fixture and handed the money to exactly the people the split existed to
   separate from it. Caught only by watching a view-only operator grant
   credits on a live stand.

2. THE IMPORT COULD ONLY WIDEN. It added and never removed, so the fixture
   could grant and never take back, and the only reachable direction was the
   unsafe one. Discovered when a corrected fixture was re-imported with
   --force and corrected nothing, silently, while reporting success.
"""
import json

import pytest
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType

from stapel_core.django.groups import (
    STAFF_GROUP_NAME,
    OperatorOnlyPermissionInFixture,
    export_staff_group_fixture,
    is_operator_only,
    register_operator_only_permission,
    setup_staff_group_from_fixture,
)


def _fixture(tmp_path, perms, name="Staff"):
    path = tmp_path / "staff_group.json"
    path.write_text(json.dumps({"group_name": name, "permissions": perms}))
    return str(path)


def _perm(codename):
    """A real permission on a model every Django install has."""
    ct = ContentType.objects.get_for_model(Permission)
    return Permission.objects.get(content_type=ct, codename=codename)


def _entry(codename):
    ct = ContentType.objects.get_for_model(Permission)
    return {
        "app_label": ct.app_label,
        "model": ct.model,
        "codename": codename,
    }


@pytest.mark.django_db
class TestAnActingPermissionIsRefused:
    def test_grant_credits_is_operator_only_out_of_the_box(self):
        assert is_operator_only("billing", "grant_credits") is True

    def test_a_fixture_naming_one_is_refused(self, tmp_path):
        ct = ContentType.objects.get_for_model(Permission)
        path = _fixture(
            tmp_path,
            [{"app_label": ct.app_label, "model": ct.model, "codename": "grant_credits"}],
        )
        with pytest.raises(OperatorOnlyPermissionInFixture) as exc:
            setup_staff_group_from_fixture(path)
        # It names the permission AND says why, because "refused" without a
        # reason is how a rule gets worked around instead of understood.
        assert "grant_credits" in str(exc.value)
        assert "every staff member" in str(exc.value)

    def test_nothing_is_applied_when_one_entry_offends(self, tmp_path):
        """A partial application of a fixture wrong in principle is worse."""
        ct = ContentType.objects.get_for_model(Permission)
        path = _fixture(
            tmp_path,
            [
                _entry("view_permission"),
                {"app_label": ct.app_label, "model": ct.model, "codename": "grant_credits"},
            ],
        )
        with pytest.raises(OperatorOnlyPermissionInFixture):
            setup_staff_group_from_fixture(path)
        group = Group.objects.filter(name=STAFF_GROUP_NAME).first()
        assert group is None or group.permissions.count() == 0

    def test_a_library_can_declare_its_own_at_the_definition_site(self, tmp_path):
        register_operator_only_permission("trigger_reindex")
        assert is_operator_only("anything", "trigger_reindex") is True
        path = _fixture(tmp_path, [_entry("trigger_reindex")])
        with pytest.raises(OperatorOnlyPermissionInFixture):
            setup_staff_group_from_fixture(path)
        OPERATOR_ONLY = __import__(
            "stapel_core.django.groups", fromlist=["OPERATOR_ONLY_PERMISSIONS"]
        ).OPERATOR_ONLY_PERMISSIONS
        OPERATOR_ONLY.discard("trigger_reindex")

    def test_an_app_qualified_declaration_only_binds_that_app(self, tmp_path):
        register_operator_only_permission("billing.settle_debt")
        assert is_operator_only("billing", "settle_debt") is True
        assert is_operator_only("shop", "settle_debt") is False
        OPERATOR_ONLY = __import__(
            "stapel_core.django.groups", fromlist=["OPERATOR_ONLY_PERMISSIONS"]
        ).OPERATOR_ONLY_PERMISSIONS
        OPERATOR_ONLY.discard("billing.settle_debt")


@pytest.mark.django_db
class TestTheImportIsAMirror:
    def test_it_adds_what_the_fixture_names(self, tmp_path):
        report = setup_staff_group_from_fixture(_fixture(tmp_path, [_entry("view_permission")]))
        group = Group.objects.get(name=STAFF_GROUP_NAME)
        assert {p.codename for p in group.permissions.all()} == {"view_permission"}
        assert report["added"] and not report["removed"]

    def test_it_REMOVES_what_the_fixture_stopped_naming(self, tmp_path):
        """The half that was impossible before 0.75.0."""
        group = Group.objects.create(name=STAFF_GROUP_NAME)
        group.permissions.add(_perm("view_permission"), _perm("add_permission"))

        report = setup_staff_group_from_fixture(
            _fixture(tmp_path, [_entry("view_permission")])
        )

        assert {p.codename for p in group.permissions.all()} == {"view_permission"}
        assert any("add_permission" in r for r in report["removed"])

    def test_re_importing_a_corrected_fixture_actually_corrects(self, tmp_path):
        # Exactly the production sequence that exposed this: import, notice
        # the fixture was wrong, correct it, re-import — and watch nothing
        # change while success is reported.
        setup_staff_group_from_fixture(
            _fixture(tmp_path, [_entry("view_permission"), _entry("add_permission")])
        )
        setup_staff_group_from_fixture(_fixture(tmp_path, [_entry("view_permission")]))
        group = Group.objects.get(name=STAFF_GROUP_NAME)
        assert {p.codename for p in group.permissions.all()} == {"view_permission"}

    def test_it_is_idempotent(self, tmp_path):
        path = _fixture(tmp_path, [_entry("view_permission")])
        setup_staff_group_from_fixture(path)
        report = setup_staff_group_from_fixture(path)
        assert not report["added"] and not report["removed"]

    def test_a_permission_the_fixture_names_but_this_service_lacks_is_reported(
        self, tmp_path
    ):
        # A renamed or removed model leaves a fixture naming something that no
        # longer exists. It used to be logged and forgotten; now it comes back
        # in the report so the fixture gets re-exported instead of rotting.
        ct = ContentType.objects.get_for_model(Permission)
        path = _fixture(
            tmp_path,
            [
                _entry("view_permission"),
                {"app_label": ct.app_label, "model": ct.model, "codename": "view_ghost"},
            ],
        )
        report = setup_staff_group_from_fixture(path)
        assert any("view_ghost" in m for m in report["missing"])
        # ...and the resolvable half still applied.
        assert Group.objects.get(name=STAFF_GROUP_NAME).permissions.count() == 1


@pytest.mark.django_db
class TestExportIsGuardedToo:
    """The loop must not be closable the wrong way round.

    Guarding only the import left one path open: a superuser puts the acting
    permission on the group by hand, `export` writes it into the fixture, and
    the next `import` accepts it as canon — because by then it IS the fixture.
    """

    def test_exporting_a_group_that_holds_an_acting_permission_is_refused(
        self, tmp_path
    ):
        ct = ContentType.objects.get_for_model(Permission)
        Permission.objects.get_or_create(
            content_type=ct, codename="grant_credits",
            defaults={"name": "Can grant credits by hand"},
        )
        group = Group.objects.create(name=STAFF_GROUP_NAME)
        group.permissions.add(_perm("view_permission"), _perm("grant_credits"))

        out = tmp_path / "staff_group.json"
        with pytest.raises(OperatorOnlyPermissionInFixture) as exc:
            export_staff_group_fixture(str(out))

        # It blames the GROUP, not the file — the fixture is not wrong yet.
        assert "group currently holds" in str(exc.value)
        assert "grant_credits" in str(exc.value)
        # And nothing was written: a half-written fixture is a fixture.
        assert not out.exists()

    def test_a_clean_group_still_exports(self, tmp_path):
        group = Group.objects.create(name=STAFF_GROUP_NAME)
        group.permissions.add(_perm("view_permission"))
        out = tmp_path / "staff_group.json"
        report = export_staff_group_fixture(str(out))
        assert out.exists()
        assert [p["codename"] for p in report["permissions"]] == ["view_permission"]

    def test_export_then_import_round_trips(self, tmp_path):
        group = Group.objects.create(name=STAFF_GROUP_NAME)
        group.permissions.add(_perm("view_permission"), _perm("add_permission"))
        out = tmp_path / "staff_group.json"
        export_staff_group_fixture(str(out))
        group.permissions.clear()
        setup_staff_group_from_fixture(str(out))
        assert {p.codename for p in group.permissions.all()} == {
            "view_permission", "add_permission",
        }
