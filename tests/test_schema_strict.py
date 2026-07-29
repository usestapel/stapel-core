"""The strict-subset transform, and the trap it exists to close.

The failure this prevents happens before any token is generated: the endpoint
rejects the request. That makes it cheap to hit and easy to misdiagnose — the
schema looks right, pydantic produced it, and `additionalProperties: false` is
already there.
"""

import pytest
from pydantic import BaseModel, ConfigDict, Field

from stapel_core.schema_strict import DROPPED_KEYS, to_strict_subset


class Inner(BaseModel):
    model_config = ConfigDict(extra="forbid")
    a: str
    b: int = 0


class Outer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=1, description="kept: descriptions are not constraints")
    tags: list[str] = []
    inner: Inner | None = None


def test_pydantic_alone_is_not_strict_ready():
    """The premise. If this ever stops being true, the transform can go."""
    raw = Outer.model_json_schema()
    assert raw["required"] == ["name"], "pydantic omits defaulted fields — that is the trap"


def test_every_property_becomes_required():
    out = to_strict_subset(Outer.model_json_schema())
    assert out["required"] == ["inner", "name", "tags"]


def test_nested_definitions_are_transformed_too():
    """A nested object that misses the rules fails the whole request."""
    out = to_strict_subset(Outer.model_json_schema())
    inner = out["$defs"]["Inner"]
    assert inner["required"] == ["a", "b"]
    assert inner["additionalProperties"] is False


def test_objects_forbid_extras():
    out = to_strict_subset(Outer.model_json_schema())
    assert out["additionalProperties"] is False


@pytest.mark.parametrize("keyword", DROPPED_KEYS)
def test_unsupported_keywords_are_dropped_everywhere(keyword):
    schema = {
        "type": "object",
        "properties": {
            "x": {"type": "string", keyword: 1},
            "y": {"type": "array", "items": {"type": "string", keyword: 1}},
        },
    }
    out = to_strict_subset(schema)
    assert keyword not in out["properties"]["x"]
    assert keyword not in out["properties"]["y"]["items"]


def test_dropping_constraints_is_safe_because_the_model_still_checks():
    """The wire schema shapes the decoder; pydantic checks the answer.

    Only the first has to fit the subset — which is why dropping `min_length`
    from the wire costs nothing.
    """
    out = to_strict_subset(Outer.model_json_schema())
    assert "minLength" not in out["properties"]["name"]
    with pytest.raises(ValueError):
        Outer(name="", tags=[], inner=None)


def test_descriptions_survive():
    """They are how word limits and taxonomies reach the model at all."""
    out = to_strict_subset(Outer.model_json_schema())
    assert out["properties"]["name"]["description"].startswith("kept")


def test_the_callers_schema_is_not_mutated():
    """The same model object is reused across calls.

    Transforming in place would hand the second call an already-stripped
    schema — constraints gone, and nothing to say where they went.
    """
    raw = Outer.model_json_schema()
    before = raw["required"][:]
    to_strict_subset(raw)
    assert raw["required"] == before
    assert "minLength" in raw["properties"]["name"]


def test_combinators_are_walked():
    schema = {
        "anyOf": [
            {"type": "object", "properties": {"a": {"type": "string", "pattern": "x"}}},
            {"type": "null"},
        ]
    }
    out = to_strict_subset(schema)
    branch = out["anyOf"][0]
    assert branch["required"] == ["a"]
    assert branch["additionalProperties"] is False
    assert "pattern" not in branch["properties"]["a"]


def test_empty_and_scalar_nodes_survive():
    assert to_strict_subset({}) == {}
    assert to_strict_subset({"type": "string"}) == {"type": "string"}
