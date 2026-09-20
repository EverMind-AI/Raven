"""Tool arguments against the tool's declared JSON schema: safe casts, then validation.

The registry runs this on every call after the tool's own ``cast_params`` hook
and before ``execute``: a string that should be an integer -- or an array, or
an object -- is coerced, a missing required key or a wrong type is reported,
and a rejected call never reaches the tool. Kept out of the paper --
:class:`raven.contracts.tool.Tool` declares the schema and the hooks; this is
what the harness does with them.
"""

import json
from typing import Any

_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def cast_params(schema: dict[str, Any] | None, params: dict[str, Any]) -> dict[str, Any]:
    """Apply safe schema-driven casts before validation."""
    schema = schema or {}
    if schema.get("type", "object") != "object":
        return params

    return _cast_object(params, schema)


def _cast_object(obj: Any, schema: dict[str, Any]) -> dict[str, Any]:
    """Cast an object (dict) according to schema."""
    if not isinstance(obj, dict):
        return obj

    props = schema.get("properties", {})
    result = {}

    for key, value in obj.items():
        if key in props:
            result[key] = _cast_value(value, props[key])
        else:
            result[key] = value

    return result


def _cast_value(val: Any, schema: dict[str, Any]) -> Any:
    """Cast a single value according to schema."""
    target_type = schema.get("type")

    if isinstance(target_type, list):
        # JSON Schema lets `type` be a list of alternatives, and that is how a
        # field says it may be given as an explicit nothing rather than left
        # out: `["integer", "null"]`. Everything below indexes `_TYPE_MAP`
        # with this value, so a list arrived as a dict key and raised
        # `unhashable type: 'list'` -- from the registry, before the tool ran,
        # which reads from the outside exactly like the tool being broken.
        if val is None:
            return val
        named = [one for one in target_type if one != "null"]
        return _cast_value(val, {**schema, "type": named[0]}) if named else val

    if target_type == "boolean" and isinstance(val, bool):
        return val
    if target_type == "integer" and isinstance(val, int) and not isinstance(val, bool):
        return val
    if target_type in _TYPE_MAP and target_type not in ("boolean", "integer", "array", "object"):
        expected = _TYPE_MAP[target_type]
        if isinstance(val, expected):
            return val

    if target_type == "integer" and isinstance(val, str):
        try:
            return int(val)
        except ValueError:
            return val

    if target_type == "number" and isinstance(val, str):
        try:
            return float(val)
        except ValueError:
            return val

    if target_type == "string":
        return val if val is None else str(val)

    if target_type == "boolean" and isinstance(val, str):
        val_lower = val.lower()
        if val_lower in ("true", "1", "yes"):
            return True
        if val_lower in ("false", "0", "no"):
            return False
        return val

    if target_type in ("array", "object") and isinstance(val, str):
        # Taken only when the parse yields the type the schema asked for, so a
        # string that is not JSON -- or is JSON of the wrong shape -- reaches
        # `validate_params` untouched and is refused by name. Coercing it into
        # something would trade a precise error for a confusing one.
        #
        # `RecursionError` is in the list because `json.loads` answers deeply
        # nested input with it and it is not a `ValueError`; see
        # `agent/tools/ask_user.py::_loads`, where catching only the other two
        # let it escape and kill the turn.
        try:
            parsed = json.loads(val)
        except (ValueError, TypeError, RecursionError):
            return val
        if isinstance(parsed, _TYPE_MAP[target_type]):
            return _cast_value(parsed, schema)
        return val

    if target_type == "array" and isinstance(val, list):
        item_schema = schema.get("items")
        return [_cast_value(item, item_schema) for item in val] if item_schema else val

    if target_type == "object" and isinstance(val, dict):
        return _cast_object(val, schema)

    return val


def validate_params(schema: dict[str, Any] | None, params: dict[str, Any]) -> list[str]:
    """Validate tool parameters against JSON schema. Returns error list (empty if valid)."""
    if not isinstance(params, dict):
        return [f"parameters must be an object, got {type(params).__name__}"]
    schema = schema or {}
    if schema.get("type", "object") != "object":
        raise ValueError(f"Schema must be object type, got {schema.get('type')!r}")
    return _validate(params, {**schema, "type": "object"}, "")


def _validate(val: Any, schema: dict[str, Any], path: str) -> list[str]:
    t, label = schema.get("type"), path or "parameter"

    if isinstance(t, list):
        # The alternatives above, checked the way alternatives are: valid if
        # the value satisfies any one of them, and the complaint from the
        # first named type when it satisfies none -- one type's message says
        # what to send, where a list of every alternative's would not.
        if val is None and "null" in t:
            return []
        first: list[str] = []
        for one in t:
            if one == "null":
                continue
            errors = _validate(val, {**schema, "type": one}, path)
            if not errors:
                return []
            first = first or errors
        return first

    if t == "integer" and (not isinstance(val, int) or isinstance(val, bool)):
        return [f"{label} should be integer"]
    if t == "number" and (not isinstance(val, _TYPE_MAP[t]) or isinstance(val, bool)):
        return [f"{label} should be number"]
    if t in _TYPE_MAP and t not in ("integer", "number") and not isinstance(val, _TYPE_MAP[t]):
        return [f"{label} should be {t}"]

    errors = []
    if "enum" in schema and val not in schema["enum"]:
        errors.append(f"{label} must be one of {schema['enum']}")
    if t in ("integer", "number"):
        if "minimum" in schema and val < schema["minimum"]:
            errors.append(f"{label} must be >= {schema['minimum']}")
        if "maximum" in schema and val > schema["maximum"]:
            errors.append(f"{label} must be <= {schema['maximum']}")
    if t == "string":
        if "minLength" in schema and len(val) < schema["minLength"]:
            errors.append(f"{label} must be at least {schema['minLength']} chars")
        if "maxLength" in schema and len(val) > schema["maxLength"]:
            errors.append(f"{label} must be at most {schema['maxLength']} chars")
    if t == "object":
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in val:
                errors.append(f"missing required {path + '.' + k if path else k}")
        for k, v in val.items():
            if k in props:
                errors.extend(_validate(v, props[k], path + "." + k if path else k))
    if t == "array" and "items" in schema:
        for i, item in enumerate(val):
            errors.extend(_validate(item, schema["items"], f"{path}[{i}]" if path else f"[{i}]"))
    return errors


__all__ = ["cast_params", "validate_params"]
