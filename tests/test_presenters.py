"""Presenter primitive (§55 slice 1): DAO→DTO, schema generation, config-swap.

Covers stapel_core.django.api.presenters (Presenter, PresenterField),
stapel_core.django.swappable (get_model/get_presenter indirection) and the
users pilot (stapel_core.django.users.presenters).
"""
import uuid

import pytest
from django.db import models
from django.test import override_settings

from stapel_core.django.api.presenters import (
    Presenter,
    PresenterField,
    _infer_type,
    _parse_presenter_docstring,
)
from stapel_core.django.outbox.models import OutboxEvent
from stapel_core.django.swappable import clear_swap_cache, get_model, get_presenter
from stapel_core.django.users.models import User
from stapel_core.django.users.presenters import (
    PRESENTER_KEY,
    UserProfilePresenter,
    get_user_profile_presenter,
)


@pytest.fixture(autouse=True)
def _reset_swap_cache():
    clear_swap_cache()
    yield
    clear_swap_cache()


# ---------------------------------------------------------------------------
# Presenter core mechanics
# ---------------------------------------------------------------------------


class OutboxPresenter(Presenter):
    """Presents an OutboxEvent row.

    Example:
        {
            "topic": "orders.created",
            "attempts": 0,
            "is_pending": true
        }
    """

    model = OutboxEvent
    fields = ("topic", "attempts")
    custom_fields = {
        "is_pending": PresenterField(
            type=bool,
            source=lambda dao: dao.dispatched_at is None,
            help_text="True while the event has not yet been delivered.",
        ),
    }


class TestBuildDto:
    def test_as_is_field_types_from_model(self):
        field_types = {f.name: f.type for f in __import__("dataclasses").fields(OutboxPresenter.dto)}
        assert field_types["topic"] is str
        assert field_types["attempts"] is int

    def test_as_is_field_help_text_from_model(self):
        import dataclasses

        by_name = {f.name: f for f in dataclasses.fields(OutboxPresenter.dto)}
        # No explicit help_text on the model field -> falls back to
        # Django's auto-generated verbose_name ("attempts").
        assert by_name["attempts"].metadata.get("help_text") == "attempts"

    def test_custom_field_help_text_from_presenter_field(self):
        import dataclasses

        by_name = {f.name: f for f in dataclasses.fields(OutboxPresenter.dto)}
        assert (
            by_name["is_pending"].metadata["help_text"]
            == "True while the event has not yet been delivered."
        )

    def test_dto_docstring_is_description_before_example(self):
        assert OutboxPresenter.dto.__doc__ == "Presents an OutboxEvent row."

    def test_unknown_field_raises(self):
        with pytest.raises(TypeError):
            class Bad(Presenter):
                model = OutboxEvent
                fields = ("nope_not_a_field",)

    def test_field_ordering_default_after_required(self):
        import dataclasses

        class WithDefault(Presenter):
            model = OutboxEvent
            fields = ("topic",)
            custom_fields = {
                "flag": PresenterField(type=bool, default=False),
            }

        names = [f.name for f in dataclasses.fields(WithDefault.dto)]
        assert names.index("topic") < names.index("flag")
        instance = WithDefault.dto(topic="x")
        assert instance.flag is False

    def test_abstract_intermediate_subclass_has_no_dto(self):
        class Abstract(Presenter):
            """No model declared yet — a library-internal base class."""

        assert not hasattr(Abstract, "dto")

        class Concrete(Abstract):
            model = OutboxEvent
            fields = ("topic",)

        assert Concrete.dto is not None

    def test_custom_field_example_metadata(self):
        import dataclasses

        class WithExample(Presenter):
            model = OutboxEvent
            fields = ("topic",)
            custom_fields = {
                "flag": PresenterField(type=bool, default=False, example=True),
            }

        by_name = {f.name: f for f in dataclasses.fields(WithExample.dto)}
        assert by_name["flag"].metadata["example"] is True


class TestTypeInference:
    def test_foreign_key_resolves_target_field_type(self):
        fk = models.ForeignKey(OutboxEvent, on_delete=models.CASCADE)
        assert _infer_type(fk) is int  # OutboxEvent's pk is a BigAutoField

    def test_unmapped_field_type_falls_back_to_any(self):
        from typing import Any

        assert _infer_type(models.GenericIPAddressField()) is Any


