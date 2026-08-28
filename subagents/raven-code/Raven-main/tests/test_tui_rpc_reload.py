"""Tests for ``reload.mcp`` RPC handler (specs §3.10, design §3a.2).

Q10 deferred: hermes UI polls ``reload.mcp`` every 5s (hard-coded in fork-imported
``useConfigSync.ts:202``). v0.1 returns a deterministic no-op shape — never
throws so the polling does not pollute logs.
"""

from __future__ import annotations

from raven.tui_rpc.dispatcher import Dispatcher
from raven.tui_rpc.methods.reload import register_reload_methods, reload_mcp


async def test_reload_mcp_returns_no_op_shape() -> None:
    result = await reload_mcp({})
    assert result == {"ok": True, "reloaded": 0, "tools_changed": False}


async def test_reload_mcp_ignores_params() -> None:
    # The hermes client passes nothing today but the contract MUST accept any
    # extra keys without complaint (forward-compat for v0.2 if we ever do
    # implement MCP admin).
    result = await reload_mcp({"force": True, "selective": ["everos"]})
    assert result["ok"] is True


async def test_reload_mcp_rapid_fire_does_not_raise() -> None:
    # Sanity perf: hermes polls every 5s; this stresses 100 back-to-back
    # invocations to ensure no shared state grows / leaks.
    for _ in range(100):
        result = await reload_mcp({})
        assert result["ok"] is True


async def test_reload_mcp_registered_via_helper() -> None:
    d = Dispatcher()
    register_reload_methods(d)
    resp = await d.dispatch({"jsonrpc": "2.0", "id": 1, "method": "reload.mcp", "params": {}})
    assert "error" not in resp
    assert resp["result"]["ok"] is True
    assert resp["result"]["tools_changed"] is False
