"""Per-step checkpoints for the Task journal.

Additive only (expand): one defaulted JSON column. Nothing is dropped and no
existing row is rewritten, so an old process and a new one run against the
same table during a rollout — the old one never writes the column and its
retries keep re-running the handler from the top, exactly as before.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('stapel_taskstore', '0002_task_retry_and_dedupe'),
    ]

    operations = [
        migrations.AddField(
            model_name='taskrecord',
            name='checkpoints',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
