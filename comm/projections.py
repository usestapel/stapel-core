"""Projection primitive — event-carried read-models over Action (docs:
module-communication.md §10).

A cross-domain read is often solved by keeping a local cache table that a
consumer fills from another domain's events: the catalog carries a
``likes_count`` fed by ``engagement.*`` Actions so a listing page renders
without a synchronous call into engagement. The pattern is *right* but was
re-invented per table — idempotency hand-rolled as a unique constraint,
backfill as a one-off script, counters drifting when a bulk ``update()``
skipped the ``post_save`` signal. Stapel formalises it:

    from stapel_core.comm import Projection

    class ListingLikes(Projection):
        name = "catalog.listing_likes"
        consumes = "engagement.likes_changed"       # Action topic(s)
        model = "catalog.ListingLikes"              # a ProjectionModel table
        source_key = "listing_id"                   # payload field = row identity
        source_of_truth = "engagement.likes_export"  # Function for rebuild
        sequence_field = "revision"                  # ordering token (else event ts)

        def apply(self, event):
            return {"likes_count": event.payload["likes_count"]}

The framework gives, once:

- **Idempotency + ordering.** Every projected row (``ProjectionModel``)
  carries a unique source key, the last applied event id and a monotonic
  sequence. An event is applied only if its position is *newer* than what
  the row already holds — a redelivered duplicate is a no-op and an
  out-of-order (stale) event never overwrites fresher state.
- **A consumer runner** wired through the ordinary Action registry: the same
  in-process on_commit delivery in a monolith, the same bus consumer across
  services — the projection code does not change when the modules split.
- **First-class rebuild** — :func:`rebuild` / ``manage.py rebuild_projection``
  re-derives the whole table from the owner's ``source_of_truth`` Function,
  batched, with progress; not a hand-written backfill script.
- **Loud config validation** (:func:`validate_registry`, run at app ready):
  one table = one source (no two projections target the same model), the
  model must derive from ``ProjectionModel``, required attributes present.

Rules the primitive encodes (violations are review/lint matters, §10):
projections are read-only for business code; one projection owns one source
domain and its table (projected fields are never mixed with locally computed
aggregates); the *owner* of the data computes each aggregate and publishes it
as a fact via ``emit()`` in its transaction — one-directional fact streams,
never recompute loops driven by ``post_save`` (which bulk updates skip).

**Two modes, one declaration** (projections-and-composition §1). The mode is
a property of the TOPOLOGY at process start, never of business code:

- **remote** (owner and consumer are separate services) — everything above:
  a materialised ``ProjectionModel`` table fed from the bus, ``rebuild`` /
  ``drift_check`` for backfill.
- **local** (owner and consumer share one ``INSTALLED_APPS`` / process) —
  no table, no bus subscription; reads go first-hand through the owner's
  ``live_query`` Function (a keyed batch lookup), synchronously in-process.

:func:`resolve_mode` auto-detects the mode from the owner's app label (the
first ``consumes`` topic prefix — ``"engagement.likes_changed"`` → app label
``engagement``; convention: a module's app_label == its bus/namespace
prefix). Business code never branches on the mode — it calls the single
accessor :func:`read`:

    from stapel_core.comm.projections import read

    likes = read("catalog.listing_likes", keys=[listing.id, ...])
    # remote: ListingLikes.objects.filter(projection_key__in=keys)
    # local:  call("engagement.likes_by_keys", {"keys": keys})
    # either way: {key: {..fields..}} with stringified keys
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Iterable

from .exceptions import ProjectionConfigError, ProjectionError

logger = logging.getLogger(__name__)


class Projection:
    """Declarative read-model: which Action topic(s) feed which local table,
    and how each event upserts a row. Subclass, set the class attributes, and
    (optionally) override :meth:`apply` / :meth:`from_snapshot`.

    Declaring a subclass with a non-empty ``name`` registers it. Instantiation
    is managed by the registry — treat a subclass as a declaration, not an
    object you construct yourself.

    Attributes:
        name: Unique projection name (also the rebuild command argument).
        consumes: Action topic or topics whose events feed the table.
        model: The ``ProjectionModel`` subclass, or its ``"app_label.Model"``
            dotted string (resolved lazily so this module stays import-light).
        source_key: Payload field carrying the source row's identity; stored
            as the row's unique ``projection_key``.
        source_of_truth: comm Function name the owner exposes to export the
            full state for :func:`rebuild`. Empty = the projection cannot be
            rebuilt (validation rejects a rebuild attempt).
        sequence_field: Payload field carrying a monotonic ordering token for
            the source row. Empty falls back to the event timestamp.
        live_query: comm Function name the owner exposes for keyed batch
            lookup — the *local-mode* read path. Contract: called with
            ``{"keys": [<str>, ...]}`` and returns ``{key: {..fields..}}``.
            Required in local mode; unused (optional) in remote mode.
        force_mode: Optional override of the auto-detected mode
            (``"local"``/``"remote"``) for edge cases, e.g. a test that wants
            to exercise the remote path inside a monolithic dev environment.
            Empty = auto-detect via :func:`resolve_mode`.
    """

    name: str = ""
    consumes: str | Iterable[str] = ()
    model: Any = None
    source_key: str = ""
    source_of_truth: str = ""
    sequence_field: str = ""
    live_query: str = ""
    force_mode: str = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if getattr(cls, "name", ""):
            projection_registry.register(cls)

    # -- overridable mapping hooks -----------------------------------------

    def apply(self, event) -> dict:
        """Map an event to the read-model fields to upsert. Default: the
        payload minus the source key. Override to select/rename/derive fields
        (and, with multiple ``consumes`` topics, branch on
        ``event.event_type``)."""
        return {k: v for k, v in event.payload.items() if k != self.source_key}

    def from_snapshot(self, row: dict) -> dict:
        """Map one owner-snapshot row (:func:`rebuild`) to read-model fields.
        Default: the row minus the source key and the reserved ``seq``."""
        return {k: v for k, v in row.items() if k not in (self.source_key, "seq")}

    # -- resolution / ordering ---------------------------------------------

    def topics(self) -> list[str]:
        c = self.consumes
        return [c] if isinstance(c, str) else list(c)

    def owner_label(self) -> str:
        """The owning domain's app label, derived from the first ``consumes``
        topic prefix (``"engagement.likes_changed"`` → ``"engagement"``).
        Convention (projections-and-composition §1 / topology §37): a
        module's Django app_label == its namespace prefix on the bus."""
        topics = self.topics()
        if not topics:
            raise ProjectionConfigError(
                f"projection {self.name!r} declares no 'consumes' topic(s)"
            )
        return topics[0].split(".", 1)[0]

    def resolved_model(self):
        m = self.model
        if isinstance(m, str):
            from django.apps import apps

            return apps.get_model(m)
        return m

    def position(self, event) -> int:
        """The monotonic ordering token for this event. From
        ``sequence_field`` when set, else the event's publish timestamp."""
        if self.sequence_field:
            try:
                return int(event.payload[self.sequence_field])
            except (KeyError, TypeError, ValueError) as exc:
                raise ProjectionError(
                    f"projection {self.name!r}: event missing/invalid "
                    f"sequence_field {self.sequence_field!r}: {exc!r}"
                ) from exc
        return int(getattr(event, "timestamp", 0) or 0)


