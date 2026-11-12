"""Core declares the facts it emits, as their owner, and emit() enforces them.

Core ships ``gdpr/schemas/emits/`` and ``notifications/schemas/emits/`` but is
not an app with a ``schemas/`` directory, so the per-app autoload never read
them: core's receipts were validated only where a consumer happened to
register a copy, and not at all once consumer schemas became per-subscription.
"""
import ast
import json
from pathlib import Path

import pytest
from django.test import override_settings

import stapel_core
from stapel_core.bus.event import Event
from stapel_core.comm import action_registry, emit
from stapel_core.comm import schemas as schemas_mod
from stapel_core.comm.exceptions import SchemaValidationError
from stapel_core.comm.schema_ownership import (
    CORE_OWNED_ACTIONS,
    check_emit_schema_conflicts,
)
from stapel_core.comm.schemas import (
    CORE_OWNER,
    core_emit_schema_files,
    register_core_emit_schemas,
)
from stapel_core.gdpr import (
    ERASURE_REQUESTED,
    OWNER_ALIVE,
    OWNER_PROBE,
    SECTION_ERASED,
    register_gdpr_owner,
)
from stapel_core.gdpr.owners import _reset_gdpr_owners
from stapel_core.testing import assert_module_registers_only_own_emit_schemas

INPROCESS = {"OUTBOX_ENABLED": False, "ACTION_TRANSPORT": "inprocess"}
CORE_FACTS = {"gdpr.owner.alive", "gdpr.section.erased", "notification.requested"}


@pytest.fixture(autouse=True)
def clean():
    action_registry.clear()
    schemas_mod.reset_autoload()
    _reset_gdpr_owners()
    yield
    action_registry.clear()
    schemas_mod.reset_autoload()
    _reset_gdpr_owners()


@pytest.fixture
def sink():
    seen: dict[str, list[dict]] = {}
    for name in (SECTION_ERASED, OWNER_ALIVE):
        action_registry.subscribe(
            name, lambda event, n=name: seen.setdefault(n, []).append(event.payload)
        )
    return seen


class _Eraser:
    def __init__(self, counts=None):
        self.calls = []
        self.counts = {"rows": 2} if counts is None else counts

    def __call__(self, subject_type, subject_key, workspace_id=None):
        self.calls.append((subject_type, subject_key, workspace_id))
        return self.counts


def test_every_shipped_emit_schema_is_found():
    assert set(core_emit_schema_files()) == CORE_FACTS
    assert set(CORE_OWNED_ACTIONS) == CORE_FACTS


def test_core_declares_each_of_them_as_owner():
    assert register_core_emit_schemas() == len(CORE_FACTS)
    declarations = action_registry.emit_declarations()
    for fact, path in core_emit_schema_files().items():
        assert declarations[fact] == {CORE_OWNER: json.loads(path.read_text())}
    assert register_core_emit_schemas() == len(CORE_FACTS)  # idempotent
    assert check_emit_schema_conflicts() == []


