from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="OutboxEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("topic", models.CharField(db_index=True, max_length=255)),
                ("event_json", models.TextField(help_text="Serialized stapel_core.bus.Event")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("dispatched_at", models.DateTimeField(blank=True, null=True)),
                ("attempts", models.PositiveIntegerField(default=0)),
                ("next_attempt_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("last_error", models.TextField(blank=True, default="")),
            ],
            options={
                "ordering": ["created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="outboxevent",
            index=models.Index(fields=["dispatched_at", "next_attempt_at"], name="outbox_pending_idx"),
        ),
    ]
