"""Tests for the Signal primitive (stapel_core.comm.signals).

Signal is the fourth comm primitive: "show this to a live observer, if one is
watching". What has to be nailed down here is exactly what the contract
promises and — just as important — what it refuses to promise.
"""
import pytest
from django.db import transaction
from django.test import override_settings

from stapel_core.comm import (
    SIGNAL_ENVELOPE_VERSION,
    InvalidSignalType,
    InvalidStreamKey,
    SignalError,
    register_signal_transport,
    signal,
    signal_transport,
    stream_key,
)
from stapel_core.comm.signals import _transports


@pytest.fixture
def collected():
    """A transport that records (stream_key, frame), wired through settings."""
    frames = []

    def transport(stream, frame):
        frames.append((stream, frame))

    with override_settings(STAPEL_COMM={"SIGNAL_TRANSPORT": transport}):
        yield frames


@pytest.fixture(autouse=True)
def clean_registry():
    before = dict(_transports)
    yield
    _transports.clear()
    _transports.update(before)


# ---------------------------------------------------------------------------
# The default: no delivery backend, no noise
# ---------------------------------------------------------------------------


def test_no_transport_configured_is_a_silent_no_op():
    # No DB, no channel layer, no settings — an HTTP-only host is the norm
    # for 25 of 26 libraries, and emitting must cost them nothing.
    frame = signal("recordings:ws:42", "recording.status", {"status": "ready"})
    assert frame["stream"] == "recordings:ws:42"
    assert signal_transport() is None


def test_none_transport_resolves_to_no_delivery():
    with override_settings(STAPEL_COMM={"SIGNAL_TRANSPORT": "none"}):
        assert signal_transport() is None


def test_unresolvable_transport_drops_frames_rather_than_raising(caplog):
    # A signal must never break the caller — a typo in the axis is reported by
    # the stapel_core.comm.E003 system check at boot, not by a 500 mid-request.
    with override_settings(STAPEL_COMM={"SIGNAL_TRANSPORT": "not_a_transport"}):
        assert signal_transport() is None
        signal("recordings:ws:42", "recording.status")


def test_dotted_path_transport_is_resolved_without_registration():
    with override_settings(
        STAPEL_COMM={"SIGNAL_TRANSPORT": "stapel_core.comm.signals.stream_key"}
    ):
        assert signal_transport() is stream_key


def test_non_callable_dotted_path_resolves_to_none():
    with override_settings(STAPEL_COMM={
        "SIGNAL_TRANSPORT": "stapel_core.comm.signals.SIGNAL_ENVELOPE_VERSION"
    }):
        assert signal_transport() is None


def test_registered_transport_name_resolves():
    def transport(stream, frame):
        pass

    register_signal_transport("channels", transport)
    with override_settings(STAPEL_COMM={"SIGNAL_TRANSPORT": "channels"}):
        assert signal_transport() is transport


# ---------------------------------------------------------------------------
# The envelope (wire v1, shared with stapel-realtime)
# ---------------------------------------------------------------------------


def test_envelope_shape():
    frame = signal("chat:conv:7", "message.typing", {"user_id": "u1"})
    assert frame == {
        "v": 1,
        "type": "message.typing",
        "stream": "chat:conv:7",
        "payload": {"user_id": "u1"},
    }
    assert SIGNAL_ENVELOPE_VERSION == 1


def test_envelope_always_carries_the_stream_field():
    # Optional in the schema (a v1 socket serves one stream), but populated
    # from day one: it is what makes a multiplexed socket possible later
    # without breaking the envelope.
    assert "stream" in signal("video:lobby:AB12", "guest.waiting")


def test_envelope_never_carries_seq():
    # Frame kind is structural, not a flag: seq belongs to a journal frame
    # born from a module's persisted model, so an ephemeral signal physically
    # cannot be mistaken for — or persisted as — journal state.
    assert "seq" not in signal("video:lobby:AB12", "guest.waiting")


def test_missing_payload_becomes_an_empty_dict():
    assert signal("video:lobby:AB12", "guest.waiting")["payload"] == {}


@pytest.mark.django_db(transaction=True)
def test_delivered_frame_is_the_returned_frame(collected):
    frame = signal("recordings:ws:42", "recording.status", {"status": "ready"})
    assert collected == [("recordings:ws:42", frame)]


# ---------------------------------------------------------------------------
# Addressing — the canonical stream key
# ---------------------------------------------------------------------------


def test_stream_key_builder():
    assert stream_key("recordings", "ws", 42) == "recordings:ws:42"
    assert stream_key("chat", "conv", "7", "typing") == "chat:conv:7:typing"


@pytest.mark.parametrize("key", [
    "recordings",
    "recordings:ws",
    "recordings:ws:42:typing:extra",
    "recordings:ws:",
    "recordings ws 42",
    "recordings:ws:42 43",
    "",
])
def test_malformed_stream_key_is_refused(key):
    with pytest.raises(InvalidStreamKey):
        signal(key, "recording.status")


def test_stream_key_is_validated_even_with_no_transport():
    # The no-op default still gates the canon: a bad address is a programming
    # error, and it must not wait for the first host that turns delivery on.
    assert signal_transport() is None
    with pytest.raises(InvalidStreamKey):
        signal("nope", "recording.status")


def test_stream_key_builder_validates_its_own_output():
    with pytest.raises(InvalidStreamKey):
        stream_key("recordings", "ws", "42:43:44")


