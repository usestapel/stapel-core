"""
Management command to reset PostgreSQL sequences.

This fixes the issue where sequences get out of sync with actual data
after bulk imports, fixture loading, or data migrations.

Usage:
    # Reset all sequences for all models
    python manage.py reset_sequences

    # Reset sequences for specific apps
    python manage.py reset_sequences --apps categories ads

    # Show what would be done without executing
    python manage.py reset_sequences --dry-run
"""

from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = 'Reset PostgreSQL sequences to match current max ID values'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apps',
            nargs='+',
            type=str,
            help='Only reset sequences for these apps (default: all)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be done without executing',
        )

    def handle(self, *args, **options):
        del args  # unused, required by Django
        app_labels = options.get('apps')
        dry_run = options.get('dry_run', False)

        if dry_run:
            self.stdout.write(self.style.WARNING('[DRY RUN] No changes will be made'))

        # Get all models
        all_models = apps.get_models()

        # Filter by app if specified
        if app_labels:
            all_models = [
                m for m in all_models
                if m._meta.app_label in app_labels
            ]

        reset_count = 0

        for model in all_models:
            # Skip models without auto-increment primary key
            pk_field = model._meta.pk
            if pk_field is None:
                continue

            # Only handle AutoField and BigAutoField
            field_type = type(pk_field).__name__
            if field_type not in ('AutoField', 'BigAutoField'):
                continue

            table_name = model._meta.db_table
            pk_column = pk_field.column
            sequence_name = f'{table_name}_{pk_column}_seq'

            # Get max ID from table
            with connection.cursor() as cursor:
                try:
                    cursor.execute(f'SELECT MAX("{pk_column}") FROM "{table_name}"')
                    row = cursor.fetchone()
                    max_id = row[0] if row else None
                except Exception as e:
                    self.stdout.write(
                        self.style.WARNING(f'  Skipping {table_name}: {e}')
                    )
                    continue

            if max_id is None:
                # Table is empty, reset to 1
                max_id = 0

            # Get current sequence value
            with connection.cursor() as cursor:
                try:
                    cursor.execute(f"SELECT last_value FROM \"{sequence_name}\"")
                    row = cursor.fetchone()
                    current_val = row[0] if row else 0
                except Exception:
                    # Sequence might not exist or have different name
                    continue

            if current_val < max_id:
                if dry_run:
                    self.stdout.write(
                        f'  Would reset {sequence_name}: {current_val} -> {max_id}'
                    )
                else:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            f"SELECT setval('\"{sequence_name}\"', %s)",
                            [max_id]
                        )
                    self.stdout.write(
                        self.style.SUCCESS(
                            f'  Reset {sequence_name}: {current_val} -> {max_id}'
                        )
                    )
                reset_count += 1

        if reset_count:
            action = 'would reset' if dry_run else 'reset'
            self.stdout.write(
                self.style.SUCCESS(f'Done: {action} {reset_count} sequence(s)')
            )
        else:
            self.stdout.write('All sequences are up to date')