class TestDocstringExampleParsing:
    def test_invalid_json_example_yields_none(self):
        description, example = _parse_presenter_docstring(
            "A presenter.\n\nExample:\n    not valid json\n"
        )
        assert description == "A presenter."
        assert example is None

    def test_empty_docstring(self):
        assert _parse_presenter_docstring("") == ("", None)


@pytest.mark.django_db
class TestPresent:
    def test_present_as_is_and_custom_fields(self):
        event = OutboxEvent.objects.create(topic="orders.created", event_json="{}")
        dto = OutboxPresenter.present(event)
        assert dto.topic == "orders.created"
        assert dto.attempts == 0
        assert dto.is_pending is True

    def test_present_many(self):
        OutboxEvent.objects.create(topic="a", event_json="{}")
        OutboxEvent.objects.create(topic="b", event_json="{}")
        dtos = OutboxPresenter.present_many(OutboxEvent.objects.order_by("topic"))
        assert [d.topic for d in dtos] == ["a", "b"]


# ---------------------------------------------------------------------------
# Nested presenter reference (custom field type = another Presenter)
# ---------------------------------------------------------------------------


class LeafPresenter(Presenter):
    """A minimal nested presenter.

    Example:
        {"topic": "orders.created"}
    """

    model = OutboxEvent
    fields = ("topic",)


class ParentPresenter(Presenter):
    """Presents an OutboxEvent alongside itself, nested (test-only shape).

    Example:
        {"topic": "orders.created", "self_as_leaf": {"topic": "orders.created"}}
    """

    model = OutboxEvent
    fields = ("topic",)
    custom_fields = {
        "self_as_leaf": PresenterField(
            type=LeafPresenter, source=lambda dao: dao, help_text="Nested self view.",
        ),
    }


class ParentManyPresenter(Presenter):
    """Presents a list of nested leaves (test-only shape).

    Example:
        {"topic": "orders.created", "siblings": [{"topic": "a"}]}
    """

    model = OutboxEvent
    fields = ("topic",)
    custom_fields = {
        "siblings": PresenterField(
            type=LeafPresenter, many=True, source=lambda dao: [dao],
            help_text="Nested list of leaves.",
        ),
    }


class TestNestedPresenter:
    def test_dto_field_type_is_nested_dto(self):
        import dataclasses

        by_name = {f.name: f for f in dataclasses.fields(ParentPresenter.dto)}
        assert by_name["self_as_leaf"].type is LeafPresenter.dto

    @pytest.mark.django_db
    def test_present_recurses_single(self):
        event = OutboxEvent.objects.create(topic="orders.created", event_json="{}")
        dto = ParentPresenter.present(event)
        assert dto.topic == "orders.created"
        assert isinstance(dto.self_as_leaf, LeafPresenter.dto)
        assert dto.self_as_leaf.topic == "orders.created"

    @pytest.mark.django_db
    def test_present_recurses_many(self):
        event = OutboxEvent.objects.create(topic="orders.created", event_json="{}")
        dto = ParentManyPresenter.present(event)
        assert len(dto.siblings) == 1
        assert dto.siblings[0].topic == "orders.created"


# ---------------------------------------------------------------------------
# Schema generation (§2): StapelDataclassSerializer + docstring example
# ---------------------------------------------------------------------------


class TestSerializerClass:
    def test_serializer_class_is_cached(self):
        assert OutboxPresenter.serializer_class() is OutboxPresenter.serializer_class()

    def test_serializer_fields_carry_help_text(self):
        ser = OutboxPresenter.serializer_class()()
        assert ser.fields["attempts"].help_text == "attempts"
        assert (
            ser.fields["is_pending"].help_text
            == "True while the event has not yet been delivered."
        )

    def test_docstring_example_attached_to_serializer(self):
        ser_cls = OutboxPresenter.serializer_class()
        examples = ser_cls._spectacular_annotation["examples"]
        assert examples[0].value == {
            "topic": "orders.created",
            "attempts": 0,
            "is_pending": True,
        }

    def test_no_example_section_means_no_examples_annotation(self):
        class NoExamplePresenter(Presenter):
            """Presents an OutboxEvent row, no example block."""

            model = OutboxEvent
            fields = ("topic",)

        annotation = getattr(NoExamplePresenter.serializer_class(), "_spectacular_annotation", None)
        assert not (annotation or {}).get("examples")


