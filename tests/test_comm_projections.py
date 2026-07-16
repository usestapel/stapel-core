"""Tests for the comm Projection primitive (event-carried read-models, §10).

Covers: idempotent upsert under duplicate events, out-of-order rejection,
end-to-end emit→subscribe→project wiring, first-class rebuild from the owner's
snapshot Function, drift-check, status/lag, and the loud config validation
(one table = one source; model must derive from ProjectionModel; required
attributes present).
"""
import pytest
from django.db import connection, models

from stapel_core.bus.event import Event
from stapel_core.comm import (
    Projection,
    drift_check,
    projection_registry,
    projection_status,
    rebuild,
)
from stapel_core.comm.exceptions import ProjectionConfigError, ProjectionError
from stapel_core.comm.projections import apply_event, validate_registry, wire_projections
from stapel_core.comm.registry import action_registry
from stapel_core.django.projections.models import ProjectionModel


# ---------------------------------------------------------------------------
# Concrete read-models (registered under the installed "users" app so the
# abstract ProjectionModel base can be exercised against a real table).
# ---------------------------------------------------------------------------


class LikesReadModel(ProjectionModel):
    likes_count = models.PositiveIntegerField(default=0)

    class Meta:
        app_label = "users"


class PlainCache(models.Model):
    """NOT a ProjectionModel — used to prove validation rejects it."""

    key = models.CharField(max_length=50)

    class Meta:
        app_label = "users"


@pytest.fixture
def likes_table(transactional_db):
    with connection.schema_editor() as editor:
        editor.create_model(LikesReadModel)
    yield
    with connection.schema_editor() as editor:
        editor.delete_model(LikesReadModel)


@pytest.fixture(autouse=True)
def clean_registries():
    """Projection/action registries are process-global; isolate each test."""
    projection_registry.clear()
    action_registry.clear()
    yield
    projection_registry.clear()
    action_registry.clear()


def _event(payload, *, topic="engagement.likes_changed", event_id="e1", ts=1000):
    return Event(event_type=topic, service="engagement", payload=payload,
                 event_id=event_id, timestamp=ts)


def _likes_projection(**overrides):
    attrs = dict(
        name="catalog.listing_likes",
        consumes="engagement.likes_changed",
        model=LikesReadModel,
        source_key="listing_id",
        source_of_truth="engagement.likes_export",
        sequence_field="revision",
    )
    attrs.update(overrides)

    def apply(self, event):
        return {"likes_count": event.payload["likes_count"]}

    attrs["apply"] = apply
    return type("_LikesProj", (Projection,), attrs)()


# ---------------------------------------------------------------------------
# Idempotency + ordering
# ---------------------------------------------------------------------------


def test_apply_creates_then_updates(likes_table):
    proj = _likes_projection()
    assert apply_event(proj, _event({"listing_id": 7, "likes_count": 3, "revision": 1})) == "created"
    row = LikesReadModel.objects.get(projection_key="7")
    assert row.likes_count == 3 and row.projection_seq == 1

    assert apply_event(proj, _event(
        {"listing_id": 7, "likes_count": 5, "revision": 2}, event_id="e2")) == "updated"
    row.refresh_from_db()
    assert row.likes_count == 5 and row.projection_seq == 2


def test_duplicate_event_is_idempotent(likes_table):
    proj = _likes_projection()
    ev = _event({"listing_id": 7, "likes_count": 3, "revision": 1}, event_id="dup")
    assert apply_event(proj, ev) == "created"
    # Exact redelivery (same event id) and a same-sequence redelivery: no-op.
    assert apply_event(proj, ev) == "skipped"
    assert apply_event(proj, _event(
        {"listing_id": 7, "likes_count": 99, "revision": 1}, event_id="other")) == "skipped"
    assert LikesReadModel.objects.get(projection_key="7").likes_count == 3
    assert LikesReadModel.objects.count() == 1


def test_out_of_order_event_rejected(likes_table):
    proj = _likes_projection()
    apply_event(proj, _event({"listing_id": 7, "likes_count": 5, "revision": 5}, event_id="e5"))
    # A stale (lower-sequence) event must not overwrite fresher state.
    assert apply_event(proj, _event(
        {"listing_id": 7, "likes_count": 1, "revision": 3}, event_id="e3")) == "skipped"
    assert LikesReadModel.objects.get(projection_key="7").likes_count == 5


