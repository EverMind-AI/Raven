"""Schema-match test: Pydantic models (raven.rpc.models) ↔ OpenRPC schema.

This test is the CI guardrail that catches drift between the single source of
truth (``rpc-schema/openrpc.json``) and the Python-side Pydantic models.

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
"anyOf [T, null]" wrapper vs schema's bare "T" + required-list).  Nullability is
therefore NOT checked here: both sides are stripped of their null branch before
the diff, so a nullable Pydantic field over a non-nullable schema one passes.
Pin that per method against real handler output instead -- see
``test_every_set_mode_answer_satisfies_the_published_result_schema``.  Instead we
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

from raven.rpc.models import METHOD_MODELS

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "rpc-schema" / "openrpc.json"


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
        if isinstance(node["type"], list):
            # Two shapes share this spelling. ``[T, "null"]`` is a nullable T --
            # the schema's way of writing what Pydantic emits as
            # ``anyOf: [T, null]`` and ``_strip_null_anyof`` reduces to T, so
            # reduce it the same way or the two sides can never agree. Anything
            # wider is JsonValue's multi-typed primitive node; collapse that.
            non_null = [t for t in node["type"] if t != "null"]
            if len(non_null) == 1:
                out["type"] = non_null[0]
            else:
                out["any"] = True
        else:
            out["type"] = node["type"]
    if "enum" in node:
        # Nullability is not compared (see the module docstring), so a null
        # listed among an enum's values is left out the way a null branch is.
        out["enum"] = sorted(v for v in node["enum"] if v is not None)
    if "const" in node:
        out["const"] = node["const"]
    # Read the reduced type, not the raw one: a nullable ``["object", "null"]``
    # still has properties to descend into.
    if out.get("type") == "array" and "items" in node:
        out["items"] = _normalize_oas_type(node["items"], schema, _seen)
    if out.get("type") == "object":
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

    from raven.rpc.models import TurnEvent

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


def test_schema_match_the_boundary_a_suspended_dag_node_emits(schema: dict[str, Any]) -> None:
    """The exact `turn.started` payload `announce_dag_exception` puts on the wire
    must validate against the published contract AND the Pydantic model.

    `turn.started` was declared on both sides and wired into neither union, so
    the sweep above never reached it. It drifted the moment the judge landed:
    `raven/agent/subagent/manager.py` began marking a suspended node
    `status="exception"` with a `node_id`, the schema still said
    `enum: [ok, error]` with `additionalProperties: false`, and the Pydantic
    payload had no `delegated` field at all under `extra="forbid"`. Nothing
    validates an outbound event, so it reached clients anyway -- but any
    consumer that DOES validate rejects the whole event, not the unknown field,
    and loses the boundary that advances the turn counter.

    Built from the marker the manager actually writes, so the day a fourth
    status or a fifth field is added there, this fails here rather than in a
    client nobody is watching.
    """
    import jsonschema
    from pydantic import TypeAdapter

    from raven.rpc.models import TurnEvent

    # The shape `announce_dag_exception` builds, verbatim.
    mark = {"kind": "dag", "label": "run-7", "status": "exception", "run_id": "run-7", "node_id": "n2"}
    event = {"type": "turn.started", "payload": {"turn_id": "t1", "delegated": {**mark, "content": "..."}}}

    inline = {**schema["components"]["schemas"]["TurnStartedEvent"], "components": schema["components"]}
    jsonschema.validate(event, inline)
    TypeAdapter(TurnEvent).validate_python(event)

    # And a direct chat's turn, which tags the payload with its addressee. The
    # shape is the one `raven/rpc/methods/turn.py` puts in `direct_targets`:
    # `agent` and `handle`. This event declared its own `target` inline instead
    # of pointing at `DirectTarget` like every other event does, and the copy
    # said `instance` -- a key the runtime has never sent -- under
    # `additionalProperties: false`. So every tagged boundary was invalid to a
    # validating consumer, for a field it does not even control.
    tagged = {
        "type": "turn.started",
        "payload": {"turn_id": "t1", "target": {"agent": "raven-code", "handle": "h1"}},
    }
    jsonschema.validate(tagged, inline)
    TypeAdapter(TurnEvent).validate_python(tagged)

    # The plain boundary, which most turns are: no delegation, no addressee.
    plain = {"type": "turn.started", "payload": {"turn_id": "t1"}}
    jsonschema.validate(plain, inline)
    TypeAdapter(TurnEvent).validate_python(plain)

    # And the fourth status: a stall notice about a node that is still running
    # (`announce_dag_exception(informational=True)`). Emitted as `notice` so a
    # client never draws a live node as failed; declared on both sides for the
    # same reason the third one is.
    notice = {**mark, "status": "notice"}
    for shape in (
        {"type": "turn.started", "payload": {"turn_id": "t1", "delegated": {**notice, "content": "..."}}},
        {"type": "subagent.delivered", "payload": {**notice, "content": "..."}},
    ):
        jsonschema.validate(shape, {**schema["components"]["schemas"]["TurnEvent"], "components": schema["components"]})
        TypeAdapter(TurnEvent).validate_python(shape)

    # And the fifth: a spawn stopped before it finished (the manager's
    # CancelledError branch announces it). Declared on both sides so a client
    # draws a stop as a stop, not as the result the run never returned.
    cancelled = {"kind": "spawn", "label": "poster", "status": "cancelled", "content": "..."}
    for shape in (
        {"type": "turn.started", "payload": {"turn_id": "t1", "delegated": cancelled}},
        {"type": "subagent.delivered", "payload": cancelled},
    ):
        jsonschema.validate(shape, {**schema["components"]["schemas"]["TurnEvent"], "components": schema["components"]})
        TypeAdapter(TurnEvent).validate_python(shape)


@pytest.mark.parametrize("truncated", [True, False, None])
def test_the_output_cut_flag_survives_each_published_delivery_shape(schema: dict[str, Any], truncated) -> None:
    import jsonschema

    from raven.rpc.models import SubagentDeliveredPayload, TranscriptDelegated, TurnStartedDelegated

    mark = {"kind": "dag", "label": "r1", "status": "exception", "output": "the node's words"}
    if truncated is not None:
        mark["output_truncated"] = truncated
    components = schema["components"]
    cases = [
        (TranscriptDelegated, components["schemas"]["TranscriptDelegated"], mark),
        (
            TurnStartedDelegated,
            components["schemas"]["TurnStartedEvent"]["properties"]["payload"]["properties"]["delegated"],
            {**mark, "content": "model brief"},
        ),
        (
            SubagentDeliveredPayload,
            components["schemas"]["SubagentDeliveredEvent"]["properties"]["payload"],
            {**mark, "content": "model brief"},
        ),
    ]
    for model, published, data in cases:
        actual = model.model_validate(data).model_dump(exclude_none=True)
        assert actual.get("output_truncated") is truncated
        jsonschema.validate(actual, {**published, "components": components})


def test_schema_match_the_stall_notice_progress_event(schema: dict[str, Any]) -> None:
    """`dag.node_stalled` is what the stall watcher's progress event becomes on
    the wire (`raven.rpc.spine._DAG_WIRE_EVENT`); it is the only frame a bound
    foreground run's panel hears, so it has to be declared on both sides."""
    import jsonschema
    from pydantic import TypeAdapter

    from raven.rpc.models import TurnEvent
    from raven.rpc.spine import _DAG_WIRE_EVENT, _dag_payload

    payload = _dag_payload("dag_node_stalled", {"run_id": "r1", "node": "n", "quiet_ms": 600000})
    event = {"type": _DAG_WIRE_EVENT["dag_node_stalled"], "payload": payload}
    jsonschema.validate(event, {**schema["components"]["schemas"]["TurnEvent"], "components": schema["components"]})
    TypeAdapter(TurnEvent).validate_python(event)


