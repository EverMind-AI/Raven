"""Tool arguments against the tool's declared JSON schema: safe casts, then validation.

The registry runs this on every call after the tool's own ``cast_params`` hook
and before ``execute``: a string that should be an integer -- or an array, or
an object -- is coerced, a missing required key or a wrong type is reported,
and a rejected call never reaches the tool. Kept out of the paper --
:class:`raven.contracts.tool.Tool` declares the schema and the hooks; this is
what the harness does with them.

A rejection is the model's only feedback on a call it got wrong, so each line says
where the problem is, what the schema wanted, and what actually arrived -- the
arriving value described by its type, keys or length rather than echoed. The
casts are lossless for the same reason: one that drops part of an argument makes
the validator judge Raven's rewrite instead of the model's call. Enforced:
``type``, ``enum``, ``const``, ``minimum``/``maximum``, ``minLength``/
``maxLength``, ``pattern``, ``required``, ``properties``,
``additionalProperties``, ``items``, ``minItems``/``maxItems`` and
``uniqueItems``. Every other keyword (``anyOf``, ``oneOf``, ``format``, ...) is
accepted unchecked.
"""

import difflib
import json
import re
from functools import lru_cache
from typing import Any

from raven.contracts.tool import RAW_ARGUMENTS_KEY

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
    extra = schema.get("additionalProperties")
    result = {}

    for key, value in obj.items():
        if key in props:
            result[key] = _cast_value(value, props[key])
        elif isinstance(extra, dict):
            result[key] = _cast_value(value, extra)
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
        # Only a number spells the same text either way. An object or a list
        # went through `str()` as its Python repr and a boolean as "True", so
        # the validator passed a value the model never wrote; left alone, it is
        # reported by name instead.
        return str(val) if isinstance(val, (int, float)) and not isinstance(val, bool) else val

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


# Longest stretch of an argument quoted back in a rejection; anything longer is
# described by its size. The rejection is model text that also lands in the
# session and on screen.
_PREVIEW_CHARS = 40
# How far below an object to look for a required key the model nested too deep.
_LOCATE_DEPTH = 4
_LOCATE_SHOWN = 3
_KEYS_SHOWN = 8
_ERRORS_SHOWN = 8
_SHAPE_CHARS = 300


class ParamError(str):
    """One rejection line, carrying the innermost object it was found in.

    A ``str`` so every caller that joins, searches or compares the list keeps
    working. ``at`` is that object's path (``""`` for the arguments themselves)
    and ``schema`` its schema -- what :func:`render_param_errors` shows the
    expected shape of.
    """

    at: str
    schema: dict[str, Any] | None

    def __new__(cls, text: str, at: str = "", schema: dict[str, Any] | None = None) -> "ParamError":
        error = super().__new__(cls, text)
        error.at = at
        error.schema = schema
        return error


def validate_params(schema: dict[str, Any] | None, params: dict[str, Any]) -> list[str]:
    """Validate tool parameters against JSON schema. Returns error list (empty if valid)."""
    if not isinstance(params, dict):
        return [f"parameters must be an object, got {type(params).__name__}"]
    schema = schema or {}
    if schema.get("type", "object") != "object":
        raise ValueError(f"Schema must be object type, got {schema.get('type')!r}")
    root = {**schema, "type": "object"}
    return _validate(params, root, "", ("", root))


def render_param_errors(errors: list[str]) -> str:
    """The body of an invalid-parameters rejection, to follow its colon.

    One problem stays on the rejection's own line, as it always read; several
    become one line each, cut after ``_ERRORS_SHOWN`` with a count of the rest.
    When a problem sits inside a nested object, that object's expected shape
    follows: the model has the whole schema already, and the level it lost track
    of is the one worth repeating.
    """
    if len(errors) == 1:
        body = f" {errors[0]}"
    else:
        lines = [f"- {error}" for error in errors[:_ERRORS_SHOWN]]
        if len(errors) > _ERRORS_SHOWN:
            lines.append(f"- ... and {len(errors) - _ERRORS_SHOWN} more")
        body = "\n" + "\n".join(lines)
    nested = next((e for e in errors if isinstance(e, ParamError) and e.at and e.schema), None)
    if nested is not None:
        body += f"\nExpected {nested.at}: {_shape(nested.schema)}"
    return body


