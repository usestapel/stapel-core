from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0005_alter_user_groups_alter_user_user_permissions"),
    ]
    operations = [
        migrations.AddField(
            model_name="user",
            name="staff_roles",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
