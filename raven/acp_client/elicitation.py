"""The schema half of ACP elicitation: wire params in, questions and content out.

Deliberately free of transport, broker and event loop, because everything here is
a decision about a JSON Schema and is worth testing without an agent process. The
glue that turns a `Field` into a question a human sees is `raven.acp_client.elicitor`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

METHOD = "elicitation/create"

MAX_FIELD_RETRIES = 2
"""Re-asks per field before the whole elicitation declines.

Bounded because a `pattern` the user cannot satisfy would otherwise re-ask
forever, and an honest decline beats a loop."""

_TRUE = {"yes", "y", "true", "1", "on"}
_FALSE = {"no", "n", "false", "0", "off"}
_TYPES = {"string", "integer", "number", "boolean", "array"}


@dataclass
class Field:
    name: str
    prompt: str
    type: str
    options: list[str] = dc_field(default_factory=list)
    required: bool = False
    constraints: dict[str, Any] = dc_field(default_factory=dict)
    custom_name: str | None = None
    """The paired free-text property a dialect folded into this one, if any."""
    meta: dict[str, Any] = dc_field(default_factory=dict)
    """The property's `_meta`, verbatim.

    Kept because it is where an adapter says things the schema has no vocabulary
    for -- which properties are two halves of one question, for one -- and a
    dialect reading that beats it guessing from the property's name."""


@dataclass
class Ask:
    message: str
    mode: str
    schema: dict[str, Any] = dc_field(default_factory=dict)
    session_id: str | None = None
    tool_call_id: str | None = None
    request_id: str | None = None


def parse_request(params: dict[str, Any]) -> Ask | None:
    """One `elicitation/create` params object, or `None` if it is not one.

    The scope fields are flattened onto the same object by the protocol's own
    `anyOf`, so `sessionId` sits beside `mode` rather than under a wrapper.
    """
    if not isinstance(params, dict):
        return None
    mode, message = params.get("mode"), params.get("message")
    if not isinstance(mode, str) or not isinstance(message, str):
        return None
    schema = params.get("requestedSchema")
    return Ask(
        message=message,
        mode=mode,
        schema=schema if isinstance(schema, dict) else {},
        session_id=_str(params.get("sessionId")),
        tool_call_id=_str(params.get("toolCallId")),
        request_id=_str(params.get("requestId")),
    )


def fields(schema: dict[str, Any]) -> list[Field]:
    """The schema's properties as an ordered question list.

    Empty for anything unusable, including a single property of an unknown type:
    a partly-asked form cannot produce content matching the schema, so the caller
    declines rather than asking half of it.
    """
    if not isinstance(schema, dict):
        return []
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        return []
    required = schema.get("required")
    required = set(required) if isinstance(required, list) else set()
    out: list[Field] = []
    for name, spec in props.items():
        if not isinstance(name, str) or not isinstance(spec, dict):
            return []
        kind = spec.get("type", "string")
        if kind not in _TYPES:
            return []
        meta = spec.get("_meta")
        out.append(
            Field(
                name=name,
                prompt=_str(spec.get("title")) or _str(spec.get("description")) or name,
                type=kind,
                options=_options(spec),
                required=name in required,
                constraints={
                    k: spec[k] for k in ("pattern", "minLength", "maxLength", "minimum", "maximum") if k in spec
                },
                meta=meta if isinstance(meta, dict) else {},
            )
        )
    return out


def coerce(field: Field, text: str) -> tuple[bool, Any]:
    """One typed value from what the user typed, or `(False, None)`."""
    text = text.strip()
    if field.type == "array":
        items = [p.strip() for p in text.split(",") if p.strip()]
        if field.options and any(i not in field.options for i in items):
            return (False, None)
        return (True, items)
    if field.type == "boolean":
        low = text.lower()
        if low in _TRUE:
            return (True, True)
        if low in _FALSE:
            return (True, False)
        return (False, None)
    if field.type in ("integer", "number"):
        try:
            value: Any = int(text) if field.type == "integer" else float(text)
        except ValueError:
            return (False, None)
        return (True, value) if _in_range(field, value) else (False, None)
    if field.options and text not in field.options:
        return (False, None)
    return (True, text) if _string_ok(field, text) else (False, None)


def accept(content: dict[str, Any]) -> dict[str, Any]:
    return {"action": "accept", "content": content}


def decline() -> dict[str, Any]:
    return {"action": "decline"}


def cancel() -> dict[str, Any]:
    return {"action": "cancel"}


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _options(spec: dict[str, Any]) -> list[str]:
    """Choices from `enum`, or from `oneOf` / `anyOf` `const` values.

    An array property carries its choices on `items`, so look there too: a
    multi-select is an array whose item schema holds the enum.
    """
    items = spec.get("items")
    for holder in (spec, items if isinstance(items, dict) else {}):
        enum = holder.get("enum")
        if isinstance(enum, list):
            return [v for v in enum if isinstance(v, str)]
        for key in ("oneOf", "anyOf"):
            variants = holder.get(key)
            if isinstance(variants, list):
                picked = [v.get("const") for v in variants if isinstance(v, dict)]
                picked = [c for c in picked if isinstance(c, str)]
                if picked:
                    return picked
    return []


def _in_range(field: Field, value: float) -> bool:
    low, high = field.constraints.get("minimum"), field.constraints.get("maximum")
    if isinstance(low, (int, float)) and value < low:
        return False
    return not (isinstance(high, (int, float)) and value > high)


def _string_ok(field: Field, text: str) -> bool:
    low, high = field.constraints.get("minLength"), field.constraints.get("maxLength")
    if isinstance(low, int) and len(text) < low:
        return False
    if isinstance(high, int) and len(text) > high:
        return False
    pattern = field.constraints.get("pattern")
    if isinstance(pattern, str):
        try:
            return re.search(pattern, text) is not None
        except re.error:
            # An agent's bad regexp is not the user's problem: accept rather than
            # declining a question that was otherwise answerable.
            return True
    return True


__all__ = [
    "METHOD",
    "MAX_FIELD_RETRIES",
    "Ask",
    "Field",
    "accept",
    "cancel",
    "coerce",
    "decline",
    "fields",
    "parse_request",
]
