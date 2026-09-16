"""
Management command for Staff group permissions.

Usage:
    # Export current Staff group permissions to fixture
    python manage.py staff_group export

    # Import Staff group permissions from fixture (only if group is empty)
    python manage.py staff_group import

    # Import Staff group permissions (force overwrite)
    python manage.py staff_group import --force

    # Show current Staff group permissions
    python manage.py staff_group show

    # Make membership follow the is_staff flag (idempotent; --dry-run first)
    python manage.py staff_group sync --dry-run
    python manage.py staff_group sync

MEMBERSHIP FOLLOWS THE FLAG. `sync` is the only thing that should decide who
is in the Staff group: it enrols every is_staff account and removes every
member that is no longer one. Do not also keep a hand-maintained list — two
lists is how a group ends up with permissions and no members, which reads as
configured and grants nobody anything.
"""

import os
from django.core.management.base import BaseCommand, CommandError
from django.conf import settings


class Command(BaseCommand):
    help = 'Manage Staff group permissions'

    def add_arguments(self, parser):
        parser.add_argument(
            'action',
            choices=['export', 'import', 'show', 'setup', 'sync'],
            help='Action to perform: export, import, show, setup, or sync'
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Force import even if group has existing permissions'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='sync: report what would change, write nothing',
        )
        parser.add_argument(
            '--file',
            type=str,
            help='Custom fixture file path (default: fixtures/staff_group.json)'
        )

    def get_fixture_path(self, options):
        """Get the fixture file path."""
        if options.get('file'):
            return options['file']

        # Default path: service's fixtures/staff_group.json
        base_dir = getattr(settings, 'BASE_DIR', '.')
        return os.path.join(base_dir, 'fixtures', 'staff_group.json')

    def handle(self, *_args, **options):
        action = options['action']

        if action == 'export':
            self.handle_export(options)
        elif action == 'import':
            self.handle_import(options)
        elif action == 'show':
            self.handle_show(options)
        elif action == 'setup':
            self.handle_setup(options)
        elif action == 'sync':
            self.handle_sync(options)

    def handle_export(self, options):
        """Export Staff group permissions to fixture file."""
        from stapel_core.django.groups import export_staff_group_fixture

        fixture_path = self.get_fixture_path(options)

        # Ensure directory exists
        os.makedirs(os.path.dirname(fixture_path), exist_ok=True)

        export_staff_group_fixture(fixture_path)
        self.stdout.write(self.style.SUCCESS(f'Exported Staff group to {fixture_path}'))

    def handle_import(self, options):
        """Import Staff group permissions from fixture file."""
        from stapel_core.django.groups import (
            setup_staff_group_from_fixture,
            load_staff_group_if_empty,
        )

        fixture_path = self.get_fixture_path(options)

        if not os.path.exists(fixture_path):
            raise CommandError(f'Fixture file not found: {fixture_path}')

        from stapel_core.django.groups import OperatorOnlyPermissionInFixture

        try:
            if options['force']:
                report = setup_staff_group_from_fixture(fixture_path)
                self.stdout.write(
                    self.style.SUCCESS(f'Force-imported Staff group from {fixture_path}')
                )
                self._report_import(report)
            else:
                if load_staff_group_if_empty(fixture_path):
                    self.stdout.write(
                        self.style.SUCCESS(f'Imported Staff group from {fixture_path}')
                    )
                else:
                    self.stdout.write(
                        self.style.WARNING(
                            'Staff group already has permissions, skipping import'
                        )
                    )
        except OperatorOnlyPermissionInFixture as exc:
            # A CommandError, so the bootstrap's `require` aborts the boot: a
            # deployment whose group fixture would hand an acting permission
            # to every staff member should not start with it half-applied.
            raise CommandError(str(exc)) from None

    def _report_import(self, report):
        """Say what the mirror moved — especially what it took away."""
        for name in report.get('added', []):
            self.stdout.write(self.style.SUCCESS(f'  + {name}'))
        for name in report.get('removed', []):
            # The half that used to be impossible: before 0.75.0 an import
            # could only ever widen a group.
            self.stdout.write(self.style.WARNING(f'  - {name}  (not in the fixture)'))
        for name in report.get('missing', []):
            self.stdout.write(self.style.ERROR(
                f'  ? {name}  named by the fixture but no such permission here '
                f'— renamed or removed model; re-export'
            ))
        if not any(report.get(k) for k in ('added', 'removed', 'missing')):
            self.stdout.write('  already in step')

    def handle_sync(self, options):
        """Make Staff-group membership follow the is_staff flag."""
        from stapel_core.django.groups import sync_staff_group

        dry_run = bool(options.get('dry_run'))
        report = sync_staff_group(dry_run=dry_run)

        self.stdout.write(f"group        {report['group']}")
        self.stdout.write(f"permissions  {report['permissions']}")
        self.stdout.write(
            f"members      {report['members_before']} -> {report['members_after']}"
        )
        for pk in report['added']:
            self.stdout.write(self.style.SUCCESS(f"  + {pk[:8]}  (is_staff)"))
        for pk in report['removed']:
            self.stdout.write(self.style.WARNING(f"  - {pk[:8]}  (no longer is_staff)"))

        if not report['added'] and not report['removed']:
            self.stdout.write(self.style.SUCCESS('already in step — nothing to do'))
        elif dry_run:
            self.stdout.write(self.style.WARNING('dry run — nothing was written'))

        if report['permissions'] == 0 and report['members_after']:
            # Worth saying out loud: members of a group that grants nothing
            # can log into the admin and act on nothing, which is the defect
            # this command's neighbours exist to fix.
            self.stdout.write(self.style.WARNING(
                'NOTE: this group grants NO permissions, so its members can '
                'reach the admin and act on nothing. Import a fixture.'
            ))

    def handle_show(self, _options):
        """Show current Staff group permissions."""
        from django.contrib.auth.models import Group

        try:
            group = Group.objects.get(name='Staff')
        except Group.DoesNotExist:
            self.stdout.write(self.style.WARNING('Staff group does not exist'))
            return

        permissions = group.permissions.all().order_by(
            'content_type__app_label',
            'content_type__model',
            'codename'
        )

        if not permissions:
            self.stdout.write(self.style.WARNING('Staff group has no permissions'))
            return

        self.stdout.write(self.style.SUCCESS(f'Staff group has {permissions.count()} permissions:'))
        self.stdout.write('')

        current_app = None
        for perm in permissions:
            app_label = perm.content_type.app_label
            if app_label != current_app:
                current_app = app_label
                self.stdout.write(self.style.MIGRATE_HEADING(f'  {app_label}:'))

            self.stdout.write(f'    - {perm.content_type.model}.{perm.codename}')

    def handle_setup(self, _options):
        """Setup Staff group with all permissions for current app models."""
        from django.contrib.auth.models import Group, Permission
        from django.contrib.contenttypes.models import ContentType

        group, created = Group.objects.get_or_create(name='Staff')

        if created:
            self.stdout.write(self.style.SUCCESS('Created Staff group'))

        # Get all installed apps that are part of this service
        installed_apps = settings.INSTALLED_APPS

        # Filter to only local apps (not django.*, rest_framework.*, etc.)
        local_apps = []
        for app in installed_apps:
            if not app.startswith('django.') and not app.startswith('rest_framework'):
                # Check if it's a local app (has models)
                try:
                    ct_count = ContentType.objects.filter(app_label=app.split('.')[-1]).count()
                    if ct_count > 0:
                        local_apps.append(app.split('.')[-1])
                except Exception:
                    pass

        if not local_apps:
            self.stdout.write(self.style.WARNING('No local apps with models found'))
            return

        self.stdout.write(f'Found local apps: {", ".join(local_apps)}')

        added_count = 0
        for app_label in local_apps:
            content_types = ContentType.objects.filter(app_label=app_label)
            for ct in content_types:
                permissions = Permission.objects.filter(content_type=ct)
                for perm in permissions:
                    if not group.permissions.filter(pk=perm.pk).exists():
                        group.permissions.add(perm)
                        added_count += 1
                        self.stdout.write(f'  Added: {app_label}.{perm.codename}')

        if added_count:
            self.stdout.write(self.style.SUCCESS(f'Added {added_count} permissions to Staff group'))
        else:
            self.stdout.write('No new permissions to add')