def test_schema_match_the_frames_a_run_that_branches_emits(schema: dict[str, Any]) -> None:
    """Every frame a graph with decisions and guards puts on the wire is valid
    against the published contract AND the Pydantic model.

    The payload-fields sweep below compares only each payload's own property
    names, so the objects nested inside it -- a started frame's nodes, a
    completed frame's summary and files, a node frame's status enum -- were never
    held to both sides by what a run really sends. Both declare
    ``additionalProperties: false`` / ``extra="forbid"``, so one side missing a
    field or a status makes a validating consumer drop the whole frame, not the
    unknown key.

    Built through ``_dag_payload`` from the dicts the runner emits, so a field the
    runner sends and the sink forgets to carry is a different test's failure.
    """
    import jsonschema
    from pydantic import TypeAdapter

    from raven.rpc.models import TurnEvent
    from raven.rpc.spine import _DAG_WIRE_EVENT, _dag_payload

    summary = {"total": 3, "completed": 2, "failed": 0, "skipped": 0, "cancelled": 0, "not_executed": 1}
    frames = [
        (
            "dag_run_started",
            {
                "run_id": "r1",
                "nodes": [
                    {"id": "triage", "subagent": "x", "depends_on": [], "routes": ["bug", "docs"], "select": "one"},
                    {"id": "fix", "subagent": "x", "depends_on": ["triage"], "when": "triage.bug"},
                    {
                        "id": "audit",
                        "subagent": "x",
                        "depends_on": ["fix"],
                        "routes": ["a"],
                        "select": "many",
                        "when": "fix.done",
                    },
                ],
            },
        ),
        ("dag_node_updated", {"run_id": "r1", "node": "fix", "status": "not_executed", "reason": "when_false"}),
        ("dag_node_updated", {"run_id": "r1", "node": "fix", "status": "not_executed", "reason": "when_unknown"}),
        (
            "dag_node_updated",
            {"run_id": "r1", "node": "fix", "status": "not_executed", "reason": "upstream_not_executed"},
        ),
        (
            "dag_node_updated",
            {
                "run_id": "r1",
                "node": "triage",
                "status": "completed",
                "started_at": 1,
                "ended_at": 2,
                "selected_routes": ["docs"],
                "route_evidence": "the reporter asks how to configure it",
            },
        ),
        ("dag_node_updated", {"run_id": "r1", "node": "audit", "status": "completed", "selected_routes": []}),
        (
            "dag_run_completed",
            {
                "run_id": "r1",
                "manifest": {
                    "dir": "/w/r1",
                    "summary": summary,
                    "files": [
                        {
                            "node": "triage",
                            "status": "completed",
                            "output_file": "/w/r1/triage.out.md",
                            "selected_routes": ["docs"],
                            "route_evidence": "the reporter asks how to configure it",
                        },
                        {"node": "audit", "status": "completed", "selected_routes": []},
                        {"node": "fix", "status": "not_executed", "reason": "when_false"},
                        {"node": "patch", "status": "not_executed", "reason": "when_unknown"},
                        {"node": "notify", "status": "not_executed", "reason": "upstream_not_executed"},
                    ],
                },
            },
        ),
    ]
    mapping = schema["components"]["schemas"]["TurnEvent"]["discriminator"]["mapping"]
    for name, payload in frames:
        event = {"type": _DAG_WIRE_EVENT[name], "payload": _dag_payload(name, payload)}
        # Against the variant the discriminator names, not the union: a oneOf
        # failure says only "not valid under any of the given schemas".
        variant = {**_resolve_ref(mapping[event["type"]], schema), "components": schema["components"]}
        jsonschema.validate(event, variant)
        TypeAdapter(TurnEvent).validate_python(event)


