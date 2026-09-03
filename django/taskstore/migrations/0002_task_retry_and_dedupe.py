"""Backoff, idempotency and a named failure reason for the Task journal.

Additive only (expand): three defaulted/nullable columns, one index, one
PARTIAL unique constraint. Nothing is dropped and no existing row is
rewritten, so an old process and a new one run against the same table
during a rollout — the old one simply never writes the new columns, and
its retries stay instant until it is replaced.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('stapel_taskstore', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='taskrecord',
            name='dedupe_key',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='taskrecord',
            name='failure_reason',
            field=models.CharField(blank=True, choices=[('handler', 'Handler error'), ('unprocessable', 'Unprocessable payload'), ('no_handler', 'No local handler'), ('deadline_exceeded', 'Deadline exceeded')], db_index=True, default='', max_length=32),
        ),
        migrations.AddField(
            model_name='taskrecord',
            name='not_before',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name='taskrecord',
            index=models.Index(fields=['state', 'not_before'], name='taskrec_notbefore_idx'),
        ),
        migrations.AddConstraint(
            model_name='taskrecord',
            constraint=models.UniqueConstraint(condition=models.Q(('state__in', ['pending', 'running']), models.Q(('dedupe_key', ''), _negated=True)), fields=('dedupe_key',), name='taskrec_live_dedupe_key'),
        ),
    ]