def test_missing_source_key_raises(likes_table):
    proj = _likes_projection()
    with pytest.raises(ProjectionError):
        apply_event(proj, _event({"likes_count": 3, "revision": 1}))


def test_timestamp_used_when_no_sequence_field(likes_table):
    proj = _likes_projection(sequence_field="")
    apply_event(proj, _event({"listing_id": 7, "likes_count": 5}, event_id="a", ts=2000))
    # Older timestamp → rejected.
    assert apply_event(proj, _event(
        {"listing_id": 7, "likes_count": 1}, event_id="b", ts=1000)) == "skipped"
    assert LikesReadModel.objects.get(projection_key="7").likes_count == 5


# ---------------------------------------------------------------------------
# End-to-end wiring through the Action registry / outbox
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_emit_projects_through_action_subscription(likes_table):
    from django.db import transaction

    from stapel_core.comm import emit

    _likes_projection()
    wire_projections()  # subscribe the projection to its topic

    with transaction.atomic():
        emit("engagement.likes_changed",
             {"listing_id": 7, "likes_count": 42, "revision": 1})
    # After commit the outbox first-chance dispatch delivered the event.
    row = LikesReadModel.objects.get(projection_key="7")
    assert row.likes_count == 42


# ---------------------------------------------------------------------------
# Rebuild (first-class) + drift-check
# ---------------------------------------------------------------------------


def _register_export(pages):
    """Register the owner's snapshot Function returning the given pages."""
    from stapel_core.comm import register_function

    state = {"i": 0}

    def export(payload):
        i = state["i"]
        state["i"] += 1
        return pages[i] if i < len(pages) else {"rows": [], "cursor": None}

    register_function("engagement.likes_export", export)


def test_rebuild_from_scratch_batched(likes_table):
    from stapel_core.comm.registry import function_registry

    function_registry.clear()
    proj = _likes_projection()
    # Stale local row that the owner no longer knows about — rebuild drops it.
    LikesReadModel.objects.create(projection_key="999", likes_count=1, projection_seq=1)

    _register_export([
        {"rows": [{"listing_id": 1, "likes_count": 10, "seq": 4},
                  {"listing_id": 2, "likes_count": 20, "seq": 4}],
         "cursor": "p2", "total": 3},
        {"rows": [{"listing_id": 3, "likes_count": 30, "seq": 4}],
         "cursor": None, "total": 3},
    ])

    seen = []
    result = rebuild("catalog.listing_likes", batch_size=2,
                     on_progress=lambda d, t: seen.append((d, t)))

    assert result.rows == 3 and result.batches == 2
    assert seen == [(2, 3), (3, 3)]
    assert not LikesReadModel.objects.filter(projection_key="999").exists()
    assert LikesReadModel.objects.get(projection_key="2").likes_count == 20
    # A live event newer than the snapshot seq supersedes the rebuild.
    assert apply_event(proj, _event(
        {"listing_id": 2, "likes_count": 25, "revision": 5}, event_id="live")) == "updated"
    # A live event older than the snapshot seq is rejected.
    assert apply_event(proj, _event(
        {"listing_id": 1, "likes_count": 0, "revision": 2}, event_id="stale")) == "skipped"


def test_rebuild_without_source_of_truth_raises(likes_table):
    _likes_projection(name="catalog.no_source", source_of_truth="")
    with pytest.raises(ProjectionConfigError):
        rebuild("catalog.no_source")


def test_drift_check_counts(likes_table):
    from stapel_core.comm.registry import function_registry

    function_registry.clear()
    _likes_projection()
    LikesReadModel.objects.create(projection_key="1", likes_count=1, projection_seq=1)
    _register_export([{"rows": [{"listing_id": 1, "seq": 1}, {"listing_id": 2, "seq": 1}],
                       "cursor": None}])
    report = drift_check("catalog.listing_likes")
    assert report.local == 1 and report.source == 2
    assert report.in_sync is False


# ---------------------------------------------------------------------------
# Status / lag
# ---------------------------------------------------------------------------


