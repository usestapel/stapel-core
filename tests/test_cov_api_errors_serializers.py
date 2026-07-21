"""Coverage tests for stapel_core.django.api errors gaps, serializers and routers."""
import sys
from dataclasses import dataclass, field
from enum import Enum
from types import SimpleNamespace

import pytest
from rest_framework import serializers as drf_serializers
from rest_framework.exceptions import ErrorDetail

from stapel_core.django.api.errors import (
    ERR_400_VALIDATION_ERROR,
    ERR_404_NOT_FOUND,
    ErrorKeysView,
    _drf_code_to_error_key,
    _extract_first_field_error,
    _registered_key,
    error_402_payment_required,
    error_405_method_not_allowed,
    error_408_request_timeout,
    error_409_conflict,
    error_410_gone,
    error_413_payload_too_large,
    error_422_unprocessable_entity,
    error_423_locked,
    error_429_too_many_requests,
)
from stapel_core.django.api.routers import OptionalSlashRouter
from stapel_core.django.api.serializers import (
    StapelDataclassSerializer,
    _apply_enum_descriptions,
    _attach_example,
    _coerce_example,
    _parse_docstring,
    _set_field_example,
)


# ---------------------------------------------------------------------------
# errors.py — remaining helpers and branches
# ---------------------------------------------------------------------------


class TestRemainingErrorHelpers:
    @pytest.mark.parametrize(
        "helper,status",
        [
            (error_402_payment_required, 402),
            (error_405_method_not_allowed, 405),
            (error_408_request_timeout, 408),
            (error_409_conflict, 409),
            (error_410_gone, 410),
            (error_413_payload_too_large, 413),
            (error_422_unprocessable_entity, 422),
            (error_423_locked, 423),
            (error_429_too_many_requests, 429),
        ],
    )
    def test_status_codes(self, helper, status):
        resp = helper()
        assert resp.status_code == status
        assert resp.data["localizable_error"].startswith(f"error.{status}.")


class TestDrfCodeMapping:
    def test_known_code(self):
        assert _drf_code_to_error_key("required") == "error.400.field.required"

    def test_unknown_code_falls_back(self):
        assert _drf_code_to_error_key("totally_unknown") == ERR_400_VALIDATION_ERROR

    def test_registered_key_none(self):
        assert _registered_key(None) is None
        assert _registered_key("nope.not.registered") is None
        assert _registered_key(ERR_404_NOT_FOUND) == ERR_404_NOT_FOUND


class TestExtractFirstFieldError:
    def test_registered_key_string(self):
        key, params, msg = _extract_first_field_error(ERR_404_NOT_FOUND)
        assert key == ERR_404_NOT_FOUND
        assert params == {}

    def test_error_detail_with_mappable_code(self):
        detail = ErrorDetail("This field is required.", code="required")
        key, params, msg = _extract_first_field_error(detail)
        assert key == "error.400.field.required"
        assert msg == "This field is required."

    def test_error_detail_invalid_code(self):
        detail = ErrorDetail("Bad.", code="invalid")
        key, _, _ = _extract_first_field_error(detail)
        assert key == ERR_400_VALIDATION_ERROR

    def test_error_detail_unmappable_code(self):
        detail = ErrorDetail("Odd.", code="strange_code")
        key, _, _ = _extract_first_field_error(detail)
        assert key == ERR_400_VALIDATION_ERROR

    def test_list_with_registered_key(self):
        key, params, _ = _extract_first_field_error([ERR_404_NOT_FOUND])
        assert key == ERR_404_NOT_FOUND

    def test_list_with_code(self):
        key, _, _ = _extract_first_field_error(
            [ErrorDetail("Too long", code="max_length")]
        )
        assert key == "error.400.field.max_length"

    def test_dict_with_non_list_value(self):
        key, params, _ = _extract_first_field_error(
            {"name": ErrorDetail("Required", code="required")}
        )
        assert key == "error.400.field.required"
        assert params == {"field": "name"}

    def test_dict_value_is_registered_key(self):
        key, params, _ = _extract_first_field_error({"code": [ERR_404_NOT_FOUND]})
        assert key == ERR_404_NOT_FOUND
        assert params == {"field": "code"}

    def test_non_field_errors_registered_key(self):
        key, params, _ = _extract_first_field_error(
            {"non_field_errors": [ERR_404_NOT_FOUND]}
        )
        assert key == ERR_404_NOT_FOUND
        assert params == {}

    def test_empty_dict_falls_back(self):
        key, params, msg = _extract_first_field_error({})
        assert key == ERR_400_VALIDATION_ERROR
        assert msg == "Validation error"