def _validate(val: Any, schema: dict[str, Any], path: str, owner: tuple[str, dict[str, Any]]) -> list[str]:
    """What is wrong with ``val`` at ``path``; ``owner`` is the object it sits in."""
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
            errors = _validate(val, {**schema, "type": one}, path, owner)
            if not errors:
                return []
            first = first or errors
        return first

    if (
        (t == "integer" and (not isinstance(val, int) or isinstance(val, bool)))
        or (t == "number" and (not isinstance(val, _TYPE_MAP[t]) or isinstance(val, bool)))
        or (t in _TYPE_MAP and t not in ("integer", "number") and not isinstance(val, _TYPE_MAP[t]))
    ):
        got = _describe(val, t)
        if t == "array" and isinstance(val, dict) and _holds_objects(schema):
            got += " -- to send one item, wrap it in a list"
        return [ParamError(f"{label} should be {t}, got {got}", *owner)]

    errors: list[str] = []
    if "enum" in schema and val not in schema["enum"]:
        errors.append(ParamError(f"{label} must be one of {schema['enum']}, got {_describe(val, None)}", *owner))
    if "const" in schema and (val != schema["const"] or isinstance(val, bool) != isinstance(schema["const"], bool)):
        errors.append(ParamError(f"{label} must be {schema['const']!r}, got {_describe(val, None)}", *owner))
    if t in ("integer", "number"):
        if "minimum" in schema and val < schema["minimum"]:
            errors.append(ParamError(f"{label} must be >= {schema['minimum']}, got {val}", *owner))
        if "maximum" in schema and val > schema["maximum"]:
            errors.append(ParamError(f"{label} must be <= {schema['maximum']}, got {val}", *owner))
    if t == "string":
        if "minLength" in schema and len(val) < schema["minLength"]:
            errors.append(ParamError(f"{label} must be at least {schema['minLength']} chars, got {len(val)}", *owner))
        if "maxLength" in schema and len(val) > schema["maxLength"]:
            errors.append(ParamError(f"{label} must be at most {schema['maxLength']} chars, got {len(val)}", *owner))
        pattern = _pattern(schema["pattern"]) if isinstance(schema.get("pattern"), str) else None
        if pattern is not None and not pattern.search(val):
            errors.append(
                ParamError(
                    f"{label} does not match the pattern {schema['pattern']!r}, got {_describe(val, None)}", *owner
                )
            )
    if t == "object":
        errors.extend(_validate_object(val, schema, path))
    if t == "array":
        errors.extend(_validate_array(val, schema, path, owner))
    return errors


def _validate_object(obj: dict[str, Any], schema: dict[str, Any], path: str) -> list[str]:
    """The object's own problems -- missing, misplaced and unknown keys -- then its values'."""
    props = schema.get("properties", {})
    extra = schema.get("additionalProperties")
    # The provider's stand-in for arguments that did not parse: listing it as a
    # key the model sent sends the model after a field it never wrote.
    unknown = [key for key in obj if key not in props and key != RAW_ARGUMENTS_KEY]
    absent = [key for key in props if key not in obj]
    meant: set[str] = set()
    errors: list[str] = []
    for key in schema.get("required", []):
        if key in obj:
            continue
        text = f"missing required {_dot(path, key)}"
        found = _locate(obj, key, path)
        near = _closest(key, [k for k in unknown if k not in meant])
        if found:
            text += (
                f' (a "{key}" key was found inside {_listing(found, _LOCATE_SHOWN)}'
                f" -- it belongs directly on {path or 'the top level'})"
            )
        elif near is not None:
            meant.add(near)
            text += f" ({_dot(path, near)} was sent instead -- did you mean {key}?)"
        errors.append(ParamError(text, path, schema))
    rest = [key for key in unknown if key not in meant]
    if rest and extra is False:
        for key in rest:
            alt = _closest(key, absent)
            hint = f"did you mean {alt}?" if alt else f"allowed: {_listing(list(props), _KEYS_SHOWN) or 'none'}"
            errors.append(ParamError(f"{_dot(path, key)} is not allowed here -- {hint}", path, schema))
    elif rest and errors:
        # An open schema accepts keys it does not name, so these are no error of
        # their own; beside a missing one, though, they are likely where it went.
        named = [f"{key} (did you mean {alt}?)" if (alt := _closest(key, absent)) else key for key in rest]
        errors.append(
            ParamError(
                f"keys the schema does not define were sent at {path or 'the top level'}: "
                f"{_listing(named, _KEYS_SHOWN)}",
                path,
                schema,
            )
        )
    for key, value in obj.items():
        if key in props:
            errors.extend(_validate(value, props[key], _dot(path, key), (path, schema)))
        elif isinstance(extra, dict):
            errors.extend(_validate(value, extra, _dot(path, key), (path, schema)))
    return errors


