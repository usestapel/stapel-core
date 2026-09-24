"""
Group management utilities for Django admin.

Provides automatic Staff group creation and user assignment.
"""

import logging
from typing import Optional

from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType

logger = logging.getLogger(__name__)

# Default Staff group name
STAFF_GROUP_NAME = 'Staff'


# ---------------------------------------------------------------------------
# Permissions that ACT, and therefore must never sit in a group fixture
# ---------------------------------------------------------------------------
#
# THE INCIDENT THIS ENCODES, 2026-09-16. `_ensure_user_in_staff_group` (see
# stapel_core.django.jwt.utils) enrols every mirrored non-superuser is_staff
# account into the Staff group on EVERY JWT request. So the Staff group is not
# a subset of staff — it IS staff. A fleet that had just built a deliberate
# split between "may look at wallets" and "may grant credits" put the new
# `grant_credits` permission into its Staff group fixture, and thereby handed
# the money to precisely the people the split existed to separate from it. The
# mistake survived review and was caught only by watching a view-only operator
# grant credits on a live stand.
#
# THE RULE, which is now a mechanism rather than a sentence in a comment:
#
#   The group carries a BASELINE OF VISIBILITY. Anything that ACTS — grants
#   money, mutates state, triggers a job — is granted PER OPERATOR and must
#   never appear in a group fixture.
#
# A permission is declared operator-only at its definition site: the library
# that adds it to a model's Meta.permissions registers it here, and
# `setup_staff_group_from_fixture` then REFUSES a fixture that names it. There
# is no escape hatch on purpose — a deployment that wants a person to hold it
# grants it to that person, which is the whole point.
#
# Entries are matched either bare ("grant_credits", any app) or app-qualified
# ("billing.grant_credits"). Bare is usually right: the codename is the verb.

# EMPTY BY DEFAULT, and `grant_credits` is deliberately NOT here.
#
# It was, for a few hours on 2026-09-17. The owner overruled it, and correctly:
# staff means QA or above in his deployment, a staff member should simply be
# able to get credits, and a per-operator step is a thing to remember on top of
# an ask he had already made twice. The security instinct was answering a
# question nobody had asked, and it made the affordance harder rather than
# safer.
#
# The mechanism stays because the shape is real — a group IS every staff
# member, so a genuinely destructive permission must not be grantable through
# one. It is simply not what "may top up a wallet" is. Register the ones that
# are, at their definition site.
OPERATOR_ONLY_PERMISSIONS: set = set()


class OperatorOnlyPermissionInFixture(Exception):
    """A group fixture named a permission that ACTS. See the block above."""


def register_operator_only_permission(codename: str) -> None:
    """Declare a permission operator-only, from the library that defines it.

    Call it from the app's ``AppConfig.ready()``, next to the model whose
    ``Meta.permissions`` introduces the codename, so the declaration and the
    definition are read together.
    """
    OPERATOR_ONLY_PERMISSIONS.add(codename)


def is_operator_only(app_label: str, codename: str) -> bool:
    """Is this permission one that must not be granted through a group."""
    return (
        codename in OPERATOR_ONLY_PERMISSIONS
        or f"{app_label}.{codename}" in OPERATOR_ONLY_PERMISSIONS
    )


def get_or_create_staff_group() -> Group:
    """
    Get or create the Staff group.

    Returns:
        Staff group instance
    """
    group, created = Group.objects.get_or_create(name=STAFF_GROUP_NAME)
    if created:
        logger.info(f"Created '{STAFF_GROUP_NAME}' group")
    return group


def add_user_to_staff_group(user) -> bool:
    """
    Add a staff user to the Staff group if not already a member.

    Only adds users who have is_staff=True.
    Superusers are not added (they have all permissions anyway).

    Args:
        user: User instance

    Returns:
        True if user was added, False otherwise
    """
    if not user.is_staff:
        return False

    if user.is_superuser:
        # Superusers don't need group membership
        return False

    group = get_or_create_staff_group()

    if not user.groups.filter(pk=group.pk).exists():
        user.groups.add(group)
        logger.info("Added user %s to '%s' group", user.pk, STAFF_GROUP_NAME)
        return True

    return False