# ---------------------------------------------------------------------------
# Mode resolution — topology decides, not business code
# ---------------------------------------------------------------------------


def resolve_mode(proj: Projection) -> str:
    """``"local"`` when the owning app is installed in this process,
    ``"remote"`` otherwise; ``force_mode`` overrides the auto-detect.

    The owner is looked up by *app label* via ``apps.get_app_config(label)``
    — NOT ``apps.is_installed()``, which compares the dotted module path
    (``"stapel_engagement"``), not the label a topic prefix carries. This
    relies on the convention "app_label == bus namespace prefix"
    (projections-and-composition §1, CTO-review note).
    """
    if proj.force_mode:
        if proj.force_mode not in ("local", "remote"):
            raise ProjectionConfigError(
                f"projection {proj.name!r}: force_mode must be 'local' or "
                f"'remote', got {proj.force_mode!r}"
            )
        return proj.force_mode
    from django.apps import apps

    try:
        apps.get_app_config(proj.owner_label())
        return "local"
    except LookupError:
        return "remote"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ProjectionRegistry:
    """name → one Projection instance. The loose-coupling seam is the topic
    name, exactly as for Actions/Functions."""

    def __init__(self) -> None:
        self._by_name: dict[str, Projection] = {}
        self._lock = threading.Lock()

    def register(self, cls: type[Projection]) -> None:
        inst = cls()
        with self._lock:
            existing = self._by_name.get(inst.name)
            if existing is not None and type(existing) is not cls:
                raise ProjectionConfigError(
                    f"projection name {inst.name!r} already registered by "
                    f"{type(existing).__name__}; names are unique"
                )
            self._by_name[inst.name] = inst

    def get(self, name: str) -> Projection:
        try:
            return self._by_name[name]
        except KeyError:
            raise ProjectionConfigError(
                f"no projection named {name!r} "
                "(is the declaring app in INSTALLED_APPS?)"
            ) from None

    def all(self) -> list[Projection]:
        return list(self._by_name.values())

    def names(self) -> list[str]:
        return sorted(self._by_name)

    def clear(self) -> None:
        """Tests only."""
        with self._lock:
            self._by_name.clear()


