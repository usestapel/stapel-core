"""The taskstore's initial migration must ADOPT a pre-rename table.

Core 0.8.0 renamed this app's label ``stapel_tasks`` → ``stapel_taskstore``
and pinned ``db_table`` to the historical name, so the table never moved —
but the migration STATE stayed under the old label. On every deployment
that had already migrated before 0.8.0, the "new" app looked unapplied and
a plain CreateModel hit `relation "stapel_tasks_taskrecord" already
exists`, killing `manage.py migrate` at boot. Fresh installs were fine,
which is why it stayed invisible until a real upgrade (ironmemo stand,
2026-07-25).

These tests exercise both paths through the executor, on the real
connection: absent table → created, pre-existing table → adopted with its
rows intact.
"""
import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

TABLE = "stapel_tasks_taskrecord"
APP = "stapel_taskstore"


def _table_exists() -> bool:
    return TABLE in connection.introspection.table_names()


def _migrate(target):
    """Run the executor to *target* ([] = unapply everything)."""
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(target)


@pytest.fixture
def at_zero():
    """Unapply the app (drops the table), restore it afterwards."""
    _migrate([(APP, None)])
    assert not _table_exists()
    yield
    _migrate([(APP, "0001_initial")])
    assert _table_exists()


@pytest.mark.django_db(transaction=True)
def test_creates_the_table_when_absent(at_zero):
    _migrate([(APP, "0001_initial")])
    assert _table_exists()


@pytest.mark.django_db(transaction=True)
def test_adopts_a_table_left_by_the_pre_rename_app(at_zero):
    """The pre-0.8.0 app created this exact table under its own label; the
    upgrade must record the migration as applied WITHOUT re-creating it,
    and without touching the rows already in there."""
    from stapel_core.django.taskstore.models import TaskRecord

    # Stand in for the old app: same table, made outside this app's history.
    with connection.schema_editor() as schema_editor:
        schema_editor.create_model(TaskRecord)
    assert _table_exists()
    with connection.cursor() as cursor:
        # The stand-in table is built from the CURRENT model, so it carries
        # the columns added since — Django's defaults are applied in Python,
        # not by the database, and a raw INSERT gets none of them. Spelling
        # them out keeps this test about migration ADOPTION rather than
        # about which columns the model happens to have this release.
        cursor.execute(
            f"INSERT INTO {TABLE} (id, kind, payload, state, error, attempts, "
            f"max_attempts, correlation_id, callback, created_at, "
            f"failure_reason, dedupe_key) "
            f"VALUES ('11111111-1111-1111-1111-111111111111', 'legacy.kind', "
            f"'{{}}', 'pending', '', 0, 3, '', '', '2026-01-01 00:00:00', "
            f"'', '')"
        )

    _migrate([(APP, "0001_initial")])  # must not raise

    assert _table_exists()
    row = TaskRecord.objects.get(kind="legacy.kind")
    assert str(row.id) == "11111111-1111-1111-1111-111111111111"
