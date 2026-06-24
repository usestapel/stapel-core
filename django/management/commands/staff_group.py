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
"""

import os
from django.core.management.base import BaseCommand, CommandError
from django.conf import settings


class Command(BaseCommand):
    help = 'Manage Staff group permissions'

    def add_arguments(self, parser):
        parser.add_argument(
            'action',
            choices=['export', 'import', 'show', 'setup'],
            help='Action to perform: export, import, show, or setup'
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Force import even if group has existing permissions'
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

        if options['force']:
            setup_staff_group_from_fixture(fixture_path)
            self.stdout.write(self.style.SUCCESS(f'Force-imported Staff group from {fixture_path}'))
        else:
            if load_staff_group_if_empty(fixture_path):
                self.stdout.write(self.style.SUCCESS(f'Imported Staff group from {fixture_path}'))
            else:
                self.stdout.write(self.style.WARNING('Staff group already has permissions, skipping import'))

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
