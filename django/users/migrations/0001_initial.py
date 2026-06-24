from django.db import migrations, models
from django.utils import timezone
import uuid


class Migration(migrations.Migration):
    initial = True
    dependencies = [
        ('auth', '0012_alter_user_first_name_max_length'),
    ]

    operations = [
        migrations.CreateModel(
            name='User',
            fields=[
                ('password', models.CharField(max_length=128, verbose_name='password')),
                ('last_login', models.DateTimeField(blank=True, null=True, verbose_name='last login')),
                ('is_superuser', models.BooleanField(default=False, help_text='Designates that this user has all permissions without explicitly assigning them.', verbose_name='superuser status')),
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('username', models.CharField(blank=True, max_length=150, unique=True, verbose_name='username')),
                ('first_name', models.CharField(blank=True, max_length=150, verbose_name='first name')),
                ('last_name', models.CharField(blank=True, max_length=150, verbose_name='last name')),
                ('email', models.EmailField(blank=True, max_length=254, null=True, unique=True, verbose_name='email address')),
                ('phone', models.CharField(blank=True, db_index=True, max_length=20, null=True, unique=True)),
                ('auth_type', models.CharField(choices=[('email', 'Email'), ('phone', 'Phone'), ('oauth', 'OAuth'), ('anonymous', 'Anonymous')], default='email', max_length=20)),
                ('is_email_verified', models.BooleanField(default=False)),
                ('is_phone_verified', models.BooleanField(default=False)),
                ('is_anonymous', models.BooleanField(default=False)),
                ('anonymous_created_at', models.DateTimeField(blank=True, null=True)),
                ('onboarding_completed', models.BooleanField(default=False)),
                ('profile_completed', models.BooleanField(default=False)),
                ('oauth_provider', models.CharField(blank=True, max_length=50, null=True)),
                ('oauth_id', models.CharField(blank=True, max_length=255, null=True)),
                ('avatar', models.URLField(blank=True, null=True)),
                ('bio', models.TextField(blank=True, max_length=500)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('last_login_ip', models.GenericIPAddressField(blank=True, null=True)),
                ('is_staff', models.BooleanField(default=False, help_text='Designates whether the user can log into this admin site.', verbose_name='staff status')),
                ('is_active', models.BooleanField(default=True, help_text='Designates whether this user should be treated as active. Unselect this instead of deleting accounts.', verbose_name='active')),
                ('date_joined', models.DateTimeField(default=timezone.now, verbose_name='date joined')),
                ('groups', models.ManyToManyField(blank=True, related_name='users_user_set', related_query_name='users_user', to='auth.group', verbose_name='groups')),
                ('user_permissions', models.ManyToManyField(blank=True, related_name='users_user_permissions_set', related_query_name='users_user_permissions', to='auth.permission', verbose_name='user permissions')),
            ],
            options={
                'app_label': 'users',
                'db_table': 'users',
            },
        ),
        migrations.AddIndex(
            model_name='user',
            index=models.Index(fields=['email'], name='users_email_idx'),
        ),
        migrations.AddIndex(
            model_name='user',
            index=models.Index(fields=['phone'], name='users_phone_idx'),
        ),
        migrations.AddIndex(
            model_name='user',
            index=models.Index(fields=['oauth_provider', 'oauth_id'], name='users_oauth_idx'),
        ),
    ]
