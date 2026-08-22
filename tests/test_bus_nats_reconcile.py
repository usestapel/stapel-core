"""Durable-consumer reconciliation for the NATS JetStream bus backend.

The defect these tests pin down was found live, on a running fleet: a
service gained a subscription topic, its worker logged the widened subject
list, publishers kept writing, and the handler never ran once. Cause:
``js.pull_subscribe(durable=...)`` binds to an existing consumer and drops
the ConsumerConfig it was handed (nats-py sets ``should_create = False``),
so the durable kept its pre-deploy ``filter_subjects`` forever. Nothing
errored anywhere — the seam was silent on both sides.

The tests use the REAL ``nats.js.api`` config/enum types against a fake
JetStream context, so field names, enum values and defaults are the ones
the server actually round-trips. The fake models the one server behaviour
the fix depends on: an in-place consumer edit keeps the ack floor, while a
delete+recreate resets it.
"""
import asyncio

import pytest

nats_api = pytest.importorskip("nats.js.api", reason="nats-py not installed")
nats_js_errors = pytest.importorskip("nats.js.errors")

from stapel_core.bus.backends.nats import (  # noqa: E402
    IMMUTABLE_FIELDS,
    RECONCILABLE_FIELDS,
    START_POSITION_FIELDS,
    ConsumerConfigConflict,
    assert_consumer_matches,
    consumer_drift,
    reconcile_durable,
    subject_set,
)

ConsumerConfig = nats_api.ConsumerConfig
STREAM = "stapel-events"


def run(coro):
    """The suite has no pytest-asyncio; the backend drives its own loop too."""
    return asyncio.run(coro)


DURABLE = "chat-service_actions"

OLD_SUBJECTS = ["stapel.evt.chat.message", "stapel.evt.chat.room"]
NEW_SUBJECTS = [*OLD_SUBJECTS, "stapel.evt.user.created"]


def declared_config(subjects=NEW_SUBJECTS, **overrides):
    """What the backend declares — mirrors NatsJetStreamBus._consume."""
    kwargs = dict(
        durable_name=DURABLE, filter_subjects=list(subjects), max_deliver=-1, ack_wait=300
    )
    kwargs.update(overrides)
    return ConsumerConfig(**kwargs)


def live_config(subjects=OLD_SUBJECTS, **overrides):
    """What the server reports for an already-existing durable."""
    return declared_config(subjects, **overrides)


class FakeConsumer:
    """A server-side consumer with the delivery bookkeeping that matters."""

    def __init__(self, config, *, delivered=0, ack_floor=0):
        self.config = config
        self.delivered = delivered
        self.ack_floor = ack_floor


class FakeConsumerInfo:
    def __init__(self, consumer):
        self.config = consumer.config
        self.delivered = consumer.delivered
        self.ack_floor = consumer.ack_floor


class FakeJS:
    """Minimal JetStream context: consumer CRUD plus a call log."""

    def __init__(self, consumers=None, *, update_error=None):
        self.consumers = dict(consumers or {})
        self.update_error = update_error
        self.calls = []

    async def consumer_info(self, stream, durable):
        self.calls.append(("consumer_info", durable))
        try:
            return FakeConsumerInfo(self.consumers[durable])
        except KeyError:
            raise nats_js_errors.NotFoundError() from None

    async def add_consumer(self, stream, config=None):
        """CONSUMER.DURABLE.CREATE — an in-place edit when it exists."""
        self.calls.append(("add_consumer", config.durable_name))
        if self.update_error is not None:
            raise self.update_error
        existing = self.consumers.get(config.durable_name)
        if existing is None:
            self.consumers[config.durable_name] = FakeConsumer(config)
        else:
            # In place: the position survives, which is the whole point.
            existing.config = config
        return FakeConsumerInfo(self.consumers[config.durable_name])

    async def delete_consumer(self, stream, durable):
        self.calls.append(("delete_consumer", durable))
        self.consumers.pop(durable, None)
        return True

    def calls_named(self, name):
        return [call for call in self.calls if call[0] == name]


# ---------------------------------------------------------------------------
# subject_set / consumer_drift — the comparison itself
# ---------------------------------------------------------------------------


def test_subject_set_normalises_both_spellings():
    """The server reports one filter as filter_subject, many as
    filter_subjects. The same filter spelled either way must compare equal."""
    single = ConsumerConfig(durable_name=DURABLE, filter_subject="stapel.evt.a")
    plural = ConsumerConfig(durable_name=DURABLE, filter_subjects=["stapel.evt.a"])

    assert subject_set(single) == subject_set(plural) == frozenset({"stapel.evt.a"})
    assert consumer_drift(single, plural) == {}
    assert subject_set(ConsumerConfig(durable_name=DURABLE)) == frozenset()


