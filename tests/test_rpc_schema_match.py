"""Schema-match test: Pydantic models (raven.tui_rpc.models) ↔ OpenRPC schema.

This test is the CI guardrail that catches drift between the single source of
truth (``ui-tui/rpc-schema/openrpc.json``) and the Python-side Pydantic models.

Strategy
--------
For each method declared in the schema:

1.  Walk the schema's ``params`` list and the matching Pydantic
    ``<Method>Params`` model.  Compare field-by-field on:
    name, required, core JSON type, enum values, item type for arrays.
2.  Walk the schema's ``result.schema`` (after $ref resolution) and the
    matching Pydantic ``<Method>Result`` model schema.  Compare on the same
    field-level invariants.

We deliberately do NOT compare every nested key (titles, descriptions, Pydantic
"anyOf [T, null]" wrapper vs schema's bare "T" + required-list).  Instead we
*normalize* both sides to a canonical ``{name → field_descriptor}`` shape and
diff those.  This gives a readable assertion message on drift while staying
robust to Pydantic's stylistic choices.

A separate test (``test_method_set_matches``) ensures the *set* of methods in
schema and the ``METHOD_MODELS`` registry are identical (catches accidental
addition/removal on either side).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from raven.tui_rpc.models import METHOD_MODELS

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "ui-tui" / "rpc-schema" / "openrpc.json"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


@pytest.fixture(scope="module")
def methods_by_name(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {m["name"]: m for m in schema["methods"]}


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


def _resolve_ref(ref: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve a local ``#/components/schemas/Foo`` style $ref."""
    assert ref.startswith("#/"), f"only local refs supported, got {ref}"
    parts = ref[2:].split("/")
    node: Any = schema
    for p in parts:
        node = node[p]
    return node


def _resolve_pyd_ref(ref: str, root: dict[str, Any]) -> dict[str, Any]:
    """Resolve a Pydantic-style ``#/$defs/Foo`` ref against a Pydantic schema."""
    assert ref.startswith("#/"), f"only local refs supported, got {ref}"
    parts = ref[2:].split("/")
    node: Any = root
    for p in parts:
        node = node[p]
    return node


#: Refs that mean "any JSON value" on both sides — the OpenRPC side uses a
#: ``$ref`` to the ``JsonValue`` component, the Pydantic side emits an empty
#: schema for ``typing.Any``.  We canonicalize both to ``{"any": True}``.
_ANY_REFS = {"JsonValue"}


