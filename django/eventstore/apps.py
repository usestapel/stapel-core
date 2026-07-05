from django.apps import AppConfig


class EventstoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "stapel_core.django.eventstore"
    label = "stapel_eventstore"
    verbose_name = "Stapel Event Store"
