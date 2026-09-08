"""Identity RPC preserves JSON-RPC framing and validates terminal bindings."""

from raven.agent.registry.identity import IdentityRegistry
from raven.contracts.terminal import RuntimeInfo, TerminalRecord
from raven.rpc.dispatcher import Dispatcher
from raven.rpc.methods.agents import register_agents_methods


async def test_identity_rpc_register_list_resolve_and_invalid_kind(tmp_path):
    registry = IdentityRegistry(
        tmp_path / "agent_registry.json", config_rows=lambda: [{"name": "coder", "kind": "builtin"}]
    )
    record = TerminalRecord(worktree_id="repo::/tmp/work", worktree_path="/tmp/work", owner="human", liveness="live")
    dispatcher = Dispatcher()
    register_agents_methods(dispatcher, registry=registry, terminal_show=lambda _: record)

    async def rpc(method, params):
        return await dispatcher.dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})

    result = await rpc(
        "agents.register",
        {"name": "worker-a", "kind": "coder", "terminal": record.handle, "session_key": "web:creator"},
    )
    assert result["jsonrpc"] == "2.0"
    assert result["result"]["agent"]["binding"]["handle"] == record.handle
    assert result["result"]["agent"]["sessionKey"] == "web:creator"
    assert result["result"]["_meta"]["runtimeId"] == RuntimeInfo().runtime_id
    assert (await rpc("agents.list", {}))["result"]["agents"][0]["agentName"] == "worker-a"
    assert (await rpc("agents.resolve", {"mention": "worker-a"}))["result"]["unique"]
    assert (await rpc("agents.show", {"name": "worker-a"}))["result"]["agent"]["kindRef"] == "coder"
    invalid = await rpc("agents.register", {"name": "bad", "kind": "missing"})
    assert invalid["error"]["data"]["code"] == "invalid_kind_ref"
    assert (await rpc("agents.register", {}))["error"]["code"] == -32602
