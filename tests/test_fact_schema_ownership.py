"""A fact's schema belongs to the module that emits it.

Two fake modules over one fact name: ``fake_payments`` ships
``schemas/emits/pay.done.json`` and emits ``pay.done``; ``fake_shop`` consumes
it with ``on_action("pay.done", schema=...)`` carrying its own, different idea
of the shape. The emitter's valid payload must be accepted whatever the order
in which the two modules come up.
"""
import json
from types import SimpleNamespace

import pytest
from django.test import override_settings

from stapel_core.comm import action_registry, emit, on_action
from stapel_core.comm import schemas as schemas_mod
from stapel_core.comm.exceptions import SchemaValidationError

FACT = "pay.done"

#: The owner's contract: a credit purchase.
OWNER_SCHEMA = {
    "type": "object",
    "required": ["user_id", "amount_cents"],
    "properties": {
        "user_id": {"type": "string"},
        "amount_cents": {"type": "integer"},
        "reference_type": {"type": "string"},
    },
}

#: A consumer's private copy with another shape (the defect).
CONSUMER_SCHEMA = {
    "type": "object",
    "required": ["reference_type", "reference_key"],
    "properties": {"reference_type": {"type": "string"}},
}

OWNER_PAYLOAD = {"user_id": "u-1", "amount_cents": 990}

NO_OUTBOX = {"OUTBOX_ENABLED": False}


@pytest.fixture(autouse=True)
def clean():
    schemas_mod.reset_autoload()
    action_registry.clear()
    yield
    schemas_mod.reset_autoload()
    action_registry.clear()


def _module(tmp_path, name, emits=None):
    root = tmp_path / name
    (root / "schemas" / "emits").mkdir(parents=True)
    for fact, schema in (emits or {}).items():
        (root / "schemas" / "emits" / f"{fact}.json").write_text(json.dumps(schema))
    return SimpleNamespace(path=str(root), name=name, label=name)


def _load_emitters(monkeypatch, *modules):
    from django.apps import apps

    monkeypatch.setattr(apps, "get_app_configs", lambda: list(modules))
    schemas_mod.autoload_schemas()


def _subscribe_consumer(seen):
    @on_action(FACT, schema=CONSUMER_SCHEMA)
    def shop_handler(event):
        seen.append(event.payload)

    return shop_handler


@pytest.mark.parametrize("order", ["emitter_first", "consumer_first"])
def test_a_consumer_schema_never_refuses_the_owners_emit(tmp_path, monkeypatch, order):
    payments = _module(tmp_path, "fake_payments", {FACT: OWNER_SCHEMA})
    seen: list = []
    if order == "emitter_first":
        _load_emitters(monkeypatch, payments)
        _subscribe_consumer(seen)
    else:
        _subscribe_consumer(seen)
        _load_emitters(monkeypatch, payments)

    with override_settings(STAPEL_COMM=NO_OUTBOX):
        emit(FACT, OWNER_PAYLOAD)

    assert action_registry._schemas[FACT] == OWNER_SCHEMA


@pytest.mark.parametrize("order", ["emitter_first", "consumer_first"])
def test_the_owner_schema_still_refuses_an_invalid_emit(tmp_path, monkeypatch, order):
    payments = _module(tmp_path, "fake_payments", {FACT: OWNER_SCHEMA})
    if order == "emitter_first":
        _load_emitters(monkeypatch, payments)
        _subscribe_consumer([])
    else:
        _subscribe_consumer([])
        _load_emitters(monkeypatch, payments)

    with override_settings(STAPEL_COMM=NO_OUTBOX):
        with pytest.raises(SchemaValidationError, match="'user_id' is a required"):
            emit(FACT, {"amount_cents": 1})


# ---------------------------------------------------------------------------
# The consumer's schema guards the consumer
# ---------------------------------------------------------------------------


def test_a_stricter_consumer_schema_filters_that_consumer_only(tmp_path, monkeypatch, caplog):
    _load_emitters(monkeypatch, _module(tmp_path, "fake_payments", {FACT: OWNER_SCHEMA}))
    shop: list = []
    _subscribe_consumer(shop)
    loyal: list = []
    on_action(FACT)(loyal.append)

    with override_settings(STAPEL_COMM=NO_OUTBOX):
        emit(FACT, OWNER_PAYLOAD)

    assert shop == []
    assert [event.payload for event in loyal] == [OWNER_PAYLOAD]
    parked = [r for r in caplog.records if "refused the payload as unprocessable" in r.getMessage()]
    assert len(parked) == 1
    assert "shop_handler" in parked[0].getMessage()
    assert 'reason="consumer_schema"' in parked[0].getMessage()


