"""Experimental hosting reuses native protocol behavior and closes leaf dispatch without core patches."""

import asyncio
import io
import json
from types import SimpleNamespace

import pytest

from experimental.curator.raven_adapter.capability.leaf import LeafRegistry, bind_leaf
from experimental.curator.raven_adapter.hosting.acp import PAGE_BYTES, ROW_BYTES, bounded, page
from experimental.curator.raven_adapter.hosting.connection import connection
from experimental.curator.raven_adapter.hosting.evidence import inspect as inspect_child
from experimental.curator.raven_adapter.hosting.transport import serve
from raven.acp.methods import AcpMethodError
from raven.agent.subagent.manager import SubagentManager
from raven.agent.subagent.role import SUBAGENT_ENV_VAR
from tests.test_acp_server import _frames, _reader, _Stack
from tests.test_subagent_manager import _StubProvider


@pytest.mark.asyncio
async def test_private_inspection_obeys_native_initialization_and_closes_the_owned_stack():
    stack = _Stack()
    calls = []

    async def factory(translator, *, channel, approval_responder):
        assert channel == "acp" and approval_responder is not None
        return stack

    async def inspect(params):
        calls.append(params)
        return {"instance": "actual-host"}

    out = io.BytesIO()
    payload = (
        b'{"jsonrpc":"2.0","id":0,"method":"_host/inspect","params":{}}\n'
        b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
        b'{"jsonrpc":"2.0","id":2,"method":"_host/inspect","params":{}}\n'
    )
    await serve(_reader(payload), out, stack_factory=factory, extensions={"_host/inspect": inspect})
    replies = {row["id"]: row for row in _frames(out)}
    assert "initialize" in replies[0]["error"]["message"]
    assert replies[2]["result"]["instance"] == "actual-host" and calls == [{}]
    assert stack.torn_down == 1


@pytest.mark.asyncio
async def test_invalid_extension_still_closes_the_constructed_stack():
    stack = _Stack()

    async def factory(*args, **kwargs):
        return stack

    with pytest.raises(ValueError, match="underscore-prefixed"):
        await serve(_reader(b""), io.BytesIO(), stack_factory=factory, extensions={"session/new": None})
    assert stack.torn_down == 1


@pytest.mark.asyncio
async def test_suspended_request_does_not_block_another_request_or_lose_eof_replies():
    stack = _Stack()
    released = asyncio.Event()

    async def factory(*args, **kwargs):
        return stack

    async def slow(params):
        await released.wait()
        return {"finished": True}

    async def release(params):
        released.set()
        return {}

    out = io.BytesIO()
    payload = (
        b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
        b'{"jsonrpc":"2.0","id":2,"method":"_slow","params":{}}\n'
        b'{"jsonrpc":"2.0","id":3,"method":"_release","params":{}}\n'
    )
    await serve(_reader(payload), out, stack_factory=factory, extensions={"_slow": slow, "_release": release})
    replies = {row["id"]: row for row in _frames(out)}
    assert replies[2]["result"]["finished"] and 3 in replies
    assert stack.torn_down == 1


@pytest.mark.asyncio
async def test_leaf_closes_spawn_direct_backend_dag_resolution_and_config_reopening(tmp_path, monkeypatch):
    from raven.agent.subagent.dag_tool import SubAgentDagTool
    from raven.config.schema import ThirdPartyAcpSubagentConfig

    monkeypatch.setenv(SUBAGENT_ENV_VAR, "1")
    provider = _StubProvider()
    manager = SubagentManager(provider=provider, workspace=tmp_path)
    runtime = SimpleNamespace(loop=SimpleNamespace(subagents=manager, tools=SimpleNamespace(get=lambda name: None)))
    bind_leaf(runtime)
    assert isinstance(manager.registry, LeafRegistry)
    assert "delegation is disabled" in await manager.spawn("Nested work", agent="Raven", session_key="leaf")
    with pytest.raises(RuntimeError, match="delegation is disabled"):
        manager._resolve_backend("Raven")
    with pytest.raises(RuntimeError, match="disabled"):
        await manager.create_instance(session_key="leaf", agent="Raven")
    dag = SubAgentDagTool(workspace=tmp_path, registry=manager.registry)
    with pytest.raises(RuntimeError, match="delegation is disabled"):
        dag.registry.backend("Raven")
    with pytest.raises(ValueError, match="delegation is disabled"):
        manager.apply_agents([ThirdPartyAcpSubagentConfig(name="Other", command="unused")])
    manager.apply_agents([])
    assert manager.registry.names() == []
    await manager.cancel_all()