def sync_staff_group(dry_run: bool = False) -> dict:
    """Make Staff-group membership follow the ``is_staff`` flag. Idempotent.

    THE SHAPE THIS CLOSES. A group with permissions and no members is the
    same defect as a fixture nobody imports: everything reads configured and
    nobody is actually granted anything. A fleet audited 2026-09-16 had a
    service whose Staff group carried thirteen permissions and had zero
    members, and another whose group had four members and no permissions.
    Both were "set up".

    Membership follows the flag, in BOTH directions, so that there is one
    list and not two:

    * every ``is_staff`` account is in the group;
    * every member that is no longer ``is_staff`` is removed from it.

    The removal half is the reason this is a mirror rather than a top-up. An
    account that loses ``is_staff`` keeps whatever the group grants until
    something takes it away, and "something" was a person remembering.

    SUPERUSERS ARE ENROLLED TOO, which is where this deliberately differs
    from :func:`add_user_to_staff_group` (that helper skips them, reasoning
    that they already have every permission). True today and irrelevant
    tomorrow: the moment a superuser is demoted to plain staff — the usual
    way an account is wound down — they would silently hold nothing, and
    nobody would connect the two events. Enrolling them costs nothing, since
    a superuser bypasses permission checks anyway, and it makes demotion a
    non-event.

    Returns a report: counts before and after, and the accounts on each side
    of the change, so a dry run is worth reading.
    """
    from django.contrib.auth import get_user_model

    User = get_user_model()
    group = get_or_create_staff_group()

    members_before = User.objects.filter(groups=group)
    to_add = list(User.objects.filter(is_staff=True).exclude(groups=group))
    to_remove = list(members_before.filter(is_staff=False))

    report = {
        "group": group.name,
        "permissions": group.permissions.count(),
        "members_before": members_before.count(),
        "added": [str(u.pk) for u in to_add],
        "removed": [str(u.pk) for u in to_remove],
        "dry_run": dry_run,
    }

    if not dry_run:
        for user in to_add:
            user.groups.add(group)
        for user in to_remove:
            user.groups.remove(group)

    report["members_after"] = (
        User.objects.filter(groups=group).count()
        if not dry_run
        else report["members_before"] + len(to_add) - len(to_remove)
    )
    logger.info(
        "staff group sync%s: %s member(s) -> %s, +%s -%s",
        " (dry run)" if dry_run else "",
        report["members_before"],
        report["members_after"],
        len(to_add),
        len(to_remove),
    )
    return report


def ensure_staff_group_permissions(
    app_label: str,
    model_permissions: Optional[dict] = None
) -> None:
    """
    Ensure Staff group has permissions for specified models.

    This function is idempotent - safe to call multiple times.

    Args:
        app_label: Django app label (e.g., 'profiles', 'auth')
        model_permissions: Dict mapping model names to permission codenames.
            If None, grants all CRUD permissions for all models in the app.
            Example: {
                'configlink': ['view_configlink', 'change_configlink'],
                'trafficlog': ['view_trafficlog'],
            }
    """
    group = get_or_create_staff_group()

    if model_permissions is None:
        # Get all content types for the app
        content_types = ContentType.objects.filter(app_label=app_label)
        for ct in content_types:
            permissions = Permission.objects.filter(content_type=ct)
            for perm in permissions:
                if not group.permissions.filter(pk=perm.pk).exists():
                    group.permissions.add(perm)
                    logger.debug(f"Added permission '{perm.codename}' to Staff group")
    else:
        for model_name, codenames in model_permissions.items():
            try:
                ct = ContentType.objects.get(app_label=app_label, model=model_name)
                for codename in codenames:
                    try:
                        perm = Permission.objects.get(content_type=ct, codename=codename)
                        if not group.permissions.filter(pk=perm.pk).exists():
                            group.permissions.add(perm)
                            logger.debug(f"Added permission '{codename}' to Staff group")
                    except Permission.DoesNotExist:
                        logger.warning(f"Permission '{codename}' not found for {app_label}.{model_name}")
            except ContentType.DoesNotExist:
                logger.warning(f"ContentType not found: {app_label}.{model_name}")