def test_subject_order_is_not_drift():
    assert consumer_drift(declared_config(NEW_SUBJECTS), live_config(NEW_SUBJECTS[::-1])) == {}


def test_widened_subjects_are_drift():
    drift = consumer_drift(declared_config(NEW_SUBJECTS), live_config(OLD_SUBJECTS))

    assert set(drift) == {"filter_subjects"}
    want, got = drift["filter_subjects"]
    assert "stapel.evt.user.created" in want
    assert "stapel.evt.user.created" not in got


def test_undeclared_fields_are_left_alone():
    """An operator may have tuned max_ack_pending by hand. The backend does
    not declare it, so it is none of this deployment's business."""
    live = live_config(NEW_SUBJECTS, max_ack_pending=5000)
    assert consumer_drift(declared_config(NEW_SUBJECTS), live) == {}


@pytest.mark.parametrize("field", START_POSITION_FIELDS)
def test_start_position_fields_are_never_drift(field):
    """deliver_policy / opt_start_* decide where a consumer STARTS. An
    existing durable is long past that point — a difference is history, not
    drift, and crash-looping over it would be a false alarm."""
    assert field not in RECONCILABLE_FIELDS
    assert field not in IMMUTABLE_FIELDS

    live = live_config(NEW_SUBJECTS)
    live.deliver_policy = nats_api.DeliverPolicy.NEW
    live.opt_start_seq = 4242
    assert consumer_drift(declared_config(NEW_SUBJECTS), live) == {}


def test_ack_wait_drift_is_detected_and_reconcilable():
    """ack_wait is the other field that goes wrong quietly: the 30s default
    redelivers messages the handler is still retrying. It is mutable, so it
    reconciles rather than crashes."""
    drift = consumer_drift(declared_config(), live_config(NEW_SUBJECTS, ack_wait=30))

    assert set(drift) == {"ack_wait"}
    assert drift["ack_wait"] == (300, 30)
    assert not set(drift) & set(IMMUTABLE_FIELDS)


def test_ack_wait_float_nanosecond_roundtrip_is_not_drift():
    """The server returns ack_wait in nanoseconds; nats-py divides back into
    a float. 300 must not read as drift against 300.0."""
    assert consumer_drift(declared_config(), live_config(NEW_SUBJECTS, ack_wait=300.0)) == {}


def test_ack_policy_drift_is_immutable():
    live = live_config(NEW_SUBJECTS, ack_policy=nats_api.AckPolicy.NONE)
    drift = consumer_drift(declared_config(), live)

    assert drift["ack_policy"] == ("explicit", "none")
    assert set(drift) & set(IMMUTABLE_FIELDS)


def test_push_consumer_under_a_pull_binding_is_drift():
    """A durable with deliver_subject is a push consumer; a pull binding
    would never drain it. Nothing is declared there, so this one is
    compared unconditionally."""
    live = live_config(NEW_SUBJECTS, deliver_subject="_INBOX.legacy")
    drift = consumer_drift(declared_config(), live)

    assert drift["deliver_subject"] == (None, "_INBOX.legacy")


# ---------------------------------------------------------------------------
# reconcile_durable — the four outcomes
# ---------------------------------------------------------------------------


def test_fresh_durable_is_left_to_pull_subscribe():
    js = FakeJS()

    assert run(reconcile_durable(js, STREAM, DURABLE, declared_config())) == "absent"
    assert js.calls_named("add_consumer") == []
    assert js.calls_named("delete_consumer") == []


def test_matching_durable_is_not_touched():
    js = FakeJS({DURABLE: FakeConsumer(live_config(NEW_SUBJECTS))})

    assert run(reconcile_durable(js, STREAM, DURABLE, declared_config())) == "matched"
    assert js.calls_named("add_consumer") == []


def test_widened_subjects_are_reconciled_in_place(caplog):
    """The live defect: durable exists with the pre-deploy subject set."""
    js = FakeJS({DURABLE: FakeConsumer(live_config(OLD_SUBJECTS))})

    with caplog.at_level("WARNING"):
        outcome = run(reconcile_durable(js, STREAM, DURABLE, declared_config()))

    assert outcome == "reconciled"
    assert js.calls_named("add_consumer") == [("add_consumer", DURABLE)]
    assert js.calls_named("delete_consumer") == []  # never recreate from zero
    assert subject_set(js.consumers[DURABLE].config) == frozenset(NEW_SUBJECTS)

    # Loud, with both sets in the line.
    message = caplog.text
    assert "reconciling durable=chat-service_actions" in message
    assert "stapel.evt.user.created" in message
    assert "before=" in message and "after=" in message