def test_projection_status(likes_table):
    proj = _likes_projection()
    apply_event(proj, _event({"listing_id": 7, "likes_count": 3, "revision": 4}, event_id="z"))
    st = projection_status("catalog.listing_likes")
    assert st.rows == 1 and st.last_seq == 4 and st.last_event_id == "z"
    assert st.lag_seconds is not None and st.lag_seconds >= 0


def test_projection_status_empty(likes_table):
    _likes_projection()
    st = projection_status("catalog.listing_likes")
    assert st.rows == 0 and st.last_seq == 0 and st.lag_seconds is None


# ---------------------------------------------------------------------------
# Loud config validation
# ---------------------------------------------------------------------------


def test_validation_requires_attributes():
    type("_NoKey", (Projection,), {
        "name": "bad.no_key", "consumes": "x.y", "model": LikesReadModel,
        "source_key": "",
    })()
    with pytest.raises(ProjectionConfigError, match="source_key"):
        validate_registry()


def test_validation_rejects_non_projection_model():
    type("_Plain", (Projection,), {
        "name": "bad.plain", "consumes": "x.y", "model": PlainCache,
        "source_key": "key",
    })()
    with pytest.raises(ProjectionConfigError, match="ProjectionModel"):
        validate_registry()


def test_validation_one_table_one_source():
    common = dict(consumes="x.y", model=LikesReadModel, source_key="listing_id")
    type("_A", (Projection,), {"name": "catalog.a", **common})()
    type("_B", (Projection,), {"name": "catalog.b", **common})()
    with pytest.raises(ProjectionConfigError, match="one table = one source"):
        validate_registry()


def test_duplicate_name_rejected_at_registration():
    type("_First", (Projection,), {
        "name": "dup.name", "consumes": "x.y", "model": LikesReadModel,
        "source_key": "k",
    })()
    with pytest.raises(ProjectionConfigError, match="already registered"):
        type("_Second", (Projection,), {
            "name": "dup.name", "consumes": "x.y", "model": LikesReadModel,
            "source_key": "k",
        })()


def test_unknown_projection_name_raises():
    with pytest.raises(ProjectionConfigError, match="no projection named"):
        projection_registry.get("does.not.exist")


# ---------------------------------------------------------------------------
# rebuild_projection management command
# ---------------------------------------------------------------------------


def test_command_rebuild(likes_table, capsys):
    from django.core.management import call_command
    from stapel_core.comm.registry import function_registry

    function_registry.clear()
    _likes_projection()
    _register_export([{"rows": [{"listing_id": 1, "likes_count": 10, "seq": 1},
                                {"listing_id": 2, "likes_count": 20, "seq": 1}],
                       "cursor": None, "total": 2}])
    call_command("rebuild_projection", "catalog.listing_likes", "--batch-size", "10")
    out = capsys.readouterr().out
    assert "rebuilt catalog.listing_likes: 2 row(s)" in out
    assert LikesReadModel.objects.count() == 2


def test_command_check(likes_table, capsys):
    from django.core.management import call_command
    from stapel_core.comm.registry import function_registry

    function_registry.clear()
    _likes_projection()
    LikesReadModel.objects.create(projection_key="1", likes_count=1, projection_seq=1)
    _register_export([{"rows": [{"listing_id": 1, "seq": 1}], "cursor": None}])
    call_command("rebuild_projection", "catalog.listing_likes", "--check")
    out = capsys.readouterr().out
    assert "local=1 source=1 [in sync]" in out


def test_default_apply_and_dotted_model_and_list_consumes(likes_table):
    """Documented defaults: default apply() copies payload minus the key,
    model resolves from an "app.Model" string, consumes accepts a list."""
    proj = type("_Defaults", (Projection,), {
        "name": "catalog.defaults",
        "consumes": ["engagement.likes_changed", "engagement.likes_reset"],
        "model": "users.LikesReadModel",
        "source_key": "listing_id",
    })()
    assert proj.topics() == ["engagement.likes_changed", "engagement.likes_reset"]
    assert proj.resolved_model() is LikesReadModel
    apply_event(proj, _event({"listing_id": 7, "likes_count": 9}, ts=5))
    assert LikesReadModel.objects.get(projection_key="7").likes_count == 9
    assert proj.from_snapshot({"listing_id": 7, "seq": 3, "likes_count": 9}) == {"likes_count": 9}