def test_schema_match_the_frames_a_reassigned_node_emits(schema: dict[str, Any]) -> None:
    """A node handed to another agent reports the attempt that runs it now: which
    agent, on which handle, whether that handle was minted, and every agent that
    tried the node. Built through ``_dag_payload`` from the dicts the runner
    emits, so a field the runner sends and the sink forgets to carry fails here.

    ``instance: null`` is a value, not an omission: a stateless retry clears the
    handle the node was carrying, and a sink that dropped it for looking empty
    would leave a client showing a session the node is no longer on.
    """
    import jsonschema
    from pydantic import TypeAdapter

    from raven.rpc.models import TurnEvent
    from raven.rpc.spine import _DAG_WIRE_EVENT, _dag_payload

    frames = [
        (
            "dag_node_updated",
            {
                "run_id": "r1",
                "node": "fix",
                "status": "running",
                "started_at": 10,
                "subagent": "beta",
                "instance": "fix-minted",
                "instance_auto": True,
                "attempt_agents": ["alpha", "beta"],
            },
        ),
        (
            "dag_node_updated",
            {
                "run_id": "r1",
                "node": "fix",
                "status": "running",
                "started_at": 20,
                "subagent": "gamma",
                "instance": None,
                "instance_auto": False,
                "attempt_agents": ["alpha", "beta", "gamma"],
            },
        ),
        (
            "dag_run_completed",
            {
                "run_id": "r1",
                "manifest": {
                    "dir": "/w/r1",
                    "summary": {"total": 1, "completed": 1},
                    "files": [
                        {
                            "node": "fix",
                            "status": "completed",
                            "subagent": "gamma",
                            "instance": None,
                            "instance_auto": False,
                            "attempt_agents": ["alpha", "beta", "gamma"],
                        }
                    ],
                },
            },
        ),
    ]
    mapping = schema["components"]["schemas"]["TurnEvent"]["discriminator"]["mapping"]
    for name, payload in frames:
        event = {"type": _DAG_WIRE_EVENT[name], "payload": _dag_payload(name, payload)}
        variant = {**_resolve_ref(mapping[event["type"]], schema), "components": schema["components"]}
        jsonschema.validate(event, variant)
        TypeAdapter(TurnEvent).validate_python(event)

    wire_moved = _dag_payload("dag_node_updated", frames[0][1])
    assert wire_moved["subagent"] == "beta"
    assert wire_moved["instance"] == "fix-minted"
    assert wire_moved["instance_auto"] is True
    assert wire_moved["attempt_agents"] == ["alpha", "beta"]
    wire_stateless = _dag_payload("dag_node_updated", frames[1][1])
    assert "instance" in wire_stateless and wire_stateless["instance"] is None
    assert wire_stateless["attempt_agents"] == ["alpha", "beta", "gamma"]
    wire_completed = _dag_payload("dag_run_completed", frames[2][1])
    assert wire_completed["files"][0]["attempt_agents"] == ["alpha", "beta", "gamma"]


