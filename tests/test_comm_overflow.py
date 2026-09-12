"""A reply too large for the broker travels by reference, transparently.

The defect, measured on the owner's stand (2026-09-09): ``llm.transcribe``
over two 148-minute meetings answered 8 637 982 bytes against a cap of
8 388 608. The transcription was done and billed at the provider; the answer
did not fit one NATS message and was thrown away.

The cap stays — a broker message is not a file. What changes is what happens
above it: the callee writes the bytes to the object store both services
share and answers a small ``{"$ref": ...}`` envelope, and the caller's
transport resolves it before ``call()`` returns. Every test below is about
one property of that: the caller sees NO difference.
"""
import hashlib
import json

import pytest

from stapel_core.comm import nats as nats_mod
from stapel_core.comm import overflow
from stapel_core.comm.exceptions import (
    FunctionPayloadTooLarge,
    FunctionReferenceError,
)
from stapel_core.django.management.commands.serve_functions import (
    decode_request,
    fit_reply,
)

CAP = 8 * 1024 * 1024  # the fleet's brokers


class MemoryStore(overflow.OverflowStore):
    """The shared bucket, in a dict. Counts what was asked of it."""

    name = "django"  # pose as the default so the store-name guard is exercised

    def __init__(self):
        self.objects = {}
        self.ttls = {}
        self.deleted = []

    def put(self, key, data, *, ttl_seconds):
        self.objects[key] = data
        self.ttls[key] = ttl_seconds
        return key

    def get(self, key):
        return self.objects[key]

    def delete(self, key):
        self.deleted.append(key)
        self.objects.pop(key, None)


@pytest.fixture
def store(settings):
    """A configured overflow store, on both ends (one process, one store)."""
    st = MemoryStore()
    settings.STAPEL_COMM = {
        **(getattr(settings, "STAPEL_COMM", {}) or {}),
        "LARGE_REPLY": {"STORE": st},
    }
    overflow.reset_store()
    yield st
    overflow.reset_store()


@pytest.fixture(autouse=True)
def _clean_store():
    overflow.reset_store()
    yield
    overflow.reset_store()


def transcript_frame(mb: float = 9.0) -> bytes:
    """A wire frame the size of a long meeting's transcript with timestamps."""
    words = int(mb * 1024 * 1024 / 64)
    frame = {"result": {"transcript": {
        "language": "ru",
        "words": [{"w": "слово", "s": i * 0.31, "e": i * 0.31 + 0.3} for i in range(words)],
    }}}
    return json.dumps(frame).encode()


class TestTheWireIsUnchangedBelowTheThreshold:
    def test_a_fitting_reply_is_passed_through_untouched(self, store):
        data = json.dumps({"result": {"summary": "short"}}).encode()
        assert fit_reply(data, CAP, "llm.summarize") is data
        assert store.objects == {}, "a store that is never touched costs nothing"

    def test_no_announced_limit_and_no_threshold_means_no_cap_of_our_own(self, store):
        data = b"x" * 10_000
        assert fit_reply(data, 0, "fn") is data
        assert store.objects == {}


class TestAnOversizedReplyTravelsByReference:
    def test_stored_then_dereferenced_gives_back_equal_bytes(self, store):
        """The property the whole mechanism exists for."""
        data = transcript_frame(9.0)
        assert len(data) > CAP, "the fixture must actually exceed the cap"

        wire = fit_reply(data, CAP, "llm.transcribe")

        # What crossed the broker is small enough to cross it.
        assert len(wire) < 1024
        envelope = json.loads(wire)
        ref = envelope["$ref"]
        assert ref["bytes"] == len(data)
        assert ref["sha256"] == hashlib.sha256(data).hexdigest()
        assert ref["key"] in store.objects

        # And the receiving end gets exactly what the function produced.
        assert overflow.dereference(ref, function="llm.transcribe") == data

    def test_the_caller_sees_no_difference_at_all(self, store, monkeypatch):
        """End to end across the seam: call() returns the same object.

        The 9 MB transcript the owner's stand lost, through the transport
        that lost it, with the mechanism in place.
        """
        result = {"transcript": {"words": [
            {"w": "слово", "speaker": "A", "start": i * 0.31, "end": i * 0.31 + 0.3}
            for i in range(110_000)
        ]}}
        reply = json.dumps({"result": result}).encode()
        assert len(reply) > CAP, f"the fixture is only {len(reply)} bytes"

        wire = fit_reply(reply, CAP, "llm.transcribe")

        class _Bridge:
            def max_payload(self, timeout=5.0):
                return CAP

            def request(self, subject, data, timeout):
                assert len(data) <= CAP, "the broker would have refused this"
                return wire

        monkeypatch.setattr(nats_mod, "get_bridge", lambda: _Bridge())
        got = nats_mod.nats_function_transport("llm.transcribe", {"audio_url": "s3://a"})
        assert got == result

    def test_the_object_is_a_postbox_and_is_consumed(self, store):
        data = transcript_frame(9.0)
        ref = json.loads(fit_reply(data, CAP, "llm.transcribe"))["$ref"]
        overflow.dereference(ref, function="llm.transcribe")
        assert store.deleted == [ref["key"]]
        assert store.objects == {}, (
            "a second verbatim copy of a private payload, under no row of any "
            "table, is a copy no erasure sweep would ever find"
        )