def _normalize_oas_type(
    node: dict[str, Any], schema: dict[str, Any], _seen: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """Normalize an OpenRPC schema fragment to a comparison-friendly form.

    Refs are *expanded* (cycle-safe via ``_seen``) so that inline objects and
    ref'd objects with the same shape compare equal.  Named component refs are
    preserved as ``{"object_name": Name, ...expanded...}`` so the test can
    detect when the wrong type is referenced.
    """
    if "$ref" in node:
        ref_name = node["$ref"].split("/")[-1]
        if ref_name in _ANY_REFS:
            return {"any": True}
        if ref_name in _seen:
            return {"ref_cycle": ref_name}
        target = _resolve_ref(node["$ref"], schema)
        expanded = _normalize_oas_type(target, schema, _seen | {ref_name})
        return expanded
    out: dict[str, Any] = {}
    if "type" in node:
        # OpenRPC declares JsonValue as a multi-typed primitive node; collapse.
        if isinstance(node["type"], list):
            out["any"] = True
        else:
            out["type"] = node["type"]
    if "enum" in node:
        out["enum"] = sorted(node["enum"])
    if "const" in node:
        out["const"] = node["const"]
    if node.get("type") == "array" and "items" in node:
        out["items"] = _normalize_oas_type(node["items"], schema, _seen)
    if node.get("type") == "object":
        if "properties" in node:
            out["properties"] = {
                pname: _normalize_oas_type(psub, schema, _seen) for pname, psub in node["properties"].items()
            }
            out["required"] = sorted(node.get("required", []))
        if "additionalProperties" in node:
            ap = node["additionalProperties"]
            if isinstance(ap, dict):
                out["values"] = _normalize_oas_type(ap, schema, _seen)
    if "oneOf" in node:
        branches = node["oneOf"]
        # ``oneOf: [JsonValue, null]`` is the schema's way of declaring an
        # optional-but-required-nullable field; collapse to "any".
        non_null = [b for b in branches if not (isinstance(b, dict) and b.get("type") == "null")]
        if len(branches) == 2 and len(non_null) == 1:
            inner = _normalize_oas_type(non_null[0], schema)
            if inner == {"any": True}:
                return {"any": True}
        out["oneOf_refs"] = sorted(
            (sub["$ref"].split("/")[-1] for sub in node["oneOf"] if "$ref" in sub),
        )
    return out


def _strip_null_anyof(field_schema: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Pydantic emits ``anyOf: [{type:X}, {type:null}]`` for ``X | None``.

    Strip the null branch and return ``(non_null_schema, was_nullable)``.
    """
    if "anyOf" in field_schema:
        branches = field_schema["anyOf"]
        non_null = [b for b in branches if b.get("type") != "null"]
        if len(non_null) == 1 and len(branches) == 2:
            return non_null[0], True
    return field_schema, False


def _normalize_pyd_type(
    node: dict[str, Any], root: dict[str, Any], _seen: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """Normalize a Pydantic-generated schema fragment for comparison.

    Resolves Pydantic ``$defs`` refs (cycle-safe) and expands them inline so
    that ref'd objects compare equal to schema-side inline objects.
    """
    node, _was_nullable = _strip_null_anyof(node)
    # Drop purely-decorative keys before deciding emptiness.
    stripped = {k: v for k, v in node.items() if k not in ("title", "description", "default")}
    if stripped == {}:
        return {"any": True}
    if "$ref" in stripped:
        ref_name = stripped["$ref"].split("/")[-1]
        if ref_name in _seen:
            return {"ref_cycle": ref_name}
        target = _resolve_pyd_ref(stripped["$ref"], root)
        return _normalize_pyd_type(target, root, _seen | {ref_name})
    out: dict[str, Any] = {}
    if "type" in stripped:
        out["type"] = stripped["type"]
    if "enum" in stripped:
        out["enum"] = sorted(stripped["enum"])
    if "const" in stripped:
        out["const"] = stripped["const"]
    if stripped.get("type") == "array" and "items" in stripped:
        out["items"] = _normalize_pyd_type(stripped["items"], root, _seen)
    if stripped.get("type") == "object":
        if "properties" in stripped:
            # Strip null-anyOf at the property level (mirrors strip-null in
            # the field-level normalization at _pyd_object_properties).
            props_out: dict[str, dict[str, Any]] = {}
            local_required = set(stripped.get("required", []))
            for pname, psub in stripped["properties"].items():
                non_null, was_nullable = _strip_null_anyof(psub)
                props_out[pname] = _normalize_pyd_type(non_null, root, _seen)
                if was_nullable:
                    local_required.discard(pname)
            out["properties"] = props_out
            out["required"] = sorted(local_required)
        ap = stripped.get("additionalProperties")
        if ap is True:
            out["values"] = {"any": True}
        elif isinstance(ap, dict):
            out["values"] = _normalize_pyd_type(ap, root, _seen)
        # ap is False (extra='forbid' default) or absent → no "values"
    if "oneOf" in stripped:
        out["oneOf_refs"] = sorted(
            (sub["$ref"].split("/")[-1] for sub in stripped["oneOf"] if "$ref" in sub),
        )
    return out


def _oas_params_to_canonical(method: dict[str, Any], schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return ``{param_name → {required: bool, schema: normalized}}``."""
    out: dict[str, dict[str, Any]] = {}
    for p in method.get("params", []):
        out[p["name"]] = {
            "required": bool(p.get("required", False)),
            "schema": _normalize_oas_type(p["schema"], schema),
        }
    return out


def _pyd_params_to_canonical(
    model: type[BaseModel],
) -> dict[str, dict[str, Any]]:
    """Return ``{field_name → {required: bool, schema: normalized}}``.

    "required" means the field has no default (Pydantic-required AND non-null).
    """
    pyd_schema = model.model_json_schema()
    required_set = set(pyd_schema.get("required", []))
    properties = pyd_schema.get("properties", {})
    out: dict[str, dict[str, Any]] = {}
    for fname, fschema in properties.items():
        is_required = fname in required_set
        non_null, was_nullable = _strip_null_anyof(fschema)
        out[fname] = {
            "required": is_required and not was_nullable,
            "schema": _normalize_pyd_type(non_null, pyd_schema),
        }
    return out


def _oas_object_properties(
    obj_schema: dict[str, Any], schema: dict[str, Any]
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """For an OpenRPC object schema (after $ref resolution), return
    ``({prop_name → normalized}, required_set)``."""
    # Follow a single layer of $ref if present.
    if "$ref" in obj_schema:
        obj_schema = _resolve_ref(obj_schema["$ref"], schema)
    properties = obj_schema.get("properties", {})
    required_set = set(obj_schema.get("required", []))
    out: dict[str, dict[str, Any]] = {}
    for name, sub in properties.items():
        out[name] = _normalize_oas_type(sub, schema)
    return out, required_set


def _pyd_object_properties(
    model: type[BaseModel],
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    pyd_schema = model.model_json_schema()
    required_set = set(pyd_schema.get("required", []))
    properties = pyd_schema.get("properties", {})
    out: dict[str, dict[str, Any]] = {}
    for fname, fschema in properties.items():
        non_null, was_nullable = _strip_null_anyof(fschema)
        out[fname] = _normalize_pyd_type(non_null, pyd_schema)
        if was_nullable:
            required_set.discard(fname)
    return out, required_set


# ---------------------------------------------------------------------------
# Method-set parity
# ---------------------------------------------------------------------------


def test_method_set_matches(methods_by_name: dict[str, dict[str, Any]]) -> None:
    """The set of method names declared in schema must equal METHOD_MODELS keys."""
    schema_names = set(methods_by_name.keys())
    pyd_names = set(METHOD_MODELS.keys())
    only_in_schema = schema_names - pyd_names
    only_in_pyd = pyd_names - schema_names
    assert not only_in_schema, f"methods in schema but not in METHOD_MODELS: {only_in_schema}"
    assert not only_in_pyd, f"methods in METHOD_MODELS but not in schema: {only_in_pyd}"


# ---------------------------------------------------------------------------
# Parametrized per-method drift checks
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def method_names(methods_by_name: dict[str, dict[str, Any]]) -> list[str]:
    return sorted(methods_by_name.keys())


def _check_params_drift(method_name: str, method: dict[str, Any], schema: dict[str, Any]) -> None:
    params_model, _ = METHOD_MODELS[method_name]
    oas = _oas_params_to_canonical(method, schema)
    pyd = _pyd_params_to_canonical(params_model)
    assert set(oas.keys()) == set(pyd.keys()), (
        f"drift in {method_name}.params: schema params {sorted(oas)} vs pydantic fields {sorted(pyd)}"
    )
    for name in oas:
        assert oas[name]["required"] == pyd[name]["required"], (
            f"drift in {method_name}.params.{name}.required: "
            f"schema={oas[name]['required']} vs pydantic={pyd[name]['required']}"
        )
        assert oas[name]["schema"] == pyd[name]["schema"], (
            f"drift in {method_name}.params.{name}.schema: "
            f"schema={oas[name]['schema']} vs pydantic={pyd[name]['schema']}"
        )


def _check_result_drift(method_name: str, method: dict[str, Any], schema: dict[str, Any]) -> None:
    _, result_model = METHOD_MODELS[method_name]
    result_schema_node = method["result"]["schema"]
    oas_props, oas_required = _oas_object_properties(result_schema_node, schema)
    pyd_props, pyd_required = _pyd_object_properties(result_model)
    assert set(oas_props.keys()) == set(pyd_props.keys()), (
        f"drift in {method_name}.result: schema properties {sorted(oas_props)} vs pydantic fields {sorted(pyd_props)}"
    )
    assert oas_required == pyd_required, (
        f"drift in {method_name}.result.required: schema={sorted(oas_required)} vs pydantic={sorted(pyd_required)}"
    )
    for name in oas_props:
        assert oas_props[name] == pyd_props[name], (
            f"drift in {method_name}.result.{name}: schema={oas_props[name]} vs pydantic={pyd_props[name]}"
        )


# Three explicit smoke tests called out in the implementation brief.  These
# also act as red-stage starting points if the parametrized suite below
# regresses.


def test_schema_match_cli_dispatch(methods_by_name: dict[str, dict[str, Any]], schema: dict[str, Any]) -> None:
    method = methods_by_name["cli.dispatch"]
    _check_params_drift("cli.dispatch", method, schema)
    _check_result_drift("cli.dispatch", method, schema)


def test_schema_match_session_create(methods_by_name: dict[str, dict[str, Any]], schema: dict[str, Any]) -> None:
    method = methods_by_name["session.create"]
    _check_params_drift("session.create", method, schema)
    _check_result_drift("session.create", method, schema)


def test_schema_match_turn_event_discriminated_union(schema: dict[str, Any]) -> None:
    """TurnEvent must be a discriminated union with ``type`` as the property.

    Compares the OpenRPC component schema's ``oneOf`` refs to the Pydantic-
    emitted ``oneOf`` refs for the ``TurnEvent`` Annotated alias.
    """
    from pydantic import TypeAdapter

    from raven.tui_rpc.models import TurnEvent

    oas = schema["components"]["schemas"]["TurnEvent"]
    assert oas.get("discriminator", {}).get("propertyName") == "type"
    oas_refs = sorted(sub["$ref"].split("/")[-1] for sub in oas["oneOf"])

    pyd = TypeAdapter(TurnEvent).json_schema()
    assert pyd.get("discriminator", {}).get("propertyName") == "type"
    pyd_refs = sorted(sub["$ref"].split("/")[-1] for sub in pyd["oneOf"])

    assert oas_refs == pyd_refs, f"TurnEvent variant set drift: schema={oas_refs} vs pydantic={pyd_refs}"
    # Mapping (type-literal → variant name) must align as well.
    oas_mapping = oas["discriminator"]["mapping"]
    pyd_mapping = pyd["discriminator"]["mapping"]
    oas_normal = {k: v.split("/")[-1] for k, v in oas_mapping.items()}
    pyd_normal = {k: v.split("/")[-1] for k, v in pyd_mapping.items()}
    assert oas_normal == pyd_normal, f"TurnEvent discriminator mapping drift: schema={oas_normal} vs pyd={pyd_normal}"


# Parametrized full-suite sweep — one test instance per method.  This is the
# primary CI guard; the three explicit tests above are sentinels with extra
# context-rich failure modes.


def _all_method_names() -> list[str]:
    schema = json.loads(SCHEMA_PATH.read_text())
    return sorted(m["name"] for m in schema["methods"])


@pytest.mark.parametrize("method_name", _all_method_names())
def test_method_params_match_schema(
    method_name: str,
    methods_by_name: dict[str, dict[str, Any]],
    schema: dict[str, Any],
) -> None:
    method = methods_by_name[method_name]
    _check_params_drift(method_name, method, schema)


@pytest.mark.parametrize("method_name", _all_method_names())
def test_method_result_matches_schema(
    method_name: str,
    methods_by_name: dict[str, dict[str, Any]],
    schema: dict[str, Any],
) -> None:
    method = methods_by_name[method_name]
    _check_result_drift(method_name, method, schema)


# ---------------------------------------------------------------------------
# Error code parity (specs §4)
# ---------------------------------------------------------------------------


EXPECTED_ERROR_CODES = {
    -32001: "session_not_found",
    -32002: "session_locked",
    -32003: "turn_in_progress",
    -32004: "mcp_server_not_connected",
    -32005: "mcp_tool_call_failed",
    -32006: "skill_not_found",
    -32007: "skill_pin_conflict",
    -32008: "model_not_available",
    -32009: "model_switch_in_turn",
    -32010: "config_field_readonly",
    -32011: "config_validation_error",
    -32012: "not_supported_in_v01",
    -32013: "cli_command_failed",
    -32014: "cli_command_timeout",
    -32015: "not_dispatch_compatible",
}


def test_error_codes_match_spec(schema: dict[str, Any]) -> None:
    errors_component = schema["components"]["errors"]
    declared: dict[int, str] = {body["code"]: body["message"] for body in errors_component.values()}
    assert declared == EXPECTED_ERROR_CODES, (
        f"error code table drift:\n"
        f"  in-schema: {sorted(declared.items())}\n"
        f"  expected:  {sorted(EXPECTED_ERROR_CODES.items())}"
    )