def test_a_consumer_schema_still_lets_through_what_it_accepts(tmp_path, monkeypatch):
    _load_emitters(monkeypatch, _module(tmp_path, "fake_payments", {FACT: OWNER_SCHEMA}))
    shop: list = []
    _subscribe_consumer(shop)
    payload = {**OWNER_PAYLOAD, "reference_type": "order", "reference_key": "7"}

    with override_settings(STAPEL_COMM=NO_OUTBOX):
        emit(FACT, payload)

    assert shop == [payload]


def test_the_raise_policy_reports_a_consumer_refusal_as_that_handlers_failure(
    tmp_path, monkeypatch
):
    from stapel_core.comm.actions import deliver_to_subscribers
    from stapel_core.bus.event import Event

    _load_emitters(monkeypatch, _module(tmp_path, "fake_payments", {FACT: OWNER_SCHEMA}))
    handler = _subscribe_consumer([])
    event = Event(event_type=FACT, service="t", payload=OWNER_PAYLOAD)

    with override_settings(STAPEL_COMM={"UNPROCESSABLE_PAYLOAD": "raise"}):
        errors = deliver_to_subscribers(event, [handler])

    assert len(errors) == 1
    assert isinstance(errors[0], SchemaValidationError)


def test_a_consumer_schema_is_not_checked_with_validation_off(tmp_path, monkeypatch):
    _load_emitters(monkeypatch, _module(tmp_path, "fake_payments", {FACT: OWNER_SCHEMA}))
    shop: list = []
    _subscribe_consumer(shop)

    with override_settings(STAPEL_COMM={**NO_OUTBOX, "VALIDATE_SCHEMAS": False}):
        emit(FACT, OWNER_PAYLOAD)

    assert shop == [OWNER_PAYLOAD]


def test_without_an_owner_schema_a_consumer_schema_validates_nothing_at_the_emit():
    shop: list = []
    _subscribe_consumer(shop)

    with override_settings(STAPEL_COMM=NO_OUTBOX):
        emit(FACT, {"anything": 1})

    assert FACT not in action_registry._schemas
    assert shop == []


# ---------------------------------------------------------------------------
# Two declared emitters: a conflict, not last-writer-wins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("first", ["fake_payments", "fake_shop"])
def test_two_emitters_with_different_schemas_are_an_error_naming_both(
    tmp_path, monkeypatch, first
):
    from stapel_core.comm.schema_ownership import E011, check_emit_schema_conflicts

    payments = _module(tmp_path, "fake_payments", {FACT: OWNER_SCHEMA})
    shop = _module(tmp_path, "fake_shop", {FACT: CONSUMER_SCHEMA})
    modules = [payments, shop] if first == "fake_payments" else [shop, payments]
    _load_emitters(monkeypatch, *modules)

    problems = check_emit_schema_conflicts()

    assert [p.id for p in problems] == [E011]
    assert "fake_payments and fake_shop" in problems[0].msg
    assert problems[0].obj == FACT
    # The first declaration stays in force; the second never replaced it.
    assert action_registry._schemas[FACT] == (
        OWNER_SCHEMA if first == "fake_payments" else CONSUMER_SCHEMA
    )


def test_two_emitters_with_the_same_validating_shape_are_a_warning(tmp_path, monkeypatch):
    from stapel_core.comm.schema_ownership import W011, check_emit_schema_conflicts

    reworded = {**OWNER_SCHEMA, "description": "a copy with its own prose"}
    _load_emitters(
        monkeypatch,
        _module(tmp_path, "fake_payments", {FACT: OWNER_SCHEMA}),
        _module(tmp_path, "fake_shop", {FACT: reworded}),
    )

    assert [p.id for p in check_emit_schema_conflicts()] == [W011]


def test_a_consumer_schema_is_never_an_emitter_conflict(tmp_path, monkeypatch):
    from stapel_core.comm.schema_ownership import check_emit_schema_conflicts

    _load_emitters(monkeypatch, _module(tmp_path, "fake_payments", {FACT: OWNER_SCHEMA}))
    _subscribe_consumer([])

    assert check_emit_schema_conflicts() == []
    assert list(action_registry.emit_declarations()[FACT]) == ["fake_payments"]


def test_one_owner_redeclaring_replaces_its_own_schema():
    action_registry.register_schema(FACT, CONSUMER_SCHEMA, owner="fake_payments")
    action_registry.register_schema(FACT, OWNER_SCHEMA, owner="fake_payments")

    assert action_registry._schemas[FACT] == OWNER_SCHEMA
    assert action_registry.emit_declarations() == {FACT: {"fake_payments": OWNER_SCHEMA}}


