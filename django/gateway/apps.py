from django.apps import AppConfig


class GatewayConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "stapel_core.django.gateway"
    label = "stapel_gateway"
    verbose_name = "Stapel Privilege Gateway"

    def ready(self):
        # comm surface for in-cluster (control-plane) callers.
        from stapel_core.gateway import functions

        functions.register()
