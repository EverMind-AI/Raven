"""Host-agent terminal tools wrap existing RPCs and preserve identity fences."""

import json
from unittest.mock import AsyncMock

import pytest

from raven.agent.tools.terminal import CreateTerminalTool, ResolveAgentTool, SendTerminalTool
from raven.contracts.terminal import TerminalError


async def test_create_registers_identity_and_returns_incarnation():
    calls = []

    async def rpc(method, params):
        calls.append((method, params))
        if method == "agents.resolve":
            return {"unique": False, "candidates": []}
        if method == "terminal.create":
            return {"terminal": {"handle": "term_test", "incarnationId": "incarnation", "title": "worker"}}
        if method == "agents.register":
            return {"agent": {"agentName": "worker"}}
        raise AssertionError(method)

    tool = CreateTerminalTool(rpc, lambda provider: ("codex", ["codex"]), lambda task: "repo::/tmp/task")
    result = json.loads(await tool.execute(provider="codex", name="worker", task="repo::/tmp/task"))
    assert result == {"handle": "term_test", "instance": "worker", "incarnation_id": "incarnation"}
    assert calls[-1][0] == "agents.register"
    assert calls[-1][1]["terminal"] == "term_test"


async def test_failed_identity_registration_closes_only_the_created_terminal():
    calls = []

    async def rpc(method, params):
        calls.append((method, params))
        if method == "agents.resolve":
            return {"unique": False, "candidates": []}
        if method == "terminal.create":
            return {"terminal": {"handle": "term_created", "incarnationId": "new"}}
        if method == "agents.register":
            raise TerminalError("duplicate_name", "Name claimed concurrently")
        return {}

    tool = CreateTerminalTool(rpc, lambda provider: ("codex", ["codex"]), lambda task: "repo::/tmp/task")
    result = json.loads(await tool.execute(provider="codex", name="worker"))
    assert result["error"]["code"] == "duplicate_name"
    assert calls[-1] == ("terminal.close", {"handle": "term_created"})


def candidate():
    return {
        "agentName": "worker",
        "orphan": False,
        "binding": {
            "handle": "term_test",
            "incarnationId": "live",
            "worktreeId": "repo::/tmp/task",
            "tabId": "tab",
            "leafId": "leaf",
        },
    }


@pytest.mark.parametrize("incarnation,should_send", [("live", True), ("restarted", False)])
async def test_send_fences_fresh_show_before_delivery(incarnation, should_send):
    calls = []

    async def rpc(method, params):
        calls.append((method, params))
        if method == "agents.resolve":
            return {"unique": True, "candidates": [{"agent": candidate(), "reason": "exact"}]}
        if method == "terminal.show":
            return {
                "terminal": {
                    **candidate()["binding"],
                    "incarnationId": incarnation,
                    "connected": True,
                    "writable": True,
                    "orphaned": False,
                }
            }
        if method == "terminal.send":
            return {"send": {"accepted": True}}
        raise AssertionError(method)

    result = json.loads(await SendTerminalTool(rpc).execute(to="worker", text="hello", require_ack=True))
    assert (calls[-1][0] == "terminal.send") is should_send
    if should_send:
        assert result["accepted"] is True
        assert calls[-1][1]["require_ack"] is True
        assert "nonce=a2a-" in calls[-1][1]["text"]
    else:
        assert result["error"]["code"] == "agent_binding_stale"


async def test_resolve_returns_candidates_without_selecting_an_ambiguous_match():
    response = {"unique": False, "candidates": [{"agent": candidate(), "reason": "alias"}] * 2}
    rpc = AsyncMock(return_value=response)
    assert json.loads(await ResolveAgentTool(rpc).execute(mention="team")) == response
    refused = json.loads(await SendTerminalTool(rpc).execute(to="team", text="hello"))
    assert refused["error"]["code"] == "agent_not_unique"
    assert all(call.args[0] == "agents.resolve" for call in rpc.await_args_list)


async def test_registered_tools_use_rpc_owner_and_do_not_bypass_disabled_tools():
    from raven.agent.tools.registry import ToolRegistry
    from raven.rpc import connection
    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.terminal_tools import register_terminal_tools

    async def resolve(params):
        assert connection.current_state()["terminal_owner"] == "raven"
        return {"unique": False, "candidates": []}

    dispatcher = Dispatcher()
    dispatcher.register("agents.resolve", resolve)
    registry = ToolRegistry()
    register_terminal_tools(registry, dispatcher)
    assert {"create_terminal", "send_terminal", "resolve_agent"} <= set(registry.tool_names)
    assert json.loads(await registry.get("resolve_agent").execute(mention="missing"))["unique"] is False
    registry.set_withheld_source(lambda: frozenset({"create_terminal", "send_terminal", "resolve_agent"}))
    assert not registry.get_definitions()


async def test_terminal_prompt_only_appears_when_tools_are_available(tmp_path):
    from raven.context_engine.segments.identity import IdentitySegmentBuilder

    names = ["create_terminal", "send_terminal", "resolve_agent"]
    builder = IdentitySegmentBuilder(tmp_path, get_tool_definitions=lambda: [{"function": {"name": n}} for n in names])
    assert "Plain-text hand-over" in (await builder.build(None)).text
    names.clear()
    assert "Plain-text hand-over" not in (await builder.build(None)).text