def test_an_explicit_declaration_is_attributed_to_the_calling_package():
    action_registry.register_schema(FACT, OWNER_SCHEMA)

    assert list(action_registry.emit_declarations()[FACT]) == [__name__.split(".")[0]]


def test_the_check_is_registered_for_manage_py_check(monkeypatch):
    """stapel_core.django's ready() registers it, so every service runs it."""
    import importlib

    import django.core.checks

    from stapel_core.comm.schema_ownership import check_emit_schema_conflicts
    from stapel_core.django.apps import CommonDjangoConfig

    registered = []
    real_register = django.core.checks.register

    def _record(check=None, *tags, **kwargs):
        if callable(check):
            registered.append(check)
        return real_register(check, *tags, **kwargs)

    monkeypatch.setattr(django.core.checks, "register", _record)
    CommonDjangoConfig(
        "stapel_core.django", importlib.import_module("stapel_core.django")
    ).ready()

    assert check_emit_schema_conflicts in registered


# ---------------------------------------------------------------------------
# The reusable gate for bricks
# ---------------------------------------------------------------------------


def _fake_package(tmp_path, monkeypatch, name, emits):
    root = tmp_path / "site" / name
    (root / "schemas" / "emits").mkdir(parents=True)
    (root / "__init__.py").write_text("")
    for fact, schema in emits.items():
        (root / "schemas" / "emits" / f"{fact}.json").write_text(json.dumps(schema))
    monkeypatch.syspath_prepend(str(tmp_path / "site"))
    return root


def _handler_from(package):
    def handler(event):
        return None

    handler.__module__ = f"{package}.actions"
    return handler


def test_the_gate_refuses_a_consumer_copy_of_a_foreign_fact(tmp_path, monkeypatch):
    from stapel_core.testing import assert_module_registers_only_own_emit_schemas

    _fake_package(tmp_path, monkeypatch, "fake_shop_gate", {"shop.order.placed": {}})
    action_registry.subscribe(FACT, _handler_from("fake_shop_gate"), schema=CONSUMER_SCHEMA)

    with pytest.raises(AssertionError, match="fake_shop_gate subscribes to 'pay.done' with schema="):
        assert_module_registers_only_own_emit_schemas("fake_shop_gate")


def test_the_gate_refuses_a_fact_declared_by_two_packages(tmp_path, monkeypatch):
    from stapel_core.testing import assert_module_registers_only_own_emit_schemas

    _fake_package(tmp_path, monkeypatch, "fake_shop_gate2", {FACT: CONSUMER_SCHEMA})
    action_registry.register_schema(FACT, OWNER_SCHEMA, owner="fake_payments")
    action_registry.register_schema(FACT, CONSUMER_SCHEMA, owner="fake_shop_gate2")

    with pytest.raises(AssertionError, match="by fake_shop_gate2 and by fake_payments"):
        assert_module_registers_only_own_emit_schemas("fake_shop_gate2")


def test_the_gate_passes_a_well_behaved_module(tmp_path, monkeypatch):
    from stapel_core.testing import assert_module_registers_only_own_emit_schemas

    _fake_package(tmp_path, monkeypatch, "fake_shop_gate3", {"shop.paid": OWNER_SCHEMA})
    action_registry.register_schema("shop.paid", OWNER_SCHEMA, owner="fake_shop_gate3")
    action_registry.register_schema(FACT, OWNER_SCHEMA, owner="fake_payments")
    action_registry.subscribe(FACT, _handler_from("fake_shop_gate3"))
    action_registry.subscribe("shop.paid", _handler_from("fake_shop_gate3"), schema=OWNER_SCHEMA)
    action_registry.subscribe(FACT, _handler_from("someone_else"), schema=CONSUMER_SCHEMA)

    assert_module_registers_only_own_emit_schemas("fake_shop_gate3")


def test_a_hand_built_event_without_a_name_still_reaches_its_handlers():
    """deliver_to_subscribers() takes any object with a payload; libraries'
    suites build one by hand. The per-subscription check must not need a
    name it cannot use."""
    from stapel_core.comm.actions import deliver_to_subscribers

    seen = []
    action_registry.subscribe(FACT, lambda event: seen.append(event.payload))
    action_registry.subscribe(FACT, lambda event: seen.append("guarded"), schema=CONSUMER_SCHEMA)
    event = SimpleNamespace(payload=dict(OWNER_PAYLOAD))

    assert deliver_to_subscribers(event, action_registry.handlers(FACT)) == []
    assert seen == [OWNER_PAYLOAD, "guarded"]
