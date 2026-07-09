from django.apps import AppConfig


class ProjectionsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "stapel_core.django.projections"
    label = "stapel_projections"
    verbose_name = "Stapel Projections"

    def ready(self):
        # Subscribe every declared Projection to its Action topic(s) through
        # the ordinary action registry (same in-process on_commit delivery in a
        # monolith, same bus consumer across services), then validate the
        # registry loudly — a misdeclared read-model fails at startup, not on
        # the first stale read.
        from stapel_core.comm.projections import validate_registry, wire_projections

        wire_projections()
        validate_registry()
