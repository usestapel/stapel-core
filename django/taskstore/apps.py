from django.apps import AppConfig


class TaskstoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "stapel_core.django.taskstore"
    # Django label for this internal comm-Task *persistence* app (async named
    # background operations — module-communication §2.1). Renamed from
    # ``stapel_tasks`` to ``stapel_taskstore`` in core 0.8.0: the generic
    # user-facing task/kanban module ``stapel-tasks`` now owns the canonical
    # ``stapel_tasks`` label (see docs/tasks-module.md §2/§11). The physical
    # table name is pinned to its historical value via ``Meta.db_table`` so the
    # rename is a label-only change — no data movement (see CHANGELOG 0.8.0).
    label = "stapel_taskstore"
    verbose_name = "Stapel Taskstore"

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
