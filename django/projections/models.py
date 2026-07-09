"""Base table for comm Projections (event-carried read-models, §10).

A projection table is owned by the framework's consumer runner, not by
business code: every row carries its source-domain identity plus the
bookkeeping the runner needs to stay idempotent and ordered — the last
applied event id and a monotonic sequence. A concrete read-model subclasses
:class:`ProjectionModel` and adds only its projected columns:

    class ListingLikes(ProjectionModel):
        likes_count = models.PositiveIntegerField(default=0)

        class Meta:
            app_label = "catalog"

The abstract base ships no table of its own (the ``projections`` app has no
migrations); the columns land in the concrete model's own table, giving the
"one table = one source" separation §10 asks for — projected fields never
mixed with locally computed aggregates.
"""
from django.db import models


class ProjectionModel(models.Model):
    """Abstract read-model row: a projected source entity plus the runner's
    idempotency/ordering bookkeeping. Read-only for business code."""

    #: Source row identity (``Projection.source_key`` value), stringified.
    #: Unique — the natural upsert key and the "unique(source-key)" §10 names.
    projection_key = models.CharField(max_length=255, unique=True, db_index=True)
    #: Monotonic ordering token of the last applied event; a new event applies
    #: only if strictly greater, so redelivery and reordering are no-ops.
    projection_seq = models.BigIntegerField(default=0)
    #: Event id of the last applied event — audit trail and exact-duplicate
    #: short-circuit.
    projection_event_id = models.CharField(max_length=64, blank=True, default="")
    #: When this row was last written by the runner (freshness/lag signal).
    projection_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    def __str__(self):
        return f"{self.projection_key} @seq{self.projection_seq}"