class TestErrorKeysView:
    def test_get_returns_registry(self):
        resp = ErrorKeysView().get(None)
        assert resp.data[ERR_404_NOT_FOUND] == "Requested resource not found"

    def test_get_service_errors_default(self):
        assert ErrorKeysView().get_service_errors() == {}


# ---------------------------------------------------------------------------
# routers.py
# ---------------------------------------------------------------------------


class TestOptionalSlashRouter:
    def test_trailing_slash_optional(self):
        router = OptionalSlashRouter()
        assert router.trailing_slash == "/?"


# ---------------------------------------------------------------------------
# serializers.py — StapelDataclassSerializer
# ---------------------------------------------------------------------------


class Color(str, Enum):
    """Colors.

    Members:
        RED: Warm color.
        BLUE: Cool color.
    """

    RED = "RED"
    BLUE = "BLUE"


class Status(str, Enum):
    def __new__(cls, value, description=""):
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj.description = description
        return obj

    ON = ("ON", "Turned on")
    OFF = ("OFF", "Turned off")


class Undocumented(str, Enum):
    A = "A"
    B = "B"


@dataclass
class Doc:
    """Schema description here.

    Attributes:
        name: Display name. Example: Alice
        color: Chosen color.
        plain: No example given
    """

    name: str
    color: Color
    plain: str = "x"


class DocSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = Doc


@dataclass
class MetaDoc:
    """Meta doc.

    Attributes:
        name: Doc help. Example: doc-example
    """

    name: str = field(
        default="x",
        metadata={
            "help_text": "Meta help",
            "example": "meta-example",
            "required": False,
        },
    )


class MetaDocSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = MetaDoc


@dataclass
class BlankDoc:
    """A DTO with an empty-string-default field (libgaps Н4 pattern).

    Attributes:
        title: Required title.
        description: Optional description defaulting to empty.
        note: Optional note defaulting to a non-empty value.
        forced: Empty default but explicitly blank-rejecting via metadata.
    """

    title: str
    description: str = ""
    note: str = "n/a"
    forced: str = field(default="", metadata={"allow_blank": False})


class BlankDocSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = BlankDoc


class TestEmptyStringDefaultAllowsBlank:
    """A str field defaulting to "" must accept "" (libgaps Н4)."""

    def test_empty_default_field_allows_blank(self):
        f = BlankDocSerializer().fields["description"]
        assert isinstance(f, drf_serializers.CharField)
        assert f.allow_blank is True
        assert f.required is False

    def test_explicit_empty_string_validates(self):
        s = BlankDocSerializer(data={"title": "t", "description": ""})
        assert s.is_valid(), s.errors
        assert s.validated_data.description == ""

    def test_omitted_key_still_validates(self):
        s = BlankDocSerializer(data={"title": "t"})
        assert s.is_valid(), s.errors

    def test_non_empty_default_does_not_force_blank(self):
        # Only an empty-string default implies blank-is-valid; a "n/a" default
        # is optional but not blankable by this rule.
        assert BlankDocSerializer().fields["note"].allow_blank is False

    def test_metadata_override_wins(self):
        # An explicit allow_blank=False in metadata beats the "" default rule.
        assert BlankDocSerializer().fields["forced"].allow_blank is False

    def test_required_field_rejects_blank(self):
        s = BlankDocSerializer(data={"title": "", "description": "x"})
        assert not s.is_valid()
        assert "title" in s.errors


@dataclass
class StatusDoc:
    status: Status


class StatusDocSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = StatusDoc


@dataclass
class UndocumentedDoc:
    kind: Undocumented


class UndocumentedDocSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = UndocumentedDoc


