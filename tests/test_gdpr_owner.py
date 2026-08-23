"""stapel_core.gdpr.owners — the data-owner side of the erasure protocol.

Every arm the nine hand-written copies had, tested once: the receipt and its
deterministic id, the transaction the receipt rides, the subject type an
owner does not claim, the malformed payload, the unusable key, the probe
answered from the same module, the deprecated account signal, and the
pseudonym funnel.
"""
import pytest
from django.test import override_settings

from stapel_core.bus.event import Event
from stapel_core.comm import action_registry, emit
from stapel_core.gdpr import (
    ERASURE_REQUESTED,
    OWNER_ALIVE,
    OWNER_PROBE,
    SECTION_ERASED,
    pseudonymize,
    receipt_id,
    register_gdpr_owner,
    registered_gdpr_owners,
)
from stapel_core.gdpr.owners import _reset_gdpr_owners

INPROCESS = {"OUTBOX_ENABLED": False, "ACTION_TRANSPORT": "inprocess"}


@pytest.fixture(autouse=True)
def clean_registries():
    action_registry.clear()
    _reset_gdpr_owners()
    yield
    action_registry.clear()
    _reset_gdpr_owners()


@pytest.fixture
def sink():
    """Collect every action delivered in-process, by name."""
    seen: dict[str, list[dict]] = {}

    def collect(name):
        def handler(event):
            seen.setdefault(name, []).append(event.payload)

        action_registry.subscribe(name, handler)

    for name in (SECTION_ERASED, OWNER_ALIVE):
        collect(name)
    return seen


def _request(**payload):
    return Event(event_type=ERASURE_REQUESTED, service="gdpr", payload=payload)


class _Eraser:
    """A minimal owner: remembers its calls, counts what it 'removed'."""

    _DEFAULT = object()

    def __init__(self, counts=_DEFAULT, raises=None):
        self.calls = []
        self.counts = {"rows": 2} if counts is self._DEFAULT else counts
        self.raises = raises

    def __call__(self, subject_type, subject_key, workspace_id=None):
        self.calls.append((subject_type, subject_key, workspace_id))
        if self.raises is not None:
            raise self.raises
        return self.counts


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def test_one_call_subscribes_the_whole_protocol():
    erase = _Eraser()
    reg = register_gdpr_owner("recordings", ["account", "recording"], erase)

    assert reg.owner == "recordings"
    assert reg.subject_types == ("account", "recording")
    assert registered_gdpr_owners() == {"recordings": ("account", "recording")}
    for name in (ERASURE_REQUESTED, OWNER_PROBE, "user.deleted"):
        assert action_registry.handlers(name), f"nothing subscribed to {name}"


def test_registering_twice_with_the_same_terms_does_not_double_subscribe():
    erase = _Eraser()
    first = register_gdpr_owner("billing", ["account"], erase)
    second = register_gdpr_owner("billing", ["account"], erase)

    assert first is second
    assert len(action_registry.handlers(ERASURE_REQUESTED)) == 1


def test_registering_the_same_owner_with_different_terms_is_refused():
    register_gdpr_owner("billing", ["account"], _Eraser())
    with pytest.raises(ValueError, match="already registered"):
        register_gdpr_owner("billing", ["account", "workspace"], _Eraser())


@pytest.mark.parametrize(
    "args",
    [
        ("", ["account"], _Eraser()),
        ("docs", [], _Eraser()),
    ],
)
def test_an_owner_that_erases_nothing_is_refused(args):
    with pytest.raises(ValueError):
        register_gdpr_owner(*args)


def test_a_non_callable_erase_is_refused():
    with pytest.raises(TypeError):
        register_gdpr_owner("docs", ["document"], "erase_subject")


def test_no_user_deleted_handler_without_the_account_subject():
    reg = register_gdpr_owner("docs", ["document"], _Eraser())
    assert reg.handle_user_deleted is None
    assert action_registry.handlers("user.deleted") == []


def test_legacy_user_deleted_can_be_switched_off():
    reg = register_gdpr_owner(
        "docs", ["account"], _Eraser(), legacy_user_deleted=False
    )
    assert reg.handle_user_deleted is None
    assert action_registry.handlers("user.deleted") == []


# ---------------------------------------------------------------------------
# gdpr.erasure.requested
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_erasure_receipts_with_counts_and_a_deterministic_id(sink):
    erase = _Eraser({"documents": 3})
    with override_settings(STAPEL_COMM=INPROCESS):
        reg = register_gdpr_owner("docs", ["account", "document"], erase)
        reg.handle_erasure_requested(
            _request(
                correlation_id="c-1",
                subject_type="document",
                subject_key="doc-9",
                workspace_id="ws-1",
            )
        )

    assert erase.calls == [("document", "doc-9", "ws-1")]
    (receipt,) = sink[SECTION_ERASED]
    assert receipt == {
        "owner": "docs",
        "subject_type": "document",
        "subject_key": "doc-9",
        "correlation_id": "c-1",
        "receipt_id": "docs:document:doc-9:c-1",
        "counts": {"documents": 3},
    }
    assert receipt["receipt_id"] == receipt_id("docs", "document", "doc-9", "c-1")


