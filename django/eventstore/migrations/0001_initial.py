from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="EventRecord",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("stream", models.CharField(db_index=True, max_length=255)),
                ("ts", models.DateTimeField(db_index=True)),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("project", models.CharField(blank=True, db_index=True, max_length=255, null=True)),
                ("task", models.CharField(blank=True, db_index=True, max_length=255, null=True)),
                ("container", models.CharField(blank=True, db_index=True, max_length=255, null=True)),
            ],
        ),
        migrations.CreateModel(
            name="EventRollup",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("name", models.CharField(db_index=True, max_length=255)),
                ("stream", models.CharField(db_index=True, max_length=255)),
                ("group_key", models.CharField(max_length=1024)),
                ("group", models.JSONField(blank=True, default=dict)),
                ("count", models.BigIntegerField(default=0)),
                ("sums", models.JSONField(blank=True, default=dict)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.AddIndex(
            model_name="eventrecord",
            index=models.Index(fields=["stream", "ts", "id"], name="evt_stream_ts_idx"),
        ),
        migrations.AddIndex(
            model_name="eventrollup",
            index=models.Index(fields=["name", "stream"], name="evt_rollup_name_idx"),
        ),
        migrations.AddConstraint(
            model_name="eventrollup",
            constraint=models.UniqueConstraint(
                fields=["name", "stream", "group_key"], name="evt_rollup_bucket_uniq"
            ),
        ),
    ]
