"""A function call must never die silently on the transport's size cap.

The defect, measured on ironmemo (2026-08-06, upload path): a
``llm.complete`` reply over a meeting transcript exceeded NATS's 1 MiB
``max_payload``. ``msg.respond()`` raised ``MaxPayloadError`` INSIDE the
subscription callback — after the function had already run. Nothing was sent
back, so the caller sat until its timeout and reported a generic failure; the
only line naming the real cause was in the server's log, on another host.

Two guards, one on each end:
  * the client refuses an oversized REQUEST before publishing, naming the size
    and the limit, instead of letting nats-py raise an opaque MaxPayloadError;
  * the server sends a small structured marker instead of an oversized REPLY,
    which the client turns back into the same precise exception.
"""
import json

import pytest

from stapel_core.comm.exceptions import FunctionCallError, FunctionPayloadTooLarge


class TestExceptionShape:
    def test_carries_the_numbers_a_human_needs(self):
        exc = FunctionPayloadTooLarge("llm.complete", 2_000_000, 1_048_576, direction="reply")
        assert exc.function == "llm.complete"
        assert exc.size == 2_000_000
        assert exc.limit == 1_048_576
        assert exc.direction == "reply"
        text = str(exc)
        assert "llm.complete" in text
        assert "2000000" in text and "1048576" in text
        # and it must say what to DO, not merely that something is too big
        assert "REFERENCE" in text or "reference" in text

    def test_is_a_function_call_error(self):
        """Callers that already catch FunctionCallError keep working."""
        assert issubclass(FunctionPayloadTooLarge, FunctionCallError)


class TestClientRefusesAnOversizedRequest:
    def test_raises_before_publishing(self, monkeypatch):
        from stapel_core.comm import nats as nats_mod

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
        assert exc.value.limit == 100
        assert sent == []

    def test_a_payload_under_the_limit_still_goes_through(self, monkeypatch):
        from stapel_core.comm import nats as nats_mod

        class _Bridge:
            def max_payload(self, timeout=5.0):
                return 10_000

            def request(self, subject, data, timeout):
                return json.dumps({"result": {"ok": True}}).encode()

        monkeypatch.setattr(nats_mod, "get_bridge", lambda: _Bridge())
        assert nats_mod.nats_function_transport("small.fn", {"a": 1}) == {"ok": True}

    def test_an_unknown_limit_does_not_block_the_call(self, monkeypatch):
        """A broker that announced nothing must not make every call fail."""
        from stapel_core.comm import nats as nats_mod

        class _Bridge:
            def max_payload(self, timeout=5.0):
                return 0

            def request(self, subject, data, timeout):
                return json.dumps({"result": "fine"}).encode()

        monkeypatch.setattr(nats_mod, "get_bridge", lambda: _Bridge())
        assert nats_mod.nats_function_transport("fn", {"x": "y" * 10_000}) == "fine"


class TestClientTranslatesTheServerMarker:
    def test_too_large_marker_becomes_the_precise_exception(self, monkeypatch):
        from stapel_core.comm import nats as nats_mod

        class _Bridge:
            def max_payload(self, timeout=5.0):
                return 1_048_576

            def request(self, subject, data, timeout):
                return json.dumps({
                    "error": "reply of 2000000 bytes exceeds the transport limit",
                    "error_code": "payload_too_large",
                    "size": 2_000_000,
                    "limit": 1_048_576,
                }).encode()

        monkeypatch.setattr(nats_mod, "get_bridge", lambda: _Bridge())
        with pytest.raises(FunctionPayloadTooLarge) as exc:
            nats_mod.nats_function_transport("llm.complete", {"prompt": "hi"})
        assert exc.value.direction == "reply"
        assert exc.value.size == 2_000_000

    def test_an_ordinary_remote_error_is_unchanged(self, monkeypatch):
        from stapel_core.comm import nats as nats_mod

        class _Bridge:
            def max_payload(self, timeout=5.0):
                return 1_048_576

            def request(self, subject, data, timeout):
                return json.dumps({"error": "ValueError('nope')"}).encode()

        monkeypatch.setattr(nats_mod, "get_bridge", lambda: _Bridge())
        with pytest.raises(FunctionCallError) as exc:
            nats_mod.nats_function_transport("fn", {})
        assert not isinstance(exc.value, FunctionPayloadTooLarge)
        assert "nope" in str(exc.value)


class TestServerSubstitutesAMarker:
    """The half that actually broke: the reply that could not be sent."""

    def _fit(self, *args):
        from stapel_core.django.management.commands.serve_functions import fit_reply

        return fit_reply(*args)

    def test_a_fitting_reply_is_passed_through_untouched(self):
        data = json.dumps({"result": {"summary": "short"}}).encode()
        assert self._fit(data, 1_048_576, "llm.complete") is data

    def test_an_oversized_reply_becomes_a_small_marker(self):
        data = json.dumps({"result": "x" * 5000}).encode()
        out = self._fit(data, 1000, "llm.complete")
        assert len(out) < 1000  # the marker itself must fit the wire
        parsed = json.loads(out)
        assert parsed["error_code"] == "payload_too_large"
        assert parsed["size"] == len(data)
        assert parsed["limit"] == 1000

    def test_no_announced_limit_means_no_cap_of_our_own(self):
        data = b"x" * 10_000
        assert self._fit(data, 0, "fn") is data

    def test_round_trip_marker_to_exception(self):
        """Server marker -> client exception, end to end across the seam."""
        from stapel_core.comm import nats as nats_mod

        big = json.dumps({"result": "y" * 5000}).encode()
        marker = self._fit(big, 1000, "llm.complete")

        class _Bridge:
            def max_payload(self, timeout=5.0):
                return 1000

            def request(self, subject, data, timeout):
                return marker

        import pytest as _pytest

        with _pytest.MonkeyPatch.context() as mp:
            mp.setattr(nats_mod, "get_bridge", lambda: _Bridge())
            with _pytest.raises(FunctionPayloadTooLarge) as exc:
                nats_mod.nats_function_transport("llm.complete", {"p": 1})
        assert exc.value.direction == "reply"
        assert exc.value.size == len(big)
        assert exc.value.limit == 1000