@pytest.mark.django_db
def test_a_redelivery_mints_the_same_receipt_id(sink):
    erase = _Eraser({"documents": 0})
    with override_settings(STAPEL_COMM=INPROCESS):
        reg = register_gdpr_owner("docs", ["document"], erase)
        event = _request(
            correlation_id="c-1", subject_type="document", subject_key="doc-9"
        )
        reg.handle_erasure_requested(event)
        reg.handle_erasure_requested(event)

    first, second = sink[SECTION_ERASED]
    assert first["receipt_id"] == second["receipt_id"]
    assert second["counts"] == {"documents": 0}  # honest zero, not a re-claim


@pytest.mark.django_db
def test_an_unclaimed_subject_type_is_ignored_silently(sink):
    erase = _Eraser()
    with override_settings(STAPEL_COMM=INPROCESS):
        reg = register_gdpr_owner("docs", ["document"], erase)
        reg.handle_erasure_requested(
            _request(
                correlation_id="c-1", subject_type="recording", subject_key="r-1"
            )
        )

    assert erase.calls == []
    assert SECTION_ERASED not in sink


@pytest.mark.django_db
@pytest.mark.parametrize(
    "payload",
    [
        {"subject_type": "document", "subject_key": "doc-9"},   # no correlation
        {"correlation_id": "c-1", "subject_key": "doc-9"},      # no type
        {"correlation_id": "c-1", "subject_type": "document"},  # no key
        {},
    ],
)
def test_a_malformed_payload_is_logged_and_dropped(sink, caplog, payload):
    erase = _Eraser()
    with override_settings(STAPEL_COMM=INPROCESS):
        reg = register_gdpr_owner("docs", ["document"], erase)
        reg.handle_erasure_requested(_request(**payload))

    assert erase.calls == []
    assert SECTION_ERASED not in sink
    assert "malformed" in caplog.text


@pytest.mark.django_db
def test_an_unusable_key_is_logged_and_never_receipted(sink, caplog):
    erase = _Eraser(raises=ValueError("badly formed hexadecimal UUID string"))
    with override_settings(STAPEL_COMM=INPROCESS):
        reg = register_gdpr_owner("docs", ["document"], erase)
        reg.handle_erasure_requested(
            _request(
                correlation_id="c-1", subject_type="document", subject_key="not-a-uuid"
            )
        )

    assert SECTION_ERASED not in sink
    assert "unusable" in caplog.text


@pytest.mark.django_db
def test_a_key_that_names_nothing_of_ours_receipts_nothing(sink):
    erase = _Eraser(counts=None)
    with override_settings(STAPEL_COMM=INPROCESS):
        reg = register_gdpr_owner("docs", ["document"], erase)
        reg.handle_erasure_requested(
            _request(correlation_id="c-1", subject_type="document", subject_key="x")
        )

    assert erase.calls == [("document", "x", None)]
    assert SECTION_ERASED not in sink


@pytest.mark.django_db
def test_an_unexpected_failure_propagates_for_redelivery(sink):
    erase = _Eraser(raises=RuntimeError("the database went away"))
    with override_settings(STAPEL_COMM=INPROCESS):
        reg = register_gdpr_owner("docs", ["document"], erase)
        with pytest.raises(RuntimeError):
            reg.handle_erasure_requested(
                _request(
                    correlation_id="c-1", subject_type="document", subject_key="x"
                )
            )
    assert SECTION_ERASED not in sink


@pytest.mark.django_db(transaction=True)
def test_the_receipt_rides_the_erase_transaction():
    """A rollback after the erase leaves no receipt in the outbox."""
    from django.db import transaction

    from stapel_core.django.outbox.models import OutboxEvent

    class _RollingBack:
        def __call__(self, subject_type, subject_key, workspace_id=None):
            return {"rows": 1}

    reg = register_gdpr_owner("docs", ["document"], _RollingBack())
    with pytest.raises(RuntimeError):
        with transaction.atomic():
            reg.handle_erasure_requested(
                _request(
                    correlation_id="c-1", subject_type="document", subject_key="x"
                )
            )
            assert OutboxEvent.objects.filter(topic=SECTION_ERASED).exists()
            raise RuntimeError("the caller's transaction failed after us")

    assert not OutboxEvent.objects.filter(topic=SECTION_ERASED).exists()


# ---------------------------------------------------------------------------
# gdpr.owner.probe
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_probe_is_answered_with_what_this_owner_really_erases(sink):
    with override_settings(STAPEL_COMM=INPROCESS):
        reg = register_gdpr_owner("media", ["account", "file"], _Eraser())
        reg.handle_owner_probe(
            Event(
                event_type=OWNER_PROBE,
                service="gdpr",
                payload={"correlation_id": "p-1"},
            )
        )

    assert sink[OWNER_ALIVE] == [
        {
            "owner": "media",
            "subject_types": ["account", "file"],
            "correlation_id": "p-1",
        }
    ]