# ---------------------------------------------------------------------------
# Swappable indirection (§3): get_model / get_presenter
# ---------------------------------------------------------------------------


class _DefaultThing:
    pass


class _HostThing:
    pass


class TestSwappableIndirection:
    def test_get_model_default_when_unset(self):
        cls = get_model("TEST_THING", default=f"{__name__}._DefaultThing")
        assert cls is _DefaultThing

    def test_get_model_override_via_stapel_swap(self):
        with override_settings(STAPEL_SWAP={"TEST_THING": f"{__name__}._HostThing"}):
            clear_swap_cache()
            cls = get_model("TEST_THING", default=f"{__name__}._DefaultThing")
        assert cls is _HostThing

    def test_get_presenter_default_and_override(self):
        with override_settings(STAPEL_SWAP={}):
            clear_swap_cache()
            assert get_presenter("TEST_PRESENTER", default=f"{__name__}.OutboxPresenter") is OutboxPresenter
        with override_settings(STAPEL_SWAP={"TEST_PRESENTER": f"{__name__}.LeafPresenter"}):
            clear_swap_cache()
            assert get_presenter("TEST_PRESENTER", default=f"{__name__}.OutboxPresenter") is LeafPresenter

    def test_resolution_is_cached(self):
        get_model("TEST_THING", default=f"{__name__}._DefaultThing")
        # A second call with a *different* default must still return the
        # cached value — resolution happens once per key.
        cls = get_model("TEST_THING", default=f"{__name__}._HostThing")
        assert cls is _DefaultThing

    def test_override_settings_signal_clears_cache_without_manual_call(self):
        # get_model() is resolved once outside the override...
        get_model("TEST_THING", default=f"{__name__}._DefaultThing")
        # ...override_settings fires django's setting_changed signal on both
        # entry and exit, which our _connect_reload() listens to — no manual
        # clear_swap_cache() needed inside the block.
        with override_settings(STAPEL_SWAP={"TEST_THING": f"{__name__}._HostThing"}):
            assert get_model("TEST_THING", default=f"{__name__}._DefaultThing") is _HostThing
        assert get_model("TEST_THING", default=f"{__name__}._DefaultThing") is _DefaultThing


# ---------------------------------------------------------------------------
# Pilot: users.User (§55 slice 1 proof-of-concept, item 4)
# ---------------------------------------------------------------------------


class HostUserProfilePresenter(UserProfilePresenter):
    """Host override: adds a field, otherwise identical to the base.

    Example:
        {
            "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "email": "user@example.com",
            "display_name": "Alice",
            "is_host_flavored": true
        }
    """

    model = UserProfilePresenter.model
    fields = UserProfilePresenter.fields
    custom_fields = {
        **UserProfilePresenter.custom_fields,
        "is_host_flavored": PresenterField(
            type=bool, source=lambda dao: True, help_text="Marks the host override.",
        ),
    }


@pytest.mark.django_db
class TestUsersPilot:
    def test_present_dao_to_dto(self):
        user = User.objects.create(
            username="alice", email="alice@example.com",
        )
        dto = UserProfilePresenter.present(user)
        assert dto.email == "alice@example.com"
        assert dto.display_name == "alice"  # falls back to username
        assert isinstance(dto.id, uuid.UUID)

    def test_schema_generation(self):
        ser = UserProfilePresenter.serializer_class()()
        assert ser.fields["display_name"].help_text == (
            "Public display name (falls back to the username)."
        )
        examples = UserProfilePresenter.serializer_class()._spectacular_annotation["examples"]
        assert examples[0].value["display_name"] == "Alice"

    def test_default_presenter_is_the_core_one(self):
        assert get_user_profile_presenter() is UserProfilePresenter

    def test_presenter_swapped_via_config(self):
        with override_settings(
            STAPEL_SWAP={PRESENTER_KEY: f"{__name__}.HostUserProfilePresenter"}
        ):
            clear_swap_cache()
            active = get_user_profile_presenter()
        assert active is HostUserProfilePresenter

        user = User.objects.create(username="bob", email="bob@example.com")
        dto = active.present(user)
        assert dto.is_host_flavored is True
        assert dto.display_name == "bob"