def test_schema_match_a_plain_frame_keeps_the_keys_it_had() -> None:
    """A run that never reassigns a node must reach the wire exactly as before.

    The runner stamps ``subagent``, ``instance`` and ``instance_auto`` on every
    completed file, and carries them on every update only once the node has
    moved. A sink that forwarded them unconditionally would add a null handle
    and a false flag to every node of every run.
    """
    from raven.rpc.spine import _dag_payload

    updated = _dag_payload("dag_node_updated", {"run_id": "r1", "node": "a", "status": "running", "started_at": 1})
    assert updated == {"run_id": "r1", "node": "a", "status": "running", "started_at": 1}

    completed = _dag_payload(
        "dag_run_completed",
        {
            "run_id": "r1",
            "manifest": {
                "dir": "/w/r1",
                "summary": {"total": 1},
                "files": [
                    {"node": "a", "status": "completed", "subagent": "alpha", "instance": None, "instance_auto": False}
                ],
            },
        },
    )
    assert completed["files"] == [{"node": "a", "status": "completed"}]


def test_schema_match_a_reassigned_node_reads_back_through_every_reader_model(schema: dict[str, Any]) -> None:
    """The readers that report an attempt history answer with keys the published
    models must accept, or a validating client loses the whole response.

    One node's record, read three ways: ``dag.get``'s snapshot node, the task
    board's node and the instance registry row it is collapsed from.
    """
    import jsonschema

    from raven.rpc.models import DagSnapshotNode, InstanceRow, SubagentCall, TaskNode

    components = schema["components"]
    history = ["alpha", "alpha", "beta"]
    cases = [
        (
            DagSnapshotNode,
            "DagSnapshotNode",
            {
                "node": "fix",
                "status": "running",
                "subagent": "beta",
                "instance": "fix-minted",
                "instance_auto": True,
                "attempt_agents": history,
            },
        ),
        (
            TaskNode,
            "TaskNode",
            {
                "node_id": "fix",
                "agent": "beta",
                "instance": "fix-minted",
                "instance_auto": True,
                "status": "running",
                "depends_on": [],
                "files": [],
                "attempt_agents": history,
            },
        ),
        (
            InstanceRow,
            "InstanceRow",
            {
                "sessionKey": "s1",
                "agent": "beta",
                "handle": "fix",
                "kind": "dag-node",
                "nodeInstance": None,
                "nodeInstanceAuto": False,
                "attemptAgents": history,
            },
        ),
        (
            SubagentCall,
            "SubagentCall",
            {
                "id": "r1/fix",
                "kind": "dag",
                "label": "fix",
                "status": "run",
                "message_count": 2,
                "agent": "beta",
                "instance": "fix-minted",
                "instance_auto": True,
                "attempt_agents": history,
            },
        ),
    ]
    for model, published, data in cases:
        dumped = model.model_validate(data).model_dump(exclude_unset=True, by_alias=True)
        jsonschema.validate(dumped, {**components["schemas"][published], "components": components})

    # And the history is not an excuse to make the keys required: a plain
    # record must still validate without any of them.
    DagSnapshotNode.model_validate({"node": "a", "status": "pending"})
    TaskNode.model_validate({"node_id": "a", "agent": "alpha", "status": "pending", "depends_on": [], "files": []})
    InstanceRow.model_validate({"sessionKey": "s1", "agent": "alpha", "handle": "a", "kind": "dag-node"})


