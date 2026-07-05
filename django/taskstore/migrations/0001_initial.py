import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
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
        migrations.AddIndex(
            model_name="taskrecord",
            index=models.Index(fields=["state", "deadline"], name="taskrec_deadline_idx"),
        ),
    ]
