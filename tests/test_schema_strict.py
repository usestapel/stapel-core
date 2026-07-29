"""The strict-subset transform, on plain schema dicts.

Deliberately pydantic-free. Core does not depend on pydantic — dataclasses
inside, DRF at the HTTP edge, pydantic only where untrusted structured text
arrives — and a test that reached for it would either add that dependency for
no reason or pass locally and fail in CI. It did exactly that once: the shared
dev venv had pydantic from a sibling library, so the import succeeded here and
nowhere else.

The companion test that pins the *premise* — that pydantic's own output is not
strict-ready — lives in `stapel-agent`, which has pydantic legitimately.

Every test below pins one rule of the subset. They read as trivia until you
remember what they buy: a request a constrained decoder will actually accept.
Loosening any of them surfaces as an HTTP error from the provider, after the
prompt was assembled and sent.
"""

import pytest

from stapel_core.schema_strict import DROPPED_KEYS, to_strict_subset

#: The shape pydantic emits for a model with defaulted fields: `required`
#: lists only the mandatory one. Written as a literal so this test does not
#: need pydantic to describe pydantic's behaviour.
PYDANTIC_LIKE = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "name": {"type": "string", "minLength": 1, "description": "kept"},
        "tags": {"type": "array", "default": [], "items": {"type": "string"}},
        "inner": {"anyOf": [{"$ref": "#/$defs/Inner"}, {"type": "null"}], "default": None},
    },
    "required": ["name"],
    "$defs": {
        "Inner": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"a": {"type": "string"}, "b": {"type": "integer", "default": 0}},
            "required": ["a"],
        }
    },
}


def test_every_property_becomes_required():
    """The rule pydantic does not satisfy, and the whole reason for this module.

    A defaulted field is correctly optional in JSON Schema and rejected by
    strict mode, which demands every property in `required`.
    """
    out = to_strict_subset(PYDANTIC_LIKE)
    assert out["required"] == ["inner", "name", "tags"]


def test_nested_definitions_are_transformed_too():
    """A nested object that misses the rules fails the whole request."""
    inner = to_strict_subset(PYDANTIC_LIKE)["$defs"]["Inner"]
    assert inner["required"] == ["a", "b"]
    assert inner["additionalProperties"] is False


def test_objects_forbid_extras():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    assert to_strict_subset(schema)["additionalProperties"] is False


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


def test_descriptions_survive():
    """They are how word limits and taxonomies reach the model at all."""
    out = to_strict_subset(PYDANTIC_LIKE)
    assert out["properties"]["name"]["description"] == "kept"


def test_the_callers_schema_is_not_mutated():
    """The same schema object is reused across calls.

    Transforming in place would hand the second call an already-stripped
    schema — constraints gone, and nothing to say where they went.
    """
    before_required = PYDANTIC_LIKE["required"][:]
    to_strict_subset(PYDANTIC_LIKE)
    assert PYDANTIC_LIKE["required"] == before_required
    assert "minLength" in PYDANTIC_LIKE["properties"]["name"]


def test_combinators_are_walked():
    schema = {
        "anyOf": [
            {"type": "object", "properties": {"a": {"type": "string", "pattern": "x"}}},
            {"type": "null"},
        ]
    }
    branch = to_strict_subset(schema)["anyOf"][0]
    assert branch["required"] == ["a"]
    assert branch["additionalProperties"] is False
    assert "pattern" not in branch["properties"]["a"]


def test_empty_and_scalar_nodes_survive():
    assert to_strict_subset({}) == {}
    assert to_strict_subset({"type": "string"}) == {"type": "string"}