def setup_staff_group_from_fixture(fixture_path: str) -> dict:
    """Make the Staff group's permissions match the fixture. A MIRROR.

    Fixture format::

        {
            "group_name": "Staff",
            "permissions": [
                {"app_label": "vpn", "model": "configlink",
                 "codename": "view_configlink"},
                ...
            ]
        }

    MIRROR, NOT TOP-UP — changed 2026-09-17, and the reason is worth keeping.
    This used to only ever ADD. A fixture could therefore widen a group and
    never narrow one, so the only reachable direction was the unsafe one and
    de-granting required somebody to know to call ``permissions.set()`` by
    hand. That was discovered the way such things are: a corrected fixture was
    re-imported with ``--force`` and corrected nothing, silently, while
    reporting success. The fixture is the truth now; what is not in it is
    removed, and the return value says what went.

    Raises :class:`OperatorOnlyPermissionInFixture` when the fixture names a
    permission that ACTS rather than reveals — see the block at the top of
    this module. The group is every staff member, so such a permission in a
    group fixture grants it to all of them.

    Returns a report: ``{"group", "added", "removed", "missing"}``.
    """
    import json
    import os

    if not os.path.exists(fixture_path):
        logger.warning(f"Fixture file not found: {fixture_path}")
        return {"group": None, "added": [], "removed": [], "missing": []}

    with open(fixture_path, 'r') as f:
        data = json.load(f)

    group_name = data.get('group_name', STAFF_GROUP_NAME)
    permissions_data = data.get('permissions', [])

    # Refuse BEFORE touching the group: a partial application of a fixture
    # that is wrong in principle is worse than not applying it at all.
    offenders = [
        f"{p['app_label']}.{p['codename']}"
        for p in permissions_data
        if is_operator_only(p.get('app_label', ''), p.get('codename', ''))
    ]
    if offenders:
        raise OperatorOnlyPermissionInFixture(
            f"{fixture_path} names {', '.join(offenders)}, which act rather "
            f"than reveal. The Staff group is every staff member (the JWT "
            f"mirror enrols them on sight), so a permission granted through "
            f"it is held by all of them. Grant it to the individual operator "
            f"instead, and keep the group to a baseline of visibility."
        )

    group, _ = Group.objects.get_or_create(name=group_name)

    wanted = []
    missing = []
    for perm_data in permissions_data:
        try:
            ct = ContentType.objects.get(
                app_label=perm_data['app_label'],
                model=perm_data['model']
            )
            wanted.append(
                Permission.objects.get(content_type=ct, codename=perm_data['codename'])
            )
        except (ContentType.DoesNotExist, Permission.DoesNotExist) as e:
            # Named but absent: usually a model that has been renamed or
            # removed and a fixture nobody re-exported. Reported, never
            # silently dropped — that is how a fixture rots unnoticed.
            missing.append(f"{perm_data.get('app_label')}.{perm_data.get('codename')}")
            logger.warning(f"Could not resolve permission {perm_data}: {e}")

    have = set(group.permissions.all())
    want = set(wanted)
    added = sorted(f"{p.content_type.app_label}.{p.codename}" for p in want - have)
    removed = sorted(f"{p.content_type.app_label}.{p.codename}" for p in have - want)

    group.permissions.set(wanted)

    if added or removed or missing:
        logger.info(
            "staff group '%s' from fixture: +%s -%s, %s unresolved",
            group_name, len(added), len(removed), len(missing),
        )
    return {
        "group": group_name,
        "added": added,
        "removed": removed,
        "missing": missing,
    }


def export_staff_group_fixture(output_path: str) -> dict:
    """Write the Staff group's current permissions out as a JSON fixture.

    REFUSES when the group currently holds a permission that ACTS, for the
    same reason the import does — and this is the side that was missing until
    0.77.0. Guarding only the read left the loop closable the wrong way round:
    a superuser adds `grant_credits` to the group by hand, `export` dutifully
    writes it into the fixture, and the next `import` accepts it as canon
    because by then it IS the fixture. The mechanism would have been defeated
    through the one path nobody checked.

    So export refuses too, and its message points at the group rather than the
    file: the fixture is not yet wrong, the GROUP is, and the fix is to take
    the permission off the group and grant it to the operator who needs it.

    Returns ``{"path", "permissions"}``.
    """
    import json

    try:
        group = Group.objects.get(name=STAFF_GROUP_NAME)
    except Group.DoesNotExist:
        logger.warning(f"'{STAFF_GROUP_NAME}' group not found")
        return {"path": None, "permissions": []}

    rows = list(group.permissions.select_related("content_type").all())

    offenders = [
        f"{p.content_type.app_label}.{p.codename}"
        for p in rows
        if is_operator_only(p.content_type.app_label, p.codename)
    ]
    if offenders:
        raise OperatorOnlyPermissionInFixture(
            f"the '{STAFF_GROUP_NAME}' group currently holds "
            f"{', '.join(offenders)}, which act rather than reveal. Exporting "
            f"would write them into the fixture and the next import would "
            f"accept them as canon. The group is every staff member (the JWT "
            f"mirror enrols them on sight), so REMOVE the permission from the "
            f"group and grant it to the individual operator, then export."
        )

    permissions = [
        {
            'app_label': p.content_type.app_label,
            'model': p.content_type.model,
            'codename': p.codename,
        }
        for p in rows
    ]
    data = {
        'group_name': STAFF_GROUP_NAME,
        'permissions': sorted(
            permissions, key=lambda x: (x['app_label'], x['model'], x['codename'])
        ),
    }

    with open(output_path, 'w') as f:
        json.dump(data, f, indent=2)

    logger.info(f"Exported {len(permissions)} permissions to {output_path}")
    return {"path": output_path, "permissions": data['permissions']}


def load_staff_group_if_empty(fixture_path: str) -> bool:
    """
    Load Staff group permissions from fixture only if group has no permissions.

    This is useful for initial setup - won't override manual changes.

    Args:
        fixture_path: Path to the JSON fixture file

    Returns:
        True if fixture was loaded, False if group already has permissions
    """
    group = get_or_create_staff_group()

    if group.permissions.exists():
        logger.debug(f"'{STAFF_GROUP_NAME}' group already has permissions, skipping fixture load")
        return False

    setup_staff_group_from_fixture(fixture_path)
    return True