async def test_schema_match_a_stateless_reassigned_node_reads_back_with_a_null_handle(
    schema: dict[str, Any], tmp_path: Path
) -> None:
    """A moved node whose current attempt keeps no session is an ordinary state,
    and the readers say so with ``instance: null``.

    Both published components declared ``instance`` as a plain string, so a
    validating client rejected the whole answer -- not the null, the response.
    Validated from what the readers really answer: ``dag.get``'s snapshot node
    off its run dir, and ``subagent.list``'s row as the node registry reports it.
    """
    import json

    import jsonschema

    from raven.agent.subagent.dag_reader import read_run
    from raven.agent.subagent.prompt_backend import LocalFileBackend
    from raven.rpc.models import SubagentCall

    components = schema["components"]
    run_id = "20260812T120000Z-deadbeef"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    (run_dir / "graph.json").write_text(
        json.dumps(
            {
                "task_summary": "moved",
                "nodes": [
                    {
                        "id": "fix",
                        "subagent": "alpha",
                        "node_summary": "repair the tokenizer",
                        "prompt_template": "Fix {{ inputs.k }}",
                        "inputs": {"k": "the parser"},
                        "depends_on": [],
                    }
                ],
            }
        )
    )
    # Every other field the reader reports has a value, so the stateless handle
    # is the only thing a validating client could object to.
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "fix": {
                    "status": "failed",
                    "subagent": "beta",
                    "instance": None,
                    "instance_auto": False,
                    "attempt_agents": ["alpha", "beta"],
                    "started_at": 1000,
                    "ended_at": 2000,
                    "prompt_file": "/w/fix.prompt.md",
                    "output_file": "/w/fix.out.md",
                    "error": "beta could not parse it",
                }
            }
        )
    )

    run = await read_run(LocalFileBackend(), str(tmp_path), run_id, str(tmp_path / "nodes"))
    snapshot_node = run["files"][0]
    assert snapshot_node["instance"] is None
    # The reader answers two keys these models have never declared -- the run's
    # ``nodes_root`` and a file's ``memory_file`` -- which is an older gap of its
    # own. What the contract does declare is validated as a client reads it.
    published = components["schemas"]["DagSnapshotNode"]
    jsonschema.validate(
        {key: value for key, value in snapshot_node.items() if key in published["properties"]},
        {**published, "components": components},
    )

    # The row `subagent.list` reports for that same node, with the stateless
    # handle its registry row carries.
    call = SubagentCall.model_validate(
        {
            "id": f"{run_id}/fix",
            "kind": "dag",
            "label": "fix",
            "status": "run",
            "message_count": 1,
            "agent": "beta",
            "instance": None,
            "instance_auto": False,
            "attempt_agents": ["alpha", "beta"],
        }
    ).model_dump(exclude_unset=True)
    jsonschema.validate(call, {**components["schemas"]["SubagentCall"], "components": components})


