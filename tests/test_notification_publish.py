"""request_notification: payload contract, shape validation, escape hatch."""
import json
from pathlib import Path

import pytest

from stapel_core.bus import get_bus
from stapel_core.kafka.events import EventType
from stapel_core.notifications import request_notification


def _published_payloads():
    bus = get_bus()
    return [
        e.payload for e in bus.events
        if e.event_type == EventType.NOTIFICATION_REQUESTED
    ]


def test_publishes_declared_payload_shape():
    assert request_notification(
        "otp_code",
        email="dest@example.com",
        variables={"code": "1234"},
        language="de",
    ) is True
    (payload,) = _published_payloads()
    assert payload == {
        "notification_type": "otp_code",
        "user_id": None,
        "email": "dest@example.com",
        "phone": None,
        "telegram_chat_id": None,
        "language": "de",
        "variables": {"code": "1234"},
    }
    # content_* keys are only present when given (schema has no null variant)
    assert "content_html" not in payload
    assert "content_text" not in payload


def test_content_escape_hatch_is_threaded_through_payload():
    assert request_notification(
        "adhoc.announcement",
        email="dest@example.com",
        content_html="<p>Hi</p>",
        content_text="Hi",
    ) is True
    (payload,) = _published_payloads()
    assert payload["content_html"] == "<p>Hi</p>"
    assert payload["content_text"] == "Hi"


@pytest.mark.parametrize("bad_type", ["", None, 42])
def test_missing_or_non_string_type_raises_early(bad_type):
    with pytest.raises(ValueError, match="notification_type"):
        request_notification(bad_type, email="dest@example.com")
    assert _published_payloads() == []


@pytest.mark.parametrize("kwargs", [
    {"content_html": 42},
    {"content_text": ["not", "a", "string"]},
])
def test_non_string_content_raises_early(kwargs):
    with pytest.raises(ValueError, match="must be a string"):
        request_notification("otp_code", email="dest@example.com", **kwargs)
    assert _published_payloads() == []


def test_telegram_chat_id_is_a_direct_address_like_email_and_phone():
    assert request_notification(
        "form.submission",
        telegram_chat_id="123456789",
        variables={"form": "contact"},
    ) is True
    (payload,) = _published_payloads()
    assert payload["telegram_chat_id"] == "123456789"
    assert payload["user_id"] is None
    assert payload["email"] is None
    assert payload["phone"] is None


def test_missing_recipient_returns_false_without_publishing():
    assert request_notification("otp_code") is False
    assert _published_payloads() == []


def _emit_schema():
    return json.loads(
        (Path(__file__).parent.parent / "notifications" / "schemas" / "emits"
         / "notification.requested.json").read_text()
    )


def test_payload_matches_committed_schema():
    """The emitted payload validates against the declared emit schema."""
    jsonschema = pytest.importorskip("jsonschema")
    request_notification(
        "otp_code",
        user_id="8f9e6a2c-0000-0000-0000-000000000001",
        variables={"code": "1234"},
        content_text="fallback",
    )
    (payload,) = _published_payloads()
    jsonschema.validate(payload, _emit_schema())


@pytest.mark.parametrize("chat_id", ["123456789", None])
def test_schema_accepts_telegram_chat_id_present_or_absent(chat_id):
    """Additive: a payload carrying the new address validates, and so does one
    from a producer that never sets it."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = _emit_schema()
    jsonschema.validate(
        {"notification_type": "otp_code", "telegram_chat_id": chat_id}, schema
    )
    jsonschema.validate({"notification_type": "otp_code"}, schema)


def test_schema_still_rejects_an_unknown_property():
    jsonschema = pytest.importorskip("jsonschema")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {"notification_type": "otp_code", "telegram_chat": "123"},
            _emit_schema(),
        )
