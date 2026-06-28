"""Tests for tui_rpc system.* handlers, dispatcher, and error mapping.

Covers:
- system.hello / system.ping / system.version handler contracts
- Dispatcher JSON-RPC 2.0 framing & error mapping (-32600, -32601, -32700, -32603)
- ConfigValidationError (-32011) raised for non-semver client_version

These tests treat handlers as `async def handler(params: dict) -> dict` and the
dispatcher as `async def dispatch(frame: dict) -> dict`. Pydantic validation
runs inside handlers (validation errors bubble up as RpcError subclasses).
"""

from __future__ import annotations

import time

import pytest

from raven.tui_rpc.dispatcher import Dispatcher
from raven.tui_rpc.errors import ConfigValidationError
from raven.tui_rpc.methods.system import (
    register_system_methods,
    system_hello,
    system_ping,
    system_version,
)

# ---------------------------------------------------------------------------
# system.* handler tests (direct calls, no dispatcher)
# ---------------------------------------------------------------------------


async def test_hello_returns_versions():
    result = await system_hello({"client_version": "0.1.0"})
    assert "server_version" in result
    assert isinstance(result["server_version"], str)
    assert "server_capabilities" in result
    assert isinstance(result["server_capabilities"], list)
    assert "jsonrpc-2.0" in result["server_capabilities"]
    assert "session" in result
    assert result["session"]["default_channel"] == "tui"
    assert result["session"]["default_session_key"].startswith("tui:")


async def test_hello_rejects_invalid_semver():
    with pytest.raises(ConfigValidationError):
        await system_hello({"client_version": "not-a-semver"})


async def test_hello_rejects_missing_client_version():
    with pytest.raises(ConfigValidationError):
        await system_hello({})


async def test_ping_returns_server_time():
    before = int(time.time() * 1000)
    result = await system_ping({})
    after = int(time.time() * 1000)
    assert result["pong"] is True
    assert isinstance(result["server_time_ms"], int)
    # Should be within the window we measured
    assert before - 1000 <= result["server_time_ms"] <= after + 1000


async def test_version_returns_three_fields():
    result = await system_version({})
    assert "server_version" in result
    assert "schema_version" in result
    assert "raven_version" in result
    assert all(
        isinstance(result[k], str) for k in ("server_version", "schema_version", "raven_version")
    )


# ---------------------------------------------------------------------------
# Dispatcher framing / error mapping
# ---------------------------------------------------------------------------


def _build_dispatcher() -> Dispatcher:
    d = Dispatcher()
    register_system_methods(d)
    return d


async def test_dispatcher_hello_happy_path():
    d = _build_dispatcher()
    frame = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "system.hello",
        "params": {"client_version": "0.1.0"},
    }
    resp = await d.dispatch(frame)
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    assert "result" in resp
    assert "error" not in resp
    assert resp["result"]["server_capabilities"]


async def test_dispatcher_unknown_method():
    d = _build_dispatcher()
    frame = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "no.such.method",
        "params": {},
    }
    resp = await d.dispatch(frame)
    assert "error" in resp
    assert resp["error"]["code"] == -32601  # method_not_found
    assert resp["id"] == 2


async def test_dispatcher_invalid_jsonrpc_version():
    d = _build_dispatcher()
    frame = {
        "jsonrpc": "1.0",
        "id": 3,
        "method": "system.ping",
        "params": {},
    }
    resp = await d.dispatch(frame)
    assert "error" in resp
    assert resp["error"]["code"] == -32600  # invalid_request


async def test_dispatcher_missing_method_field():
    d = _build_dispatcher()
    frame = {"jsonrpc": "2.0", "id": 4}
    resp = await d.dispatch(frame)
    assert "error" in resp
    assert resp["error"]["code"] == -32600


async def test_dispatcher_validation_error_maps_to_32011():
    d = _build_dispatcher()
    frame = {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "system.hello",
        "params": {"client_version": "not-semver"},
    }
    resp = await d.dispatch(frame)
    assert "error" in resp
    assert resp["error"]["code"] == -32011  # config_validation_error
    assert resp["error"]["message"] == "config_validation_error"


async def test_dispatcher_internal_error_maps_to_32603():
    d = Dispatcher()

    async def boom(params: dict) -> dict:
        raise RuntimeError("kaboom")

    d.register("test.boom", boom)
    frame = {"jsonrpc": "2.0", "id": 6, "method": "test.boom", "params": {}}
    resp = await d.dispatch(frame)
    assert "error" in resp
    assert resp["error"]["code"] == -32603  # internal_error
    # Traceback tail should be included for debuggability
    assert "data" in resp["error"]
    assert "traceback_tail" in resp["error"]["data"]


async def test_dispatcher_parse_response_id_echoed():
    d = _build_dispatcher()
    frame = {
        "jsonrpc": "2.0",
        "id": "string-id-abc",
        "method": "system.ping",
        "params": {},
    }
    resp = await d.dispatch(frame)
    assert resp["id"] == "string-id-abc"
    assert resp["result"]["pong"] is True