class TestTheReferenceIsVerified:
    def test_a_digest_mismatch_is_refused(self, store):
        data = transcript_frame(9.0)
        ref = json.loads(fit_reply(data, CAP, "llm.transcribe"))["$ref"]
        tampered = bytearray(store.objects[ref["key"]])
        tampered[-2:] = b"!!"
        store.objects[ref["key"]] = bytes(tampered)

        with pytest.raises(FunctionReferenceError) as exc:
            overflow.dereference(ref, function="llm.transcribe")
        assert "sha256" in str(exc.value)

    def test_a_short_read_is_refused_rather_than_delivered(self, store):
        data = transcript_frame(9.0)
        ref = json.loads(fit_reply(data, CAP, "llm.transcribe"))["$ref"]
        store.objects[ref["key"]] = store.objects[ref["key"]][:-100]

        with pytest.raises(FunctionReferenceError) as exc:
            overflow.dereference(ref, function="llm.transcribe")
        assert "bytes" in str(exc.value)

    def test_a_key_outside_the_prefix_is_refused(self, store):
        ref = {"store": "django", "key": "../../etc/passwd", "bytes": 3}
        with pytest.raises(FunctionReferenceError) as exc:
            overflow.dereference(ref, function="evil.fn")
        assert "PREFIX" in str(exc.value)

    def test_a_reference_to_another_store_is_refused(self, store):
        ref = {"store": "somebody-elses-bucket", "key": "stapel/comm/overflow/x"}
        with pytest.raises(FunctionReferenceError) as exc:
            overflow.dereference(ref, function="fn")
        assert "both ends" in str(exc.value)

    def test_an_expired_object_names_the_ttl_setting(self, store):
        data = transcript_frame(9.0)
        ref = json.loads(fit_reply(data, CAP, "llm.transcribe"))["$ref"]
        store.objects.clear()  # the lifecycle rule got there first

        with pytest.raises(FunctionReferenceError) as exc:
            overflow.dereference(ref, function="llm.transcribe")
        assert "TTL_SECONDS" in str(exc.value)


class TestTheObjectIsGivenALifetime:
    def test_the_default_is_24h_and_reaches_the_store(self, store):
        ref = json.loads(fit_reply(transcript_frame(9.0), CAP, "fn"))["$ref"]
        assert store.ttls[ref["key"]] == 86400
        assert ref["expires_at"].endswith("+00:00")

    def test_a_configured_ttl_is_what_the_store_is_told(self, settings):
        st = MemoryStore()
        settings.STAPEL_COMM = {
            **(getattr(settings, "STAPEL_COMM", {}) or {}),
            "LARGE_REPLY": {"STORE": st, "TTL_SECONDS": 600},
        }
        overflow.reset_store()
        ref = json.loads(fit_reply(transcript_frame(9.0), CAP, "fn"))["$ref"]
        assert st.ttls[ref["key"]] == 600


class TestWithoutAStoreNothingIsLostSilently:
    def test_the_callee_still_sends_the_marker(self, settings):
        """The pre-0.66 behaviour, unchanged where there is no store."""
        settings.STAPEL_COMM = {
            **(getattr(settings, "STAPEL_COMM", {}) or {}), "LARGE_REPLY": {},
        }
        overflow.reset_store()
        data = transcript_frame(9.0)
        out = fit_reply(data, CAP, "llm.transcribe")
        parsed = json.loads(out)
        assert parsed["error_code"] == "payload_too_large"
        assert parsed["size"] == len(data)

    def test_the_caller_gets_an_error_naming_the_setting(self, settings):
        settings.STAPEL_COMM = {
            **(getattr(settings, "STAPEL_COMM", {}) or {}), "LARGE_REPLY": {},
        }
        overflow.reset_store()
        ref = {"store": "django", "key": "stapel/comm/overflow/reply/f/x.json",
               "bytes": 10}
        with pytest.raises(FunctionReferenceError) as exc:
            overflow.dereference(ref, function="llm.transcribe")
        text = str(exc.value)
        assert 'STAPEL_COMM["LARGE_REPLY"]["STORE"]' in text

    def test_an_oversized_request_still_refuses_before_the_wire(
        self, settings, monkeypatch
    ):
        settings.STAPEL_COMM = {
            **(getattr(settings, "STAPEL_COMM", {}) or {}), "LARGE_REPLY": {},
        }
        overflow.reset_store()
        sent = []

        class _Bridge:
            def max_payload(self, timeout=5.0):
                return 100

            def request(self, subject, data, timeout):  # pragma: no cover
                sent.append(data)
                raise AssertionError("must not reach the wire")

        monkeypatch.setattr(nats_mod, "get_bridge", lambda: _Bridge())
        with pytest.raises(FunctionPayloadTooLarge) as exc:
            nats_mod.nats_function_transport("big.fn", {"blob": "x" * 500})
        assert exc.value.direction == "request"
        assert sent == []


