"""Turn a JSON Schema into the strict subset a constrained decoder accepts.

`strict: true` is what makes structured output a decoder *constraint* rather
than a hint — but the endpoints that honour it accept only a narrow subset of
JSON Schema, and a schema outside that subset is rejected before a single
token is generated. The subset originates with OpenAI; Meta documents theirs
as "modeled on OpenAI's", and xAI and OpenRouter accept the same shape.

Two rules matter, and pydantic satisfies neither on its own:

**Every object must list every property as required.** Pydantic omits any
field that has a default, which is correct JSON Schema and wrong here — so a
model with a single defaulted field is rejected outright. This is the trap:
`extra="forbid"` gives you `additionalProperties: false` and makes the schema
*look* ready, while `required` quietly stays short.

**Unsupported constraint keywords must go.** `minLength`, `pattern`,
`maxItems` and friends are not in the subset. Dropping them loses no safety:
the response is re-validated client-side against the real model, which still
enforces every constraint. What the wire schema does is shape the decoder; what
pydantic does is check the answer. Only the first has to fit the subset.

The all-required rule does change what the model is asked for — an optional
field becomes one the model must emit. That is a genuine cost, not a free
transform, and it is why this is applied at the transport that demands it
rather than to the caller's model. Anthropic's parse path derives its own
format from the raw pydantic schema and needs none of this, so it never sees
the transform.

Lives in core rather than next to the LLM transport because it is a pure JSON
Schema transform with no provider knowledge in it — and because two sides need
it: the transport that sends the schema, and any caller that wants to inspect
what will actually go out before spending money on the call.

Ported from the harness (``pipeline/summarize/providers.py``, measured against
four provider families), where it was written after live calls failed on
exactly the schema pydantic emits by default.
"""

from __future__ import annotations

import copy
from typing import Any

#: Keywords the strict subset may reject. Client-side validation still enforces
#: all of them when the response is parsed back into the model.
DROPPED_KEYS = (
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minItems",
    "maxItems",
    "pattern",
    "format",
    "default",
)


def to_strict_subset(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``schema`` that a strict-mode decoder will accept.

    Recurses through ``properties``, ``$defs``/``definitions``, ``items`` and
    the combinators, because a nested object that misses the rules fails the
    whole request just as surely as the root does.

    The input is deep-copied. Mutating a caller's schema in place would be a
    particularly nasty surprise here: the same model object is typically reused
    across calls, and the second call would be handed an already-transformed
    schema with its constraints gone.
    """
    out = copy.deepcopy(schema)

    def _walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                _walk(item)
            return
        if not isinstance(node, dict):
            return
        for key in DROPPED_KEYS:
            node.pop(key, None)
        props = node.get("properties")
        if isinstance(props, dict):
            node["required"] = sorted(props.keys())
            node["additionalProperties"] = False
        for child in ("properties", "$defs", "definitions"):
            sub = node.get(child)
            if isinstance(sub, dict):
                for value in sub.values():
                    _walk(value)
        for child in ("items", "anyOf", "oneOf", "allOf", "prefixItems"):
            if child in node:
                _walk(node[child])

    _walk(out)
    return out