def _validate_array(
    items: list[Any], schema: dict[str, Any], path: str, owner: tuple[str, dict[str, Any]]
) -> list[str]:
    label = path or "parameter"
    errors: list[str] = []
    if "minItems" in schema and len(items) < schema["minItems"]:
        errors.append(
            ParamError(f"{label} should have at least {_items(schema['minItems'])}, got {len(items)}", *owner)
        )
    if "maxItems" in schema and len(items) > schema["maxItems"]:
        errors.append(ParamError(f"{label} should have at most {_items(schema['maxItems'])}, got {len(items)}", *owner))
    if schema.get("uniqueItems"):
        seen: dict[str, int] = {}
        for index, item in enumerate(items):
            try:
                mark = json.dumps(item, sort_keys=True, default=repr)
            except (TypeError, ValueError, RecursionError):
                continue
            if mark in seen:
                errors.append(
                    ParamError(
                        f"{_index(path, index)} repeats {_index(path, seen[mark])}; items must be unique", *owner
                    )
                )
                break
            seen[mark] = index
    if isinstance(schema.get("items"), dict):
        for index, item in enumerate(items):
            errors.extend(_validate(item, schema["items"], _index(path, index), owner))
    return errors


def _describe(val: Any, expected: str | None) -> str:
    """What arrived, by type and shape rather than by content."""
    if val is None:
        return "null"
    if isinstance(val, bool):
        return f"boolean {json.dumps(val)}"
    if isinstance(val, (int, float)):
        return f"{'integer' if isinstance(val, int) else 'number'} {val}"
    if isinstance(val, dict):
        if not val:
            return "an empty object"
        return f"an object with keys: {_listing([str(k) for k in val], _KEYS_SHOWN)}"
    if isinstance(val, list):
        return f"an array of {_items(len(val))}"
    if isinstance(val, str):
        if expected in ("array", "object") and val.lstrip()[:1] in ("[", "{"):
            return _describe_json_text(val, expected)
        return f"a string {val!r}" if len(val) <= _PREVIEW_CHARS else f"a string ({len(val)} chars)"
    return type(val).__name__