projection_registry = ProjectionRegistry()


# ---------------------------------------------------------------------------
# Config validation — loud, at app ready
# ---------------------------------------------------------------------------


def validate_registry() -> None:
    """Validate every declared projection; raise :class:`ProjectionConfigError`
    on the first problem. Called from the projections AppConfig.ready(), so a
    misdeclaration fails at startup, not on the first stale read.

    The checks branch on the resolved mode (projections-and-composition §1):
    a *local* projection is valid without ``model`` but must declare
    ``live_query`` (its only read path); a *remote* projection must declare
    ``model`` (``live_query`` is optional there)."""
    from stapel_core.django.projections.models import ProjectionModel

    seen_tables: dict[str, str] = {}
    for proj in projection_registry.all():
        if not proj.topics():
            raise ProjectionConfigError(
                f"projection {proj.name!r} declares no 'consumes' topic(s)"
            )
        if not proj.source_key:
            raise ProjectionConfigError(
                f"projection {proj.name!r} declares no 'source_key'"
            )
        mode = resolve_mode(proj)  # also rejects a bogus force_mode
        if mode == "local":
            # No table in local mode — reads go first-hand through the
            # owner's keyed live_query Function; table checks don't apply.
            if not proj.live_query:
                raise ProjectionConfigError(
                    f"projection {proj.name!r} resolves to local mode (owner "
                    f"app {proj.owner_label()!r} is installed) but declares "
                    "no 'live_query' Function — local mode has no table, the "
                    "keyed live_query is its only read path"
                )
            continue
        if proj.model is None:
            raise ProjectionConfigError(
                f"projection {proj.name!r} resolves to remote mode (owner "
                f"app {proj.owner_label()!r} not installed) but declares no "
                "'model' — remote mode materialises a ProjectionModel table"
            )
        model = proj.resolved_model()
        if not (isinstance(model, type) and issubclass(model, ProjectionModel)):
            raise ProjectionConfigError(
                f"projection {proj.name!r} model {model!r} must derive from "
                "stapel_core.django.projections.models.ProjectionModel "
                "(it carries the source-key/sequence/event-id bookkeeping)"
            )
        # One table = one source: two projections filling the same read-model
        # would interleave two domains' facts under one sequence line — the
        # SellerProfile anti-pattern §10 warns about. Fail loudly.
        table = model._meta.db_table
        owner = seen_tables.get(table)
        if owner is not None:
            raise ProjectionConfigError(
                f"projections {owner!r} and {proj.name!r} both target table "
                f"{table!r}; one table = one source (module-communication §10)"
            )
        seen_tables[table] = proj.name


# ---------------------------------------------------------------------------
# Consumer runner
# ---------------------------------------------------------------------------


def apply_event(proj: Projection, event) -> str:
    """Idempotently upsert one event into the projection's table. Returns
    ``"created" | "updated" | "skipped"`` (skipped = duplicate or out-of-order).

    The whole read is ``select_for_update``-locked so concurrent consumers of
    the same source key serialise; the sequence guard makes redelivery and
    reordering harmless — an event applies iff its position is strictly newer
    than the row's."""
    from django.db import transaction

    model = proj.resolved_model()
    raw_key = event.payload.get(proj.source_key)
    if raw_key is None:
        raise ProjectionError(
            f"projection {proj.name!r}: event {event.event_type!r} carries no "
            f"source_key {proj.source_key!r} in payload"
        )
    key = str(raw_key)
    seq = proj.position(event)
    fields = proj.apply(event)

    with transaction.atomic():
        row = (
            model.objects.select_for_update()
            .filter(projection_key=key)
            .first()
        )
        if row is None:
            model.objects.create(
                projection_key=key,
                projection_seq=seq,
                projection_event_id=event.event_id,
                **fields,
            )
            return "created"
        # Exact redelivery (same event id) or a stale/reordered event whose
        # position is not newer than what we already applied: no-op.
        if event.event_id and event.event_id == row.projection_event_id:
            return "skipped"
        if seq <= row.projection_seq:
            return "skipped"
        for f, v in fields.items():
            setattr(row, f, v)
        row.projection_seq = seq
        row.projection_event_id = event.event_id
        row.save(
            update_fields=[*fields, "projection_seq", "projection_event_id",
                           "projection_updated_at"],
        )
        return "updated"