def _where_shapes_part(oas: Any, pyd: Any, path: str = "") -> list[str]:
    """Every place two normalised shapes disagree, as paths a reader can go to."""
    if not (isinstance(oas, dict) and isinstance(pyd, dict)):
        return [] if oas == pyd else [f"{path}: schema={oas} vs pydantic={pyd}"]
    out: list[str] = []
    for key in sorted(set(oas) | set(pyd)):
        if key not in oas:
            out.append(f"{path}/{key}: only in pydantic")
        elif key not in pyd:
            out.append(f"{path}/{key}: only in schema")
        else:
            out += _where_shapes_part(oas[key], pyd[key], f"{path}/{key}")
    return out


@pytest.mark.parametrize(
    "event",
    [
        "DagRunStartedEvent",
        "DagNodeUpdatedEvent",
        "DagRunReplannedEvent",
        "DagNodeStalledEvent",
        "DagRunCompletedEvent",
    ],
)
def test_schema_match_a_dag_event_agrees_down_to_its_nested_objects(event: str, schema: dict[str, Any]) -> None:
    """The payload-fields sweep below stops at a payload's own property names, so
    the objects nested in a dag frame -- a started frame's nodes, a completed
    frame's summary and files -- and every enum on them were compared nowhere.
    Both sides forbid extra keys, so a field or a status one side lacks makes a
    validating consumer drop the whole frame. Walked the way the method sweep
    walks a result, naming each place the two part."""
    import raven.rpc.models as models

    oas = _normalize_oas_type({"$ref": f"#/components/schemas/{event}"}, schema)
    pyd_schema = getattr(models, event).model_json_schema()
    pyd = _normalize_pyd_type(pyd_schema, pyd_schema)

    assert not _where_shapes_part(oas, pyd), f"drift in {event}: " + "; ".join(_where_shapes_part(oas, pyd))


