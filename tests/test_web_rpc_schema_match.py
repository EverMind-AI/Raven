"""Schema-match test for the web dialect: raven.web_rpc.models ↔ openrpc-web.json.

The same guardrail ``tests/test_rpc_schema_match.py`` provides for the terminal
contract, applied to the ``raven.*`` namespace the gateway's web dispatcher
serves. The two contracts live in separate files on purpose: ui-tui generates
its client from ``openrpc.json``, so the browser-only vocabulary must not leak
into it -- which is why this file compares ``WEB_METHOD_MODELS`` against
``rpc-schema/openrpc-web.json`` instead of growing the terminal schema.

The normalization helpers are the terminal test's own, imported rather than
copied, so the two dialects can never drift on what "matching" means.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from raven.web_rpc.models import WEB_METHOD_MODELS
from tests.test_rpc_schema_match import (
    _oas_object_properties,
    _oas_params_to_canonical,
    _pyd_object_properties,
    _pyd_params_to_canonical,
)

WEB_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "rpc-schema" / "openrpc-web.json"


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return json.loads(WEB_SCHEMA_PATH.read_text())


@pytest.fixture(scope="module")
def methods_by_name(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {m["name"]: m for m in schema["methods"]}


def test_method_set_matches(methods_by_name: dict[str, dict[str, Any]]) -> None:
    schema_names = set(methods_by_name.keys())
    pyd_names = set(WEB_METHOD_MODELS.keys())
    only_in_schema = schema_names - pyd_names
    only_in_pyd = pyd_names - schema_names
    assert not only_in_schema, f"methods in openrpc-web.json but not in WEB_METHOD_MODELS: {only_in_schema}"
    assert not only_in_pyd, f"methods in WEB_METHOD_MODELS but not in openrpc-web.json: {only_in_pyd}"


def test_every_method_is_namespaced() -> None:
    """The web contract holds the raven.* dialect and nothing else -- a method
    without the prefix belongs in the terminal contract (openrpc.json)."""
    unprefixed = {name for name in WEB_METHOD_MODELS if not name.startswith("raven.")}
    assert unprefixed == set(), f"non-raven.* methods do not belong in this contract: {sorted(unprefixed)}"


def _all_method_names() -> list[str]:
    schema = json.loads(WEB_SCHEMA_PATH.read_text())
    return sorted(m["name"] for m in schema["methods"])


@pytest.mark.parametrize("method_name", _all_method_names())
def test_method_params_match_schema(
    method_name: str,
    methods_by_name: dict[str, dict[str, Any]],
    schema: dict[str, Any],
) -> None:
    params_model, _ = WEB_METHOD_MODELS[method_name]
    oas = _oas_params_to_canonical(methods_by_name[method_name], schema)
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


@pytest.mark.parametrize("method_name", _all_method_names())
def test_method_result_matches_schema(
    method_name: str,
    methods_by_name: dict[str, dict[str, Any]],
    schema: dict[str, Any],
) -> None:
    _, result_model = WEB_METHOD_MODELS[method_name]
    result_schema_node = methods_by_name[method_name]["result"]["schema"]
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