def _make_handler(proj: Projection):
    def handler(event) -> None:
        apply_event(proj, event)

    handler.__name__ = f"project_{proj.name.replace('.', '_')}"
    return handler


def wire_projections() -> int:
    """Subscribe every declared *remote-mode* projection to its Action
    topic(s) through the ordinary action registry. Returns the number of
    subscriptions. Idempotent per process (the action registry dedupes
    identical handlers, and one handler object is created per projection
    here).

    Local-mode projections are skipped: the owner applies its own events
    in-process already, there is no table to feed — reads go through
    ``live_query`` (see :func:`read`)."""
    from .actions import subscribe_action

    _handlers = _wired_handlers
    count = 0
    for proj in projection_registry.all():
        if resolve_mode(proj) == "local":
            logger.debug(
                "projection %s: local mode (owner %r installed) — no bus "
                "subscription", proj.name, proj.owner_label(),
            )
            continue
        handler = _handlers.get(proj.name)
        if handler is None:
            handler = _make_handler(proj)
            _handlers[proj.name] = handler
        for topic in proj.topics():
            subscribe_action(topic, handler)
            count += 1
    return count


_wired_handlers: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# read() — the single mode-blind accessor for business code
# ---------------------------------------------------------------------------

#: ProjectionModel bookkeeping columns — never part of a read() result.
_BOOKKEEPING_FIELDS = frozenset(
    {"projection_key", "projection_seq", "projection_event_id",
     "projection_updated_at"}
)


def read(name: str, keys: Iterable[Any]) -> dict[str, dict]:
    """Batch-read projection *name* for *keys* — the ONE accessor business
    code uses instead of querying the ``ProjectionModel`` directly (a direct
    ORM query silently hard-wires the remote mode into the caller).

    Returns ``{key: {..fields..}}`` — identical shape in both modes; keys are
    stringified (the same normalisation ``apply_event`` uses for
    ``projection_key``) and absent keys are simply missing from the result.

    - remote: one ``projection_key__in`` query against the projection table;
      fields = the model's own columns (bookkeeping stripped).
    - local: one synchronous ``call(live_query, {"keys": [...]})`` to the
      owner's keyed batch Function (in-process — no serialisation, no bus).
    """
    proj = projection_registry.get(name)
    key_list = [str(k) for k in keys]
    if not key_list:
        return {}

    if resolve_mode(proj) == "local":
        if not proj.live_query:
            raise ProjectionConfigError(
                f"projection {name!r} is in local mode but declares no "
                "'live_query' Function"
            )
        from .functions import call

        resp = call(proj.live_query, {"keys": key_list})
        if not isinstance(resp, dict):
            raise ProjectionError(
                f"projection {name!r}: live_query {proj.live_query!r} must "
                f"return a dict {{key: fields}}, got {type(resp).__name__}"
            )
        return {str(k): v for k, v in resp.items()}

    model = proj.resolved_model()
    out: dict[str, dict] = {}
    for row in model.objects.filter(projection_key__in=key_list):
        out[row.projection_key] = {
            f.attname: getattr(row, f.attname)
            for f in model._meta.concrete_fields
            if not f.primary_key and f.attname not in _BOOKKEEPING_FIELDS
        }
    return out


# ---------------------------------------------------------------------------
# Rebuild — first-class backfill from the owner's snapshot Function
# ---------------------------------------------------------------------------


@dataclass
class RebuildResult:
    """Outcome of a :func:`rebuild`.

    Attributes:
        name: Projection name.
        rows: Rows written.
        batches: ``source_of_truth`` calls made.
    """

    name: str
    rows: int
    batches: int


@dataclass
class DriftReport:
    """Count comparison between the local table and the owner's snapshot.

    Attributes:
        name: Projection name.
        local: Rows in the projection table.
        source: Rows the owner reports.
        in_sync: ``local == source``.
    """

    name: str
    local: int
    source: int

    @property
    def in_sync(self) -> bool:
        return self.local == self.source