@pytest.mark.django_db
def test_the_probe_answers_without_a_correlation_id(sink):
    with override_settings(STAPEL_COMM=INPROCESS):
        reg = register_gdpr_owner("media", ["file"], _Eraser())
        reg.handle_owner_probe(
            Event(event_type=OWNER_PROBE, service="gdpr", payload={})
        )

    assert sink[OWNER_ALIVE] == [{"owner": "media", "subject_types": ["file"]}]


@pytest.mark.django_db
def test_the_probe_reaches_the_owner_through_the_bus(sink):
    """The registered subscriber answers — not merely the returned callable."""
    with override_settings(STAPEL_COMM=INPROCESS):
        register_gdpr_owner("media", ["file"], _Eraser())
        emit(OWNER_PROBE, {"correlation_id": "p-2"})

    assert sink[OWNER_ALIVE][0]["owner"] == "media"


# ---------------------------------------------------------------------------
# user.deleted — the deprecated account signal
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_user_deleted_runs_the_same_erase_and_receipts(sink):
    erase = _Eraser({"wallets": 1})
    with override_settings(STAPEL_COMM=INPROCESS):
        reg = register_gdpr_owner("billing", ["account"], erase)
        reg.handle_user_deleted(
            Event(
                event_type="user.deleted",
                service="gdpr",
                payload={"user_id": 42, "correlation_id": "c-9"},
            )
        )

    assert erase.calls == [("account", "42", None)]
    (receipt,) = sink[SECTION_ERASED]
    assert receipt["receipt_id"] == "billing:account:42:c-9"
    assert receipt["user_id"] == "42"


@pytest.mark.django_db
def test_user_deleted_without_a_correlation_id_erases_and_stays_quiet(sink):
    erase = _Eraser()
    with override_settings(STAPEL_COMM=INPROCESS):
        reg = register_gdpr_owner("billing", ["account"], erase)
        reg.handle_user_deleted(
            Event(event_type="user.deleted", service="gdpr", payload={"user_id": 42})
        )

    assert erase.calls == [("account", "42", None)]
    assert SECTION_ERASED not in sink


@pytest.mark.django_db
def test_user_deleted_without_a_user_id_is_logged_and_dropped(sink, caplog):
    erase = _Eraser()
    with override_settings(STAPEL_COMM=INPROCESS):
        reg = register_gdpr_owner("billing", ["account"], erase)
        reg.handle_user_deleted(
            Event(event_type="user.deleted", service="gdpr", payload={})
        )

    assert erase.calls == []
    assert "without user_id" in caplog.text


@pytest.mark.django_db
def test_user_deleted_with_an_unusable_key_is_logged_not_retried(sink, caplog):
    erase = _Eraser(raises=TypeError("int() argument must be a string"))
    with override_settings(STAPEL_COMM=INPROCESS):
        reg = register_gdpr_owner("billing", ["account"], erase)
        reg.handle_user_deleted(
            Event(
                event_type="user.deleted",
                service="gdpr",
                payload={"user_id": "x", "correlation_id": "c-1"},
            )
        )

    assert SECTION_ERASED not in sink
    assert "unusable" in caplog.text


# ---------------------------------------------------------------------------
# two owners in one process (the monolith case)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_each_owner_answers_only_for_the_subjects_it_claims(sink):
    docs, media = _Eraser({"documents": 1}), _Eraser({"files": 5})
    with override_settings(STAPEL_COMM=INPROCESS):
        register_gdpr_owner("docs", ["document"], docs)
        register_gdpr_owner("media", ["file"], media)
        emit(
            ERASURE_REQUESTED,
            {"correlation_id": "c-1", "subject_type": "file", "subject_key": "f-1"},
        )

    assert docs.calls == []
    assert media.calls == [("file", "f-1", None)]
    assert [r["owner"] for r in sink[SECTION_ERASED]] == ["media"]


# ---------------------------------------------------------------------------
# pseudonymize — the one keyed-HMAC funnel
# ---------------------------------------------------------------------------


def test_pseudonymize_is_stable_prefixed_and_not_the_id():
    first = pseudonymize("42")
    assert first.startswith("erased:")
    assert first == pseudonymize(42)  # str()-ed, so an int id is the same subject
    assert "42" not in first[len("erased:"):]
    assert len(first) == len("erased:") + 32


def test_pseudonymize_is_idempotent():
    once = pseudonymize("42")
    assert pseudonymize(once) == once


def test_pseudonymize_separates_subjects():
    assert pseudonymize("42") != pseudonymize("43")


def test_pseudonymize_is_keyed_by_the_secret_key():
    with override_settings(SECRET_KEY="one"):
        first = pseudonymize("42")
    with override_settings(SECRET_KEY="two"):
        second = pseudonymize("42")
    assert first != second


def test_pseudonymize_honours_a_custom_prefix():
    value = pseudonymize("42", prefix="gone:")
    assert value.startswith("gone:")
    assert pseudonymize(value, prefix="gone:") == value