def _describe_json_text(text: str, expected: str) -> str:
    """A string that reads as JSON where an array or object belongs.

    The caster has already taken every such string that parsed to the right
    type, so this one holds the wrong type or stops parsing part-way -- and
    where it stops is what the model needs, since it believes it sent JSON.
    """
    size = f"a string ({len(text)} chars)"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        if exc.msg == "Extra data":
            head = _json_kind(json.JSONDecoder().raw_decode(text, len(text) - len(text.lstrip()))[0])
            return (
                f"{size} holding a JSON {head} followed by extra text at char {exc.pos}: "
                f"{_clip(text[exc.pos :])!r} -- send the {expected} itself rather than a string; "
                "if that text is another parameter, send it beside this one"
            )
        near = text[max(0, exc.pos - _PREVIEW_CHARS // 2) : exc.pos + _PREVIEW_CHARS // 2]
        return f"{size} that is not valid JSON: {exc.msg} at char {exc.pos}, near {near!r}"
    except (ValueError, RecursionError):
        return f"{size} nested too deeply to read as JSON"
    return f"{size} holding a JSON {_json_kind(parsed)}"


def _json_kind(value: Any) -> str:
    """Of JSON text that opens with a bracket, which kind it holds."""
    return "object" if isinstance(value, dict) else "array"


def _holds_objects(schema: dict[str, Any]) -> bool:
    items = schema.get("items")
    return isinstance(items, dict) and (items.get("type") == "object" or "properties" in items)


def _locate(obj: dict[str, Any], key: str, path: str) -> list[str]:
    """Where below ``obj`` the ``key`` it lacks turns up, nearest first."""
    found: list[str] = []
    level = [(_dot(path, k), v) for k, v in obj.items()]
    for _ in range(_LOCATE_DEPTH):
        deeper: list[tuple[str, Any]] = []
        for where, value in level:
            if isinstance(value, dict):
                if key in value:
                    found.append(where)
                deeper.extend((_dot(where, k), v) for k, v in value.items())
            elif isinstance(value, list):
                deeper.extend((_index(where, i), v) for i, v in enumerate(value))
        level = deeper
    return found


def _closest(name: str, candidates: list[str]) -> str | None:
    """The candidate a model most plausibly meant by ``name``, if any is close.

    Spelling-insensitive first, because the live misses were camelCase for
    snake_case (``multiSelect``, ``extractMode``); then by similarity.
    """
    folded = {_fold(c): c for c in candidates}
    if _fold(name) in folded:
        return folded[_fold(name)]
    close = difflib.get_close_matches(str(name), [str(c) for c in candidates], n=1, cutoff=0.6)
    return close[0] if close else None


def _fold(name: Any) -> str:
    return str(name).replace("_", "").replace("-", "").lower()


@lru_cache(maxsize=256)
def _pattern(source: str) -> re.Pattern[str] | None:
    r"""The compiled pattern, or None for one ``re`` cannot read.

    JSON Schema patterns are ECMA-262, and some legal ones -- ``\p{L}`` -- are an
    error in Python; a schema the harness cannot check is not the model's fault.
    """
    try:
        return re.compile(source)
    except re.error:
        return None


def _shape(schema: Any, depth: int = 0) -> str:
    """A schema as a compact signature: ``{question: string, options?: string[]}``."""
    if not isinstance(schema, dict):
        return "any"
    t = schema.get("type")
    if "const" in schema:
        return json.dumps(schema["const"], ensure_ascii=False)
    if isinstance(schema.get("enum"), list) and 0 < len(schema["enum"]) <= 5:
        return " | ".join(json.dumps(v, ensure_ascii=False) for v in schema["enum"])
    if isinstance(t, list):
        return " | ".join("null" if one == "null" else _shape({**schema, "type": one}, depth) for one in t)
    if t == "array":
        inner = _shape(schema.get("items"), depth + 1) if isinstance(schema.get("items"), dict) else "any"
        return f"({inner})[]" if " | " in inner else f"{inner}[]"
    if t == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        if not props:
            return "object"
        if depth >= 1:
            return "{...}"
        required = set(schema.get("required", []))
        fields: list[str] = []
        size = 2
        for key, sub in props.items():
            field = f"{key}{'' if key in required else '?'}: {_shape(sub, depth + 1)}"
            if fields and size + len(field) > _SHAPE_CHARS:
                fields.append("...")
                break
            fields.append(field)
            size += len(field) + 2
        return "{" + ", ".join(fields) + "}"
    return t if isinstance(t, str) else "any"


def _listing(items: list[str], limit: int) -> str:
    shown = ", ".join(items[:limit])
    return f"{shown}, and {len(items) - limit} more" if len(items) > limit else shown


def _clip(text: str) -> str:
    return text if len(text) <= _PREVIEW_CHARS else text[:_PREVIEW_CHARS] + "..."


def _items(count: int) -> str:
    return f"{count} item" if count == 1 else f"{count} items"


def _dot(path: str, key: Any) -> str:
    return f"{path}.{key}" if path else str(key)


def _index(path: str, index: int) -> str:
    return f"{path}[{index}]" if path else f"[{index}]"


__all__ = ["ParamError", "cast_params", "render_param_errors", "validate_params"]