class TestTheRequestDirectionToo:
    def test_an_oversized_request_travels_by_reference(self, store, monkeypatch):
        """The mirror image: a Function whose ARGUMENT is the bulk."""
        seen = {}

        class _Bridge:
            def max_payload(self, timeout=5.0):
                return 1000

            def request(self, subject, data, timeout):
                seen["data"] = data
                return json.dumps({"result": "ok"}).encode()

        monkeypatch.setattr(nats_mod, "get_bridge", lambda: _Bridge())
        payload = {"document": "x" * 50_000}
        assert nats_mod.nats_function_transport("llm.summarize", payload) == "ok"

        assert len(seen["data"]) < 1000, "the request crossed a 1000-byte broker"
        # ...and the server side resolves it back to the payload the caller
        # passed, which is the only thing the handler ever sees.
        assert decode_request(seen["data"], "llm.summarize") == payload


class TestTheThresholdIsConfigurable:
    def test_below_the_broker_cap_the_deployment_may_still_offload(self, settings):
        st = MemoryStore()
        settings.STAPEL_COMM = {
            **(getattr(settings, "STAPEL_COMM", {}) or {}),
            "LARGE_REPLY": {"STORE": st, "THRESHOLD_BYTES": 1000},
        }
        overflow.reset_store()
        data = json.dumps({"result": "y" * 5000}).encode()
        wire = fit_reply(data, CAP, "fn")
        assert json.loads(wire)["$ref"]["bytes"] == len(data)

    def test_a_threshold_above_the_cap_cannot_raise_it(self, settings):
        """Past max_payload the message does not go out at all."""
        settings.STAPEL_COMM = {
            **(getattr(settings, "STAPEL_COMM", {}) or {}),
            "LARGE_REPLY": {"STORE": MemoryStore(), "THRESHOLD_BYTES": 64 * 1024 * 1024},
        }
        overflow.reset_store()
        assert overflow.threshold_bytes(CAP) == CAP

    def test_a_store_that_is_down_never_loses_a_reply_that_fits(self, settings):
        """A preference must not turn a working reply into a failure."""
        class _Broken(MemoryStore):
            def put(self, key, data, *, ttl_seconds):
                raise RuntimeError("bucket unreachable")

        settings.STAPEL_COMM = {
            **(getattr(settings, "STAPEL_COMM", {}) or {}),
            "LARGE_REPLY": {"STORE": _Broken(), "THRESHOLD_BYTES": 1000},
        }
        overflow.reset_store()
        data = json.dumps({"result": "y" * 5000}).encode()
        assert fit_reply(data, CAP, "fn") is data


class TestTheDefaultStoreIsTheSharedBucket:
    def test_django_default_storage_round_trip(self, settings, tmp_path):
        """"django" is the fleet's shared S3/MinIO bucket in production and
        the filesystem in a test — the same Storage API either way."""
        settings.MEDIA_ROOT = str(tmp_path)
        settings.STAPEL_COMM = {
            **(getattr(settings, "STAPEL_COMM", {}) or {}),
            "LARGE_REPLY": {"STORE": "django"},
        }
        overflow.reset_store()

        data = transcript_frame(1.0)
        ref = overflow.store_frame(data, function="llm.transcribe", direction="reply")
        assert ref["store"] == "django"
        assert (tmp_path / ref["key"]).exists()
        assert overflow.dereference(ref, function="llm.transcribe") == data
        assert not (tmp_path / ref["key"]).exists(), "read once, then gone"


class TestTheStoreIsCheckedAtBoot:
    def test_a_typo_in_the_dotted_path_fails_the_check(self, settings):
        from stapel_core.comm.checks import check_large_reply_store

        settings.STAPEL_COMM = {
            **(getattr(settings, "STAPEL_COMM", {}) or {}),
            "LARGE_REPLY": {"STORE": "myapp.storage.NoSuchStore"},
        }
        overflow.reset_store()
        errors = check_large_reply_store()
        assert [e.id for e in errors] == ["stapel_core.comm.E004"]

    def test_no_store_is_the_default_and_is_never_reported(self, settings):
        from stapel_core.comm.checks import check_large_reply_store

        settings.STAPEL_COMM = {
            **(getattr(settings, "STAPEL_COMM", {}) or {}), "LARGE_REPLY": {},
        }
        overflow.reset_store()
        assert check_large_reply_store() == []
