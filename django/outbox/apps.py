from django.apps import AppConfig


class OutboxConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "stapel_core.django.outbox"
    label = "stapel_outbox"
    verbose_name = "Stapel Outbox"