def _iter_snapshot(proj: Projection, batch_size: int):
    """Page the owner's ``source_of_truth`` Function. Contract: called with
    ``{"cursor": <opaque|None>, "limit": n}`` and returns
    ``{"rows": [ {source_key: ..., "seq": <int>, **fields}, ... ],
       "cursor": <next|None>, "total": <int|None>}``. Yields (rows, total)
    per page until the cursor is exhausted."""
    from .functions import call

    cursor = None
    while True:
        resp = call(proj.source_of_truth, {"cursor": cursor, "limit": batch_size})
        rows = resp.get("rows", []) if isinstance(resp, dict) else list(resp)
        total = resp.get("total") if isinstance(resp, dict) else None
        yield rows, total
        cursor = resp.get("cursor") if isinstance(resp, dict) else None
        if not cursor:
            return


def rebuild(name: str, *, batch_size: int = 500, on_progress=None) -> RebuildResult:
    """Re-derive the whole projection table from the owner's ``source_of_truth``
    Function. All-or-nothing: the clear+repopulate runs in one transaction, so
    a failed export leaves the existing projection intact.

    ``on_progress(done, total)`` — if given — is called after each batch
    (``total`` may be ``None`` when the owner does not report it).

    The snapshot's per-row ``seq`` seeds each row's sequence, so live events
    that arrive after a rebuild supersede it (newer position) while events
    older than the snapshot are correctly rejected."""
    proj = projection_registry.get(name)
    if not proj.source_of_truth:
        raise ProjectionConfigError(
            f"projection {name!r} has no 'source_of_truth' Function — cannot "
            "rebuild (declare the owner's export Function to enable rebuild)"
        )
    from django.db import transaction

    model = proj.resolved_model()
    rows_written = 0
    batches = 0
    with transaction.atomic():
        model.objects.all().delete()
        for rows, total in _iter_snapshot(proj, batch_size):
            objs = []
            for row in rows:
                key = str(row[proj.source_key])
                objs.append(
                    model(
                        projection_key=key,
                        projection_seq=int(row.get("seq", 0)),
                        projection_event_id="",
                        **proj.from_snapshot(row),
                    )
                )
            model.objects.bulk_create(objs)
            rows_written += len(objs)
            batches += 1
            if on_progress is not None:
                on_progress(rows_written, total)
    logger.info("rebuilt projection %s: %d row(s) in %d batch(es)",
                name, rows_written, batches)
    return RebuildResult(name=name, rows=rows_written, batches=batches)


def drift_check(name: str, *, batch_size: int = 500) -> DriftReport:
    """Compare the local row count against the owner's snapshot count without
    writing anything — the cheap health check §10 calls optional. A mismatch
    is the signal to :func:`rebuild`."""
    proj = projection_registry.get(name)
    if not proj.source_of_truth:
        raise ProjectionConfigError(
            f"projection {name!r} has no 'source_of_truth' Function — cannot "
            "drift-check"
        )
    model = proj.resolved_model()
    local = model.objects.count()
    source = 0
    for rows, _total in _iter_snapshot(proj, batch_size):
        source += len(rows)
    return DriftReport(name=name, local=local, source=source)


# ---------------------------------------------------------------------------
# Status / lag
# ---------------------------------------------------------------------------


@dataclass
class ProjectionStatus:
    """Observability snapshot of a projection table.

    Attributes:
        name: Projection name.
        rows: Rows currently projected.
        last_seq: Highest applied sequence (0 when empty).
        last_event_id: Event id of the most recently updated row.
        last_updated: When the most recent row was last written (or None).
        lag_seconds: Seconds since ``last_updated`` (None when empty) — a
            coarse freshness/lag signal for a monolith without a broker to
            report consumer offsets.
    """

    name: str
    rows: int
    last_seq: int
    last_event_id: str
    last_updated: Any
    lag_seconds: float | None


def projection_status(name: str) -> ProjectionStatus:
    proj = projection_registry.get(name)
    model = proj.resolved_model()
    latest = model.objects.order_by("-projection_updated_at").first()
    rows = model.objects.count()
    if latest is None:
        return ProjectionStatus(name, 0, 0, "", None, None)
    from django.db.models import Max
    from django.utils import timezone

    last_seq = model.objects.aggregate(m=Max("projection_seq"))["m"] or 0
    lag = (timezone.now() - latest.projection_updated_at).total_seconds()
    return ProjectionStatus(
        name=name,
        rows=rows,
        last_seq=int(last_seq),
        last_event_id=latest.projection_event_id,
        last_updated=latest.projection_updated_at,
        lag_seconds=lag,
    )


__all__ = [
    "Projection",
    "ProjectionRegistry",
    "projection_registry",
    "resolve_mode",
    "read",
    "validate_registry",
    "wire_projections",
    "apply_event",
    "rebuild",
    "drift_check",
    "projection_status",
    "RebuildResult",
    "DriftReport",
    "ProjectionStatus",
]
