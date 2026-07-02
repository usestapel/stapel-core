from django.apps import AppConfig


class TaskstoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "stapel_core.django.taskstore"
    label = "stapel_tasks"
    verbose_name = "Stapel Tasks"

    def ready(self):
        # Framework subscriber: executes locally-registered task kinds when
        # their requested-event arrives (in-process, relay or bus consumer).
        from stapel_core.comm.actions import subscribe_action
        from stapel_core.comm.tasks import TASK_REQUESTED, handle_task_requested

        subscribe_action(TASK_REQUESTED, handle_task_requested)

        # Register committed JSON schemas (schemas/emits, schemas/functions)
        # so VALIDATE_SCHEMAS can enforce contract/code consistency.
        from stapel_core.comm.schemas import autoload_schemas

        autoload_schemas()