def test_narrowed_subjects_are_reconciled_in_place():
    """A topic dropped from the code must be dropped server-side too, or the
    worker keeps receiving events no handler is registered for."""
    js = FakeJS({DURABLE: FakeConsumer(live_config(NEW_SUBJECTS))})

    outcome = run(reconcile_durable(js, STREAM, DURABLE, declared_config(OLD_SUBJECTS)))

    assert outcome == "reconciled"
    assert subject_set(js.consumers[DURABLE].config) == frozenset(OLD_SUBJECTS)
    assert js.calls_named("delete_consumer") == []


def test_reconcile_preserves_the_ack_floor():
    """Publish 3, ack 2, widen the subjects: the next delivery is #3.

    An in-place edit keeps delivered/ack_floor; a delete+recreate would
    reset both and replay the whole stream through the handler. This is the
    reason the fix refuses to recreate even when the server says no.
    """
    js = FakeJS({DURABLE: FakeConsumer(live_config(OLD_SUBJECTS), delivered=2, ack_floor=2)})

    run(reconcile_durable(js, STREAM, DURABLE, declared_config(NEW_SUBJECTS)))

    consumer = js.consumers[DURABLE]
    assert consumer.ack_floor == 2, "ack floor reset — the stream would replay"
    assert consumer.delivered == 2
    assert consumer.delivered + 1 == 3  # next delivery is #3, not #1
    assert js.calls_named("delete_consumer") == []


def test_immutable_drift_fails_loudly_and_changes_nothing():
    live = live_config(OLD_SUBJECTS, ack_policy=nats_api.AckPolicy.NONE)
    js = FakeJS({DURABLE: FakeConsumer(live)})

    with pytest.raises(ConsumerConfigConflict) as excinfo:
        run(reconcile_durable(js, STREAM, DURABLE, declared_config()))

    message = str(excinfo.value)
    assert DURABLE in message
    assert "ack_policy" in message
    assert "stapel.evt.user.created" in message  # declared set
    assert "stapel.evt.chat.room" in message     # actual set
    # No half-applied state, and above all no recreate.
    assert js.calls_named("add_consumer") == []
    assert js.calls_named("delete_consumer") == []


def test_server_rejection_fails_loudly():
    """Some fields are immutable only on some server versions. When the edit
    comes back rejected the boot must die naming the durable, not carry on
    deaf."""
    rejection = nats_js_errors.BadRequestError(
        description="consumer filter subject cannot be updated", err_code=10052
    )
    js = FakeJS({DURABLE: FakeConsumer(live_config(OLD_SUBJECTS))}, update_error=rejection)

    with pytest.raises(ConsumerConfigConflict) as excinfo:
        run(reconcile_durable(js, STREAM, DURABLE, declared_config()))

    message = str(excinfo.value)
    assert DURABLE in message
    assert "rejected the in-place update" in message
    assert "stapel.evt.user.created" in message
    assert excinfo.value.__cause__ is rejection
    assert js.calls_named("delete_consumer") == []


def test_update_consumer_is_preferred_when_the_client_has_one():
    """nats-py 2.15 has no update_consumer(); a later one may. Use it if so."""
    js = FakeJS({DURABLE: FakeConsumer(live_config(OLD_SUBJECTS))})
    seen = []

    async def update_consumer(stream, config=None):
        seen.append(config)
        js.consumers[DURABLE].config = config

    js.update_consumer = update_consumer

    assert run(reconcile_durable(js, STREAM, DURABLE, declared_config())) == "reconciled"
    assert js.calls_named("add_consumer") == []
    assert subject_set(seen[0]) == frozenset(NEW_SUBJECTS)


# ---------------------------------------------------------------------------
# assert_consumer_matches — the startup invariant
# ---------------------------------------------------------------------------


def test_assert_logs_the_verified_subjects(caplog):
    js = FakeJS({DURABLE: FakeConsumer(live_config(NEW_SUBJECTS))})

    with caplog.at_level("INFO"):
        run(assert_consumer_matches(js, STREAM, DURABLE, declared_config()))

    assert "verified against the server" in caplog.text
    assert "stapel.evt.user.created" in caplog.text


def test_assert_catches_an_update_the_server_swallowed():
    """Belt and braces: a server that answers OK without applying the edit
    still cannot leave a deaf worker running."""
    js = FakeJS({DURABLE: FakeConsumer(live_config(OLD_SUBJECTS))})

    with pytest.raises(ConsumerConfigConflict, match="still differs after setup"):
        run(assert_consumer_matches(js, STREAM, DURABLE, declared_config()))
