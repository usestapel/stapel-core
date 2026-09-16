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
        logger.info(f"Added user '{user.email}' to '{STAFF_GROUP_NAME}' group")
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


def setup_staff_group_from_fixture(fixture_path: str) -> None:
    """
    Load Staff group permissions from a JSON fixture file.

    Fixture format:
    {
        "group_name": "Staff",
        "permissions": [
            {"app_label": "vpn", "model": "configlink", "codename": "view_configlink"},
            ...
        ]
    }

    Args:
        fixture_path: Path to the JSON fixture file
    """
    import json
    import os

    if not os.path.exists(fixture_path):
        logger.warning(f"Fixture file not found: {fixture_path}")
        return

    with open(fixture_path, 'r') as f:
        data = json.load(f)

    group_name = data.get('group_name', STAFF_GROUP_NAME)
    group, _ = Group.objects.get_or_create(name=group_name)

    permissions_data = data.get('permissions', [])
    added_count = 0

    for perm_data in permissions_data:
        try:
            ct = ContentType.objects.get(
                app_label=perm_data['app_label'],
                model=perm_data['model']
            )
            perm = Permission.objects.get(
                content_type=ct,
                codename=perm_data['codename']
            )
            if not group.permissions.filter(pk=perm.pk).exists():
                group.permissions.add(perm)
                added_count += 1
        except (ContentType.DoesNotExist, Permission.DoesNotExist) as e:
            logger.warning(f"Could not add permission {perm_data}: {e}")

    if added_count:
        logger.info(f"Added {added_count} permissions to '{group_name}' group from fixture")


def export_staff_group_fixture(output_path: str) -> None:
    """
    Export current Staff group permissions to a JSON fixture file.

    Args:
        output_path: Path to write the JSON fixture file
    """
    import json

    try:
        group = Group.objects.get(name=STAFF_GROUP_NAME)
    except Group.DoesNotExist:
        logger.warning(f"'{STAFF_GROUP_NAME}' group not found")
        return

    permissions = []
    for perm in group.permissions.all():
        permissions.append({
            'app_label': perm.content_type.app_label,
            'model': perm.content_type.model,
            'codename': perm.codename,
        })

    data = {
        'group_name': STAFF_GROUP_NAME,
        'permissions': sorted(permissions, key=lambda x: (x['app_label'], x['model'], x['codename']))
    }

    with open(output_path, 'w') as f:
        json.dump(data, f, indent=2)

    logger.info(f"Exported {len(permissions)} permissions to {output_path}")


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
