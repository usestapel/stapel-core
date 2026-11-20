"""Docstring examples that are falsy (0, false, "") reach the emitted schema.

``Example: 0`` is as much a declaration as ``Example: 7``; a truthiness test
on the parsed value used to drop it from both the field ``example`` and the
component's auto-attached example, so a contract example missed the field
and failed validation against its own ``required`` list.
"""
from dataclasses import dataclass

import pytest
from drf_spectacular.generators import SchemaGenerator
from rest_framework.views import APIView

from stapel_core.django.api.serializers import StapelDataclassSerializer
from stapel_core.django.openapi.schemas import PermissionAwareAutoSchema


@dataclass
class FalsyDoc:
    """A response whose documented examples are all falsy.

    Attributes:
        count: Items left. Example: 0
        enabled: Whether it is on. Example: false
        note: Free text. Example: ""
        ratio: Share of the total. Example: 0.0
        label: A truthy control. Example: seven
    """

    count: int
    enabled: bool
    note: str
    ratio: float
    label: str


class FalsyDocSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = FalsyDoc


EXPECTED = {"count": 0, "enabled": False, "note": "", "ratio": 0.0, "label": "seven"}


class FalsyView(APIView):
    schema = PermissionAwareAutoSchema()
    authentication_classes = []

    def get(self, request):  # pragma: no cover - never called
        raise NotImplementedError

    def get_serializer(self, *args, **kwargs):
        return FalsyDocSerializer(*args, **kwargs)


def _component():
    from django.urls import path

    from drf_spectacular.utils import extend_schema

    view = extend_schema(responses=FalsyDocSerializer)(FalsyView)
    generator = SchemaGenerator(patterns=[path("falsy/", view.as_view())])
    schema = generator.get_schema(request=None, public=True)
    return schema, schema["components"]["schemas"]["FalsyDoc"]


def test_the_component_example_keeps_every_falsy_value():
    examples = FalsyDocSerializer._spectacular_annotation["examples"]
    assert examples[0].value == EXPECTED


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_each_field_carries_its_example_into_the_schema(name):
    _, component = _component()
    prop = component["properties"][name]
    assert "example" in prop, f"{name}: example dropped"
    assert prop["example"] == EXPECTED[name]
    assert type(prop["example"]) is type(EXPECTED[name])


def test_the_operation_example_is_complete():
    schema, _ = _component()
    (item,) = schema["paths"].values()
    content = item["get"]["responses"]["200"]["content"]["application/json"]
    (example,) = content["examples"].values()
    assert example["value"] == EXPECTED