def test_the_autoload_declares_core_first_whatever_the_app_order(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from django.apps import apps

    copy = tmp_path / "fake_lib" / "schemas" / "emits"
    copy.mkdir(parents=True)
    (copy / f"{SECTION_ERASED}.json").write_text(json.dumps({"type": "object"}))
    lib = SimpleNamespace(path=str(tmp_path / "fake_lib"), name="fake_lib", label="fake_lib")
    monkeypatch.setattr(apps, "get_app_configs", lambda: [lib])
    schemas_mod.autoload_schemas()

    assert list(action_registry.emit_declarations()[SECTION_ERASED]) == [CORE_OWNER, "fake_lib"]
    shipped = json.loads(core_emit_schema_files()[SECTION_ERASED].read_text())
    assert action_registry._schemas[SECTION_ERASED] == shipped
    (problem,) = [p for p in check_emit_schema_conflicts() if p.obj == SECTION_ERASED]
    assert problem.id == "stapel_core.comm.E011"


def test_core_app_ready_declares_them():
    """CommonDjangoConfig.ready() is the declaration every service runs."""
    import inspect

    from stapel_core.django.apps import CommonDjangoConfig

    assert "register_core_emit_schemas()" in inspect.getsource(CommonDjangoConfig.ready)


@pytest.mark.parametrize("fact, payload", [
    (SECTION_ERASED, {"correlation_id": "c-1", "unexpected": 1}),
    (SECTION_ERASED, {"owner": "docs"}),
    (OWNER_ALIVE, {"owner": "docs", "subject_types": "account"}),
    ("notification.requested", {"user_id": "u-1"}),
])
def test_emit_now_refuses_a_payload_core_s_own_schema_refuses(fact, payload):
    register_core_emit_schemas()
    with override_settings(STAPEL_COMM=INPROCESS), pytest.raises(SchemaValidationError):
        emit(fact, payload)


# ---------------------------------------------------------------------------
# Every payload core actually emits passes its own schema
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("workspace_id", ["", None, "ws-1", 12])
def test_the_request_shapes_stapel_gdpr_sends_are_erased(sink, workspace_id):
    """stapel-gdpr sends ``workspace_id: ""`` for an unscoped erasure."""
    register_core_emit_schemas()
    erase = _Eraser()
    with override_settings(STAPEL_COMM=INPROCESS):
        reg = register_gdpr_owner("docs", ["account"], erase)
        reg.handle_erasure_requested(Event(
            event_type=ERASURE_REQUESTED, service="gdpr",
            payload={"request_id": 1, "correlation_id": "c-1", "subject_type": "account",
                     "subject_key": "u-1", "workspace_id": workspace_id},
        ))
    assert erase.calls == [("account", "u-1", workspace_id)]
    assert len(sink[SECTION_ERASED]) == 1


@pytest.mark.django_db
@pytest.mark.parametrize("correlation_id, subject_key", [("c-1", "doc-9"), (7, 9)])
def test_erasure_receipt_passes_its_schema(sink, correlation_id, subject_key):
    register_core_emit_schemas()
    erase = _Eraser({"documents": 3, "nested": {"a": 1}})
    with override_settings(STAPEL_COMM=INPROCESS):
        reg = register_gdpr_owner("docs", ["document"], erase)
        reg.handle_erasure_requested(Event(
            event_type=ERASURE_REQUESTED, service="gdpr",
            payload={"correlation_id": correlation_id, "subject_type": "document",
                     "subject_key": subject_key, "workspace_id": "ws-1",
                     "request_id": 5, "origin": "user"},
        ))
    (receipt,) = sink[SECTION_ERASED]
    assert receipt["subject_key"] == str(subject_key)


@pytest.mark.django_db
def test_legacy_user_deleted_receipt_passes_its_schema(sink):
    register_core_emit_schemas()
    with override_settings(STAPEL_COMM=INPROCESS):
        reg = register_gdpr_owner("billing", ["account"], _Eraser({"wallets": 1}))
        reg.handle_user_deleted(Event(
            event_type="user.deleted", service="gdpr",
            payload={"user_id": 42, "correlation_id": "c-9"},
        ))
    assert sink[SECTION_ERASED][0]["user_id"] == "42"


@pytest.mark.django_db
@pytest.mark.parametrize("payload", [{"correlation_id": "p-1"}, {}, {"correlation_id": 3}])
def test_probe_answer_passes_its_schema(sink, payload):
    register_core_emit_schemas()
    with override_settings(STAPEL_COMM=INPROCESS):
        register_gdpr_owner("media", ["account", "file"], _Eraser())
        emit(OWNER_PROBE, payload)
    (alive,) = sink[OWNER_ALIVE]
    assert alive["owner"] == "media"


def test_notification_request_payload_passes_its_schema(monkeypatch):
    """request_notification publishes on the bus; its payload is the fact."""
    from stapel_core.comm.registry import action_registry as registry
    from stapel_core.notifications import publish as publish_mod

    register_core_emit_schemas()
    sent = []
    monkeypatch.setattr(publish_mod, "publish", lambda topic, event: sent.append(event))
    assert publish_mod.request_notification(
        "otp_code", user_id="u-1", variables={"code": "1"}, dedup_key="k", dedup_ttl=60,
        content_text="hi",
    )
    registry.validate("notification.requested", sent[0].payload)


# ---------------------------------------------------------------------------
# Core consumes stapel-gdpr's facts without copies of their schemas
# ---------------------------------------------------------------------------


def test_core_registers_only_its_own_emit_schemas():
    from stapel_core.gdpr import provider_bridge

    register_core_emit_schemas()
    register_gdpr_owner("docs", ["document"], _Eraser())
    monkey_subscribed = provider_bridge._subscribed
    provider_bridge._subscribed = False
    try:
        provider_bridge.register_provider_bridge()
        assert_module_registers_only_own_emit_schemas("stapel_core")
        assert action_registry.consumer_schemas().get(ERASURE_REQUESTED, []) == []
        assert action_registry.consumer_schemas().get(OWNER_PROBE, []) == []
    finally:
        provider_bridge._subscribed = monkey_subscribed


@pytest.mark.django_db
@pytest.mark.parametrize("payload", [
    {"correlation_id": {"x": 1}, "subject_type": "document", "subject_key": "d"},
    {"correlation_id": "c", "subject_type": ["document"], "subject_key": "d"},
    {"correlation_id": "c", "subject_type": "document", "subject_key": {"id": 1}},
    {"correlation_id": True, "subject_type": "document", "subject_key": "d"},
    {"correlation_id": "c", "subject_type": "document", "subject_key": "d",
     "workspace_id": {"id": 1}},
])
def test_a_mistyped_request_is_dropped_by_the_handler(sink, caplog, payload):
    """What the loose consumer schema used to refuse, the handler refuses."""
    erase = _Eraser()
    with override_settings(STAPEL_COMM=INPROCESS):
        reg = register_gdpr_owner("docs", ["document"], erase)
        reg.handle_erasure_requested(
            Event(event_type=ERASURE_REQUESTED, service="gdpr", payload=payload)
        )
    assert erase.calls == []
    assert SECTION_ERASED not in sink
    assert "malformed" in caplog.text


def _core_emitted_fact_constants() -> set[str]:
    """Names passed as the first argument of emit() in core's gdpr and
    notifications packages, resolved through module constants."""
    root = Path(stapel_core.__file__).resolve().parent
    names = set()
    for package in ("gdpr", "notifications"):
        for path in (root / package).rglob("*.py"):
            tree = ast.parse(path.read_text())
            constants = {
                t.id: node.value.value
                for node in ast.walk(tree) if isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
                for t in node.targets if isinstance(t, ast.Name)
            }
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "emit"
                        and node.args):
                    arg = node.args[0]
                    if isinstance(arg, ast.Constant):
                        names.add(arg.value)
                    elif isinstance(arg, ast.Name):
                        from stapel_core.gdpr import owners

                        names.add(constants.get(arg.id) or getattr(owners, arg.id))
    return names


def test_every_fact_core_s_gdpr_layer_emits_ships_a_schema():
    assert _core_emitted_fact_constants() == {SECTION_ERASED, OWNER_ALIVE}
    assert _core_emitted_fact_constants() <= set(core_emit_schema_files())