class TestSubclassBehaviour:
    def test_docstring_becomes_class_doc(self):
        assert DocSerializer.__doc__ == "Schema description here."

    def test_example_attached_from_docstring(self):
        examples = DocSerializer._spectacular_annotation["examples"]
        assert examples[0].value == {"name": "Alice"}

    def test_metadata_example_takes_precedence(self):
        examples = MetaDocSerializer._spectacular_annotation["examples"]
        assert examples[0].value == {"name": "meta-example"}

    def test_subclass_without_meta_dataclass_is_noop(self):
        class Bare(StapelDataclassSerializer):
            pass

        assert not hasattr(Bare, "_stapel_doc") or Bare._stapel_doc is not None

    def test_get_fields_applies_docstring_help(self):
        fields = DocSerializer().get_fields()
        assert fields["name"].help_text == "Display name"
        assert fields["name"]._spectacular_annotation["example"] == "Alice"
        assert fields["plain"].help_text == "No example given"

    def test_get_fields_metadata_overrides(self):
        fields = MetaDocSerializer().get_fields()
        assert fields["name"].help_text == "Meta help"
        assert fields["name"].required is False
        assert fields["name"]._spectacular_annotation["example"] == "meta-example"

    def test_enum_choices_from_docstring_members(self):
        fields = DocSerializer().get_fields()
        assert dict(fields["color"].choices) == {
            "RED": "Warm color",
            "BLUE": "Cool color",
        }

    def test_enum_choices_from_description_attribute(self):
        fields = StatusDocSerializer().get_fields()
        assert dict(fields["status"].choices) == {
            "ON": "Turned on",
            "OFF": "Turned off",
        }

    def test_enum_without_docs_left_untouched(self):
        fields = UndocumentedDocSerializer().get_fields()
        assert set(dict(fields["kind"].choices)) == {"A", "B"}


class TestAutoGeneratedInstance:
    def test_description_annotation_set(self):
        inst = StapelDataclassSerializer(dataclass=Doc)
        assert inst._spectacular_annotation["description"] == "Schema description here."

    def test_get_fields_runtime_parse(self):
        inst = StapelDataclassSerializer(dataclass=Doc)
        fields = inst.get_fields()
        assert fields["name"].help_text == "Display name"

    def test_serializer_dataclass_field_property(self):
        inst = StapelDataclassSerializer(dataclass=Doc)
        assert inst.serializer_dataclass_field is StapelDataclassSerializer

    def test_no_description_no_annotation(self):
        @dataclass
        class NoDoc:
            value: int

        NoDoc.__doc__ = ""  # dataclasses auto-generate a signature docstring
        inst = StapelDataclassSerializer(dataclass=NoDoc)
        annotation = getattr(inst, "_spectacular_annotation", None) or {}
        assert "description" not in annotation


class TestParseDocstring:
    def test_empty(self):
        parsed = _parse_docstring("")
        assert parsed == {"description": "", "attributes": {}, "members": {}}

    def test_members_section(self):
        parsed = _parse_docstring(
            "Top.\n\n    Members:\n        ON: Is on.\n        OFF: Is off.\n"
        )
        assert parsed["members"] == {"ON": "Is on", "OFF": "Is off"}
        assert parsed["description"] == "Top."

    def test_attributes_with_and_without_example(self):
        parsed = _parse_docstring(
            "Desc.\n\n"
            "    Attributes:\n"
            "        a: Help text. Example: 42\n"
            "        b: Plain help.\n"
        )
        assert parsed["attributes"]["a"] == {"help_text": "Help text", "example": 42}
        assert parsed["attributes"]["b"] == {"help_text": "Plain help"}


class TestHelpers:
    def test_apply_enum_descriptions_empty_enum(self):
        class Empty(Enum):
            pass

        fake_field = SimpleNamespace(enum_class=Empty)
        _apply_enum_descriptions(fake_field, {})  # early return, no error

    def test_coerce_example_json_values(self):
        assert _coerce_example('{"a": 1}') == {"a": 1}
        assert _coerce_example("42") == 42
        assert _coerce_example("true") is True
        assert _coerce_example("plain text") == "plain text"

    def test_set_field_example(self):
        f = drf_serializers.CharField()
        _set_field_example(f, "hello")
        assert f._spectacular_annotation["example"] == "hello"

    def test_attach_example(self):
        class Dummy:
            pass

        _attach_example(Dummy, "Name", {"k": "v"})
        assert Dummy._spectacular_annotation["examples"][0].value == {"k": "v"}

    def test_attach_example_import_error(self, monkeypatch):
        class Dummy2:
            pass

        monkeypatch.setitem(sys.modules, "drf_spectacular.utils", None)
        _attach_example(Dummy2, "Name", {"k": "v"})
        assert not hasattr(Dummy2, "_spectacular_annotation")