def test_invalid_stream_key_is_a_signal_error():
    assert issubclass(InvalidStreamKey, SignalError)
    assert issubclass(InvalidSignalType, SignalError)


# ---------------------------------------------------------------------------
# Frame types
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("type_", ["welcome", "ping", "resync", "kick", "error"])
def test_reserved_wire_protocol_types_are_refused(type_):
    with pytest.raises(InvalidSignalType):
        signal("chat:conv:7", type_)


@pytest.mark.parametrize("type_", ["Recording.Status", "recording status", "", "1st"])
def test_malformed_type_is_refused(type_):
    with pytest.raises(InvalidSignalType):
        signal("chat:conv:7", type_)


def test_payload_must_be_a_dict():
    with pytest.raises(TypeError):
        signal("chat:conv:7", "message.new", ["not", "a", "dict"])


# ---------------------------------------------------------------------------
# Timing — never ahead of the commit it describes
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_delivery_waits_for_commit(collected):
    with transaction.atomic():
        signal("recordings:ws:42", "recording.status", {"status": "ready"})
        assert collected == []
    assert len(collected) == 1


@pytest.mark.django_db(transaction=True)
def test_rollback_discards_the_signal(collected):
    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with transaction.atomic():
            signal("recordings:ws:42", "recording.status", {"status": "ready"})
            raise Boom()

    assert collected == []


@pytest.mark.django_db(transaction=True)
def test_outside_a_transaction_delivery_is_immediate(collected):
    # Nothing to wait for, so on_commit runs the callback right away.
    signal("recordings:ws:42", "recording.status", {"status": "ready"})
    assert len(collected) == 1


@pytest.mark.django_db(transaction=True)
def test_order_is_preserved_within_one_stream(collected):
    with transaction.atomic():
        signal("recordings:ws:42", "recording.status", {"n": 1})
        signal("recordings:ws:42", "recording.status", {"n": 2})
    assert [f["payload"]["n"] for _, f in collected] == [1, 2]


@pytest.mark.django_db(transaction=True)
def test_signal_writes_no_outbox_row():
    # A signal is not "an Action delivered to the browser": no durable row, no
    # retry, nothing accumulating in a table with no retention.
    from stapel_core.django.outbox.models import OutboxEvent

    with transaction.atomic():
        signal("recordings:ws:42", "recording.status", {"status": "ready"})
    assert OutboxEvent.objects.count() == 0


# ---------------------------------------------------------------------------
# The seam holds when the transport misbehaves
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_transport_failure_never_reaches_the_caller():
    def exploding(stream, frame):
        raise RuntimeError("channel layer is down")

    with override_settings(STAPEL_COMM={"SIGNAL_TRANSPORT": exploding}):
        with transaction.atomic():
            signal("recordings:ws:42", "recording.status")
    # The commit above went through: a lost frame is a legal outcome, and the
    # observer resyncs over REST.


@pytest.mark.django_db(transaction=True)
def test_transport_failure_does_not_stop_the_mutation():
    from stapel_core.django.users.models import User

    def exploding(stream, frame):
        raise RuntimeError("boom")

    with override_settings(STAPEL_COMM={"SIGNAL_TRANSPORT": exploding}):
        with transaction.atomic():
            User.objects.create_user(
                username="signal", email="signal@example.com", password="x")
            signal("recordings:ws:42", "recording.status")

    assert User.objects.filter(email="signal@example.com").exists()


@pytest.mark.django_db(transaction=True)
def test_transport_receives_the_routing_key_separately(collected):
    signal("chat:conv:7", "message.typing")
    stream, frame = collected[0]
    assert stream == "chat:conv:7" == frame["stream"]


# ---------------------------------------------------------------------------
# The boot-time gate for a misconfigured axis
# ---------------------------------------------------------------------------


def test_check_passes_with_no_transport():
    from stapel_core.comm.checks import check_signal_transport

    with override_settings(STAPEL_COMM={}):
        assert check_signal_transport() == []


def test_check_flags_an_unknown_transport_name():
    from stapel_core.comm.checks import (
        E003_SIGNAL_TRANSPORT_UNRESOLVABLE,
        check_signal_transport,
    )

    with override_settings(STAPEL_COMM={"SIGNAL_TRANSPORT": "chanels"}):
        errors = check_signal_transport()
    assert [e.id for e in errors] == [E003_SIGNAL_TRANSPORT_UNRESOLVABLE]


def test_check_flags_an_unimportable_dotted_path():
    from stapel_core.comm.checks import check_signal_transport

    with override_settings(
        STAPEL_COMM={"SIGNAL_TRANSPORT": "no_such_module.transport"}
    ):
        assert len(check_signal_transport()) == 1


def test_check_flags_a_non_callable_target():
    from stapel_core.comm.checks import check_signal_transport

    with override_settings(STAPEL_COMM={
        "SIGNAL_TRANSPORT": "stapel_core.comm.signals.SIGNAL_ENVELOPE_VERSION"
    }):
        assert len(check_signal_transport()) == 1


def test_check_passes_for_a_registered_name():
    from stapel_core.comm.checks import check_signal_transport

    register_signal_transport("channels", lambda stream, frame: None)
    with override_settings(STAPEL_COMM={"SIGNAL_TRANSPORT": "channels"}):
        assert check_signal_transport() == []