def test_command_unknown_projection_errors():
    from django.core.management import call_command
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("rebuild_projection", "nope.nope")


# ---------------------------------------------------------------------------
# Local/remote mode: auto-detect, validate branching, wiring, read()
# (projections-and-composition §1)
# ---------------------------------------------------------------------------


def _local_projection(**overrides):
    """A projection whose owner app IS installed: topic prefix "users" matches
    the installed stapel_core.django.users app label — local mode."""
    attrs = dict(
        name="catalog.user_badges",
        consumes="users.badges_changed",
        source_key="user_id",
        live_query="users.badges_by_keys",
    )
    attrs.update(overrides)
    return type("_LocalProj", (Projection,), attrs)()


def test_resolve_mode_autodetect():
    from stapel_core.comm.projections import resolve_mode

    # Owner app label "users" is installed → local.
    assert resolve_mode(_local_projection()) == "local"
    # Owner app label "engagement" is not installed → remote.
    assert resolve_mode(_likes_projection()) == "remote"


def test_resolve_mode_force_override():
    from stapel_core.comm.projections import resolve_mode

    proj = _local_projection(name="catalog.forced", force_mode="remote",
                             model=LikesReadModel)
    assert resolve_mode(proj) == "remote"
    bogus = _local_projection(name="catalog.bogus", force_mode="sideways")
    with pytest.raises(ProjectionConfigError, match="force_mode"):
        resolve_mode(bogus)


def test_validate_local_valid_without_model():
    _local_projection()  # no model at all
    validate_registry()  # must not raise


def test_validate_local_requires_live_query():
    _local_projection(live_query="")
    with pytest.raises(ProjectionConfigError, match="live_query"):
        validate_registry()


def test_validate_remote_requires_model():
    _likes_projection(model=None)
    with pytest.raises(ProjectionConfigError, match="model"):
        validate_registry()


def test_wire_skips_local_projections():
    _local_projection()
    _likes_projection()
    count = wire_projections()
    # Only the remote projection subscribed (one topic).
    assert count == 1
    assert action_registry.handlers("users.badges_changed") == []
    assert len(action_registry.handlers("engagement.likes_changed")) == 1


def test_read_remote_via_table(likes_table):
    from stapel_core.comm.projections import read

    proj = _likes_projection()
    apply_event(proj, _event({"listing_id": 7, "likes_count": 3, "revision": 1}))
    apply_event(proj, _event({"listing_id": 8, "likes_count": 5, "revision": 1},
                             event_id="e2"))
    result = read("catalog.listing_likes", keys=[7, 8, 999])
    # Same shape as local mode: {key: fields}, bookkeeping stripped,
    # absent keys absent.
    assert result == {"7": {"likes_count": 3}, "8": {"likes_count": 5}}


def test_read_local_via_live_query(db):
    from stapel_core.comm import register_function
    from stapel_core.comm.projections import read
    from stapel_core.comm.registry import function_registry

    function_registry.clear()
    _local_projection()
    seen = {}

    def badges_by_keys(payload):
        seen.update(payload)
        return {k: {"badges": int(k) * 10} for k in payload["keys"]}

    register_function("users.badges_by_keys", badges_by_keys)
    result = read("catalog.user_badges", keys=[1, 2])
    assert seen == {"keys": ["1", "2"]}  # keys stringified on the wire
    assert result == {"1": {"badges": 10}, "2": {"badges": 20}}


def test_read_empty_keys_short_circuits():
    from stapel_core.comm.projections import read

    _local_projection()
    assert read("catalog.user_badges", keys=[]) == {}


def test_read_local_bad_live_query_shape(db):
    from stapel_core.comm import register_function
    from stapel_core.comm.projections import read
    from stapel_core.comm.registry import function_registry

    function_registry.clear()
    _local_projection()
    register_function("users.badges_by_keys", lambda payload: [1, 2])
    with pytest.raises(ProjectionError, match="must return a dict"):
        read("catalog.user_badges", keys=[1])