@pytest.mark.asyncio
async def test_inspection_acquires_the_native_parent_binding_without_changing_backend_class():
    from unittest.mock import AsyncMock

    from raven.acp_client.acp_agent import AcpAgentBackend

    pool = SimpleNamespace(acquire=AsyncMock(return_value=object()))
    backend = SimpleNamespace(
        pool=pool,
        name="Child",
        command="host-child",
        cwd=None,
        env={"MODE": "test"},
        ready_timeout_ms=7000,
    )
    provider = SimpleNamespace(provider_name="custom", api_protocol="chat")
    found = await connection(backend, workspace="/task", provider=provider, model="model")
    assert found is pool.acquire.return_value
    pool.acquire.assert_awaited_once_with(
        name="Child",
        command="host-child",
        cwd="/task",
        env={"MODE": "test"},
        binding={"RAVEN_PARENT_MODEL": "model", "RAVEN_PARENT_PROVIDER": "custom", "RAVEN_PARENT_PROTOCOL": "chat"},
        ready_timeout_s=7.0,
    )
    assert not hasattr(AcpAgentBackend, "connection")


def test_leaf_rejects_reintroduced_orchestration_tools(monkeypatch):
    monkeypatch.setenv(SUBAGENT_ENV_VAR, "1")
    loop = SimpleNamespace(tools=SimpleNamespace(get=lambda name: object() if name == "spawn" else None))
    with pytest.raises(ValueError, match="orchestration tools"):
        bind_leaf(SimpleNamespace(loop=loop))


def _rows():
    context = [{"role": "user", "content": "x" * 50_000} for _ in range(40)]
    return [
        {"kind": "hosting.ready", "turn_id": None},
        *({"kind": "provider.delta", "turn_id": "t", "delta": {"content": "tok"}} for _ in range(500)),
        {"kind": "provider.request", "turn_id": "t", "parameters": {"messages": context}},
        {"kind": "action.result", "turn_id": "t", "result": {"kind": "retry"}},
    ]


def test_a_page_leaves_streamed_deltas_in_the_log_and_shortens_only_oversized_rows():
    rows = _rows()
    result = page(rows, 0)
    kinds = [row["kind"] for row in result["records"]]
    assert kinds == ["hosting.ready", "provider.request", "action.result"]
    assert result["omitted"] == {"provider.delta": 500} and result["next"] == len(rows)
    request = result["records"][1]
    assert len(json.dumps(rows[-2])) > ROW_BYTES >= len(json.dumps(request)) // 8
    messages = request["parameters"]["messages"]
    assert len(messages) == 21 and "more items in the child's log" in messages[4]
    assert "more characters in the child's log" in messages[-1]["content"]
    assert result["records"][2] == rows[-1] and rows[-2]["parameters"]["messages"][-1]["content"] == "x" * 50_000
    assert bounded("short") == "short" and bounded([1, 2]) == [1, 2]


def test_a_page_stops_under_its_byte_budget_and_resumes_from_next():
    rows = [{"kind": "memory.result", "turn_id": "t", "value": "v" * 10_000} for _ in range(10)]
    first = page(rows, 0, budget=25_000)
    assert len(first["records"]) == 2 and first["next"] == 2
    assert page(rows, 9, budget=1)["records"] == [rows[9]]
    assert page(rows, len(rows))["records"] == [] and PAGE_BYTES < 8 * 1024 * 1024
    for offset in (-1, len(rows) + 1, True, "0"):
        with pytest.raises(AcpMethodError):
            page(rows, offset)


@pytest.mark.asyncio
async def test_inspect_fetches_every_page_from_an_offset_and_otherwise_carries_no_records():
    rows = [{"kind": "memory.result", "turn_id": "t", "value": f"{index:03d}" * 4_000} for index in range(400)]
    rows.insert(3, {"kind": "provider.delta", "turn_id": "t", "delta": {}})
    requests = []

    class Client:
        async def request(self, method, params, timeout):
            requests.append(params)
            report = {"pid": 7, "log": "observations.jsonl", "total": len(rows)}
            return {**report, **page(rows, params["offset"])} if "offset" in params else report

    plain = await inspect_child(Client())
    assert "records" not in plain and plain["total"] == 401 and requests == [{}]
    report = await inspect_child(Client(), offset=1)
    assert report["records"] == [row for row in rows[1:] if row["kind"] != "provider.delta"]
    assert report["omitted"] == {"provider.delta": 1} and len(requests) >= 4
