"""Initial taskstore schema — written to be ADOPTABLE.

The app label was renamed ``stapel_tasks`` → ``stapel_taskstore`` in core
0.8.0 (the user-facing task/kanban module took over the old label) with
``Meta.db_table`` pinned to the historical name, so the physical table
never moved. What did NOT move is the migration STATE: a database that
migrated before 0.8.0 records those rows under the old app label, so this
app arrives looking unapplied and a plain ``CreateModel`` walks straight
into `relation "stapel_tasks_taskrecord" already exists` — every upgrade
of a pre-0.8.0 deployment dies inside `manage.py migrate`, while fresh
installs are fine (which is exactly why it stayed invisible until the
ironmemo stand upgraded, 2026-07-25).

``replaces = [("stapel_tasks", …)]`` is NOT usable here: the old label now
belongs to a different, real app, and claiming its migrations would
swallow that module's own history.

So the DDL is skipped when the table is already there. The state is built
either way, and the operation still uses the HISTORICAL model from
``to_state`` (never the live one), so a fresh database gets exactly the
0001-era shape and later migrations apply their deltas normally.
"""

import uuid

from django.db import migrations, models


class CreateModelIfAbsent(migrations.CreateModel):
    """CreateModel that adopts a table created outside this app's history.

    Same state effect as ``CreateModel``; the DDL (table + its Meta
    indexes) runs only when the table is genuinely missing.
    """

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(app_label, self.name)
        if model._meta.db_table in schema_editor.connection.introspection.table_names():
            return
        super().database_forwards(app_label, schema_editor, from_state, to_state)


class AddIndexIfAbsent(migrations.AddIndex):
    """AddIndex that tolerates the index the pre-rename app already made."""

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(app_label, self.model_name)
        with schema_editor.connection.cursor() as cursor:
            existing = schema_editor.connection.introspection.get_constraints(
                cursor, model._meta.db_table
            )
        if self.index.name in existing:
            return
        super().database_forwards(app_label, schema_editor, from_state, to_state)


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        CreateModelIfAbsent(
            name="TaskRecord",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("kind", models.CharField(db_index=True, max_length=255)),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("state", models.CharField(choices=[("pending", "Pending"), ("running", "Running"), ("done", "Done"), ("failed", "Failed")], db_index=True, default="pending", max_length=16)),
                ("result", models.JSONField(blank=True, null=True)),
                ("error", models.TextField(blank=True, default="")),
                ("attempts", models.PositiveIntegerField(default=0)),
                ("max_attempts", models.PositiveIntegerField(default=3)),
                ("deadline", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("correlation_id", models.CharField(blank=True, db_index=True, default="", max_length=255)),
                ("callback", models.CharField(blank=True, default="", max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={"ordering": ["created_at"], "db_table": "stapel_tasks_taskrecord"},
        ),
        # The index rides the same guard: on the adoption path the
        # pre-rename app already created it under this exact name.
        AddIndexIfAbsent(
            model_name="taskrecord",
            index=models.Index(fields=["state", "deadline"], name="taskrec_deadline_idx"),
        ),
    ]