def test_schema_match_turn_event_payload_fields(schema: dict[str, Any]) -> None:
    """Every variant's payload must declare the same fields on both sides.

    The union test above compares only the *set of variants*, which is why a
    field added to one side alone survived it: ``tool.complete`` gained
    ``file_change`` at the emit site and in the Pydantic model with no schema
    entry, and nothing failed. That is not a cosmetic drift -- both sides declare
    ``additionalProperties: false`` / ``extra="forbid"``, so a consumer
    validating against the schema rejects the *whole event*, not just the
    unknown field, and a client loses the tool result entirely.

    Compares names only. Types are left to the per-method sweep: an event payload
    reuses the same component schemas, and a name mismatch is the failure this
    contract actually suffers.
    """
    from pydantic import TypeAdapter

    from raven.rpc.models import TurnEvent

    components = schema["components"]["schemas"]
    oas_mapping = components["TurnEvent"]["discriminator"]["mapping"]
    pyd = TypeAdapter(TurnEvent).json_schema()
    pyd_defs = pyd["$defs"]

    def properties(node: dict[str, Any], defs: dict[str, Any]) -> set[str]:
        payload = node["properties"].get("payload", {})
        if "$ref" in payload:
            payload = defs[payload["$ref"].split("/")[-1]]
        return set(payload.get("properties", {}))

    drift: dict[str, tuple[list[str], list[str]]] = {}
    for literal, ref in pyd["discriminator"]["mapping"].items():
        pyd_fields = properties(pyd_defs[ref.split("/")[-1]], pyd_defs)
        oas_fields = properties(components[oas_mapping[literal].split("/")[-1]], components)
        if pyd_fields != oas_fields:
            drift[literal] = (sorted(pyd_fields - oas_fields), sorted(oas_fields - pyd_fields))

    assert not drift, "payload field drift (pydantic-only, schema-only): " + repr(drift)


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
    -32010: "config_field_readonly",
    -32011: "config_validation_error",
    -32012: "not_supported_in_v01",
    -32013: "cli_command_failed",
    -32014: "cli_command_timeout",
    -32015: "not_dispatch_compatible",
    -32017: "subagent_not_found",
    -32018: "session_title_too_long",
    -32019: "subagent_not_ready",
    -32020: "trajectory_disabled",
    -32021: "entry_not_found",
    -32022: "cursor_expired",
    -32023: "revision_changed",
    -32024: "trajectory_task_not_found",
    -32025: "trajectory_task_limit",
    -32026: "bugreport_unavailable",
    -32027: "review_conflict",
    -32028: "bugreport_state",
    -32029: "imported_read_only",
    -32030: "imported_package_invalid",
    -32031: "imported_busy",
}


def test_every_spec_error_code_maps_back_to_its_class() -> None:
    from raven.rpc import errors

    for code, message in EXPECTED_ERROR_CODES.items():
        cls = errors.JSONRPC_ERROR_REGISTRY.get(code)
        assert cls is not None, f"{code} has no class in JSONRPC_ERROR_REGISTRY"
        assert cls.MESSAGE == message
        assert cls.__name__ in errors.__all__


def test_error_codes_match_spec(schema: dict[str, Any]) -> None:
    errors_component = schema["components"]["errors"]
    declared: dict[int, str] = {body["code"]: body["message"] for body in errors_component.values()}
    assert declared == EXPECTED_ERROR_CODES, (
        f"error code table drift:\n"
        f"  in-schema: {sorted(declared.items())}\n"
        f"  expected:  {sorted(EXPECTED_ERROR_CODES.items())}"
    )


@pytest.mark.slow
def test_a_model_is_built_when_first_used_not_at_import(tmp_path: Path) -> None:
    """A host validates only the methods its clients call, so each validator waits
    for its first use rather than all of them being built and held for the life of
    the process. Checked in a fresh interpreter, where no model has been used yet."""
    import subprocess
    import sys

    probe = (
        "import pydantic\n"
        "from raven.rpc import models\n"
        "classes = [c for c in vars(models).values()\n"
        "           if isinstance(c, type) and issubclass(c, pydantic.BaseModel) and c.__module__ == models.__name__]\n"
        "print('models:', len(classes) > 0)\n"
        "print('built at import:', [c.__name__ for c in classes if c.__pydantic_complete__][:5])\n"
        "params = models.SessionListParams.model_validate({'limit': 3})\n"
        "print('first use:', params.limit, models.SessionListParams.__pydantic_complete__)\n"
        "try:\n"
        "    models.SessionListParams.model_validate({'unexpected': 1})\n"
        "except pydantic.ValidationError as exc:\n"
        "    print('refused:', exc.errors()[0]['type'])\n"
    )
    done = subprocess.run(
        [sys.executable, "-P", "-c", probe], cwd=tmp_path, capture_output=True, text=True, timeout=120
    )

    assert "models: True" in done.stdout, done.stdout + done.stderr
    assert "built at import: []" in done.stdout, done.stdout + done.stderr
    assert "first use: 3 True" in done.stdout, done.stdout + done.stderr
    assert "refused: extra_forbidden" in done.stdout, done.stdout + done.stderr
