# stapel: expand
# verified: pure column widening of avatar (URLField) from the Django default
# varchar(200) to varchar(500). Forward- and backward-compatible (a grow never
# truncates existing data and old code writing <=200 still fits), so no N-1
# window is needed. Fixes StringDataRightTruncation on OAuth signup when a
# provider (Google/GitHub) returns an avatar URL longer than 200 chars.
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0006_user_staff_roles"),
    ]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="avatar",
            field=models.URLField(blank=True, max_length=500, null=True),
        ),
    ]
