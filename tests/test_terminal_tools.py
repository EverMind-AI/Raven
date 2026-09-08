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

    tool = CreateTerminalTool(
        rpc, lambda provider, unattended=False: ("codex", ["codex"]), lambda task, session: "repo::/tmp/task"
    )
    tool.set_context("tui", "creator")
    result = json.loads(await tool.execute(provider="codex", name="worker", task="repo::/tmp/task"))
    assert result == {"handle": "term_test", "instance": "worker", "incarnation_id": "incarnation"}
    assert calls[-1][0] == "agents.register"
    assert calls[-1][1]["terminal"] == "term_test"
    assert calls[-1][1]["session_key"] == "tui:creator"


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

    tool = CreateTerminalTool(
        rpc, lambda provider, unattended=False: ("codex", ["codex"]), lambda task, session: "repo::/tmp/task"
    )
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


@pytest.fixture
def configured_terminal_kinds(monkeypatch):
    from types import SimpleNamespace

    rows = []
    monkeypatch.setattr(
        "raven.config.loader.load_config", lambda: SimpleNamespace(subagents=SimpleNamespace(agents=rows))
    )
    return rows


@pytest.mark.parametrize("provider", ["claude-code", "claude_code", "claude", "claude code", " Claude Code "])
def test_provider_command_accepts_claude_aliases(configured_terminal_kinds, provider):
    from raven.rpc.terminal_tools import provider_command

    configured_terminal_kinds.extend(
        [{"name": "researcher", "preset": "claude_code"}, {"name": "implementer", "preset": "codex"}]
    )
    assert provider_command(provider) == ("researcher", ["claude"])


@pytest.mark.parametrize("provider", ["codex", "codex-cli"])
def test_provider_command_accepts_codex_aliases(configured_terminal_kinds, provider):
    from raven.rpc.terminal_tools import provider_command

    configured_terminal_kinds.append({"name": "implementer", "preset": "codex"})
    assert provider_command(provider) == ("implementer", ["codex"])


@pytest.mark.parametrize("rows", [[], [{"name": "implementer", "preset": "codex"}]])
def test_provider_command_explains_missing_kind(configured_terminal_kinds, rows):
    from raven.rpc.terminal_tools import provider_command

    configured_terminal_kinds.extend(rows)
    with pytest.raises(TerminalError, match="External Agents.*Claude Code or Codex preset") as error:
        provider_command("claude-code")
    assert error.value.code == "no_matching_kind"


def test_provider_command_preserves_ambiguity_and_exact_names(configured_terminal_kinds):
    from raven.rpc.terminal_tools import provider_command

    configured_terminal_kinds.extend(
        [{"name": "researcher", "preset": "claude_code"}, {"name": "reviewer", "preset": "claude_code"}]
    )
    with pytest.raises(TerminalError, match="External Agents.*exact name") as error:
        provider_command("claude-code")
    assert error.value.code == "provider_not_unique"
    assert provider_command("reviewer") == ("reviewer", ["claude"])


def test_provider_command_does_not_launch_unsupported_kind(configured_terminal_kinds):
    from raven.rpc.terminal_tools import provider_command

    configured_terminal_kinds.append({"name": "custom", "preset": "unsupported"})
    with pytest.raises(TerminalError) as error:
        provider_command("custom")
    assert error.value.code == "no_matching_kind"


@pytest.mark.parametrize(
    "provider,preset,command,flag",
    [
        ("claude-code", "claude_code", "claude", "--dangerously-skip-permissions"),
        ("codex-cli", "codex", "codex", "--dangerously-bypass-approvals-and-sandbox"),
    ],
)
def test_hosted_commands_ignore_acp_adapter(configured_terminal_kinds, provider, preset, command, flag):
    from raven.rpc.terminal_tools import provider_command

    configured_terminal_kinds.append({"name": "worker", "preset": preset, "command": "claude-agent-acp"})
    assert provider_command(provider) == ("worker", [command])
    assert provider_command(provider, unattended=True) == ("worker", [command, flag])


@pytest.mark.parametrize("task", ["", "/selected/task", "/unrelated/task"])
async def test_registered_create_uses_calling_session_cwd(monkeypatch, configured_terminal_kinds, task):
    from raven.agent.tools.registry import ToolRegistry
    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.terminal_tools import register_terminal_tools

    configured_terminal_kinds.append({"name": "claude_code", "preset": "claude_code"})
    monkeypatch.setattr("raven.rpc.terminal_tools.task_worktree", lambda path: f"repo::{path}")
    seen = []

    async def resolve(params):
        return {"candidates": []}

    async def create(params):
        seen.append(params)
        return {"terminal": {"handle": "term_new", "incarnationId": "new"}}

    async def register(params):
        return {}

    dispatcher = Dispatcher()
    for method, handler in [("agents.resolve", resolve), ("terminal.create", create), ("agents.register", register)]:
        dispatcher.register(method, handler)
    registry = ToolRegistry()
    sessions = {"tui:selected": "/selected/task"}
    register_terminal_tools(registry, dispatcher, session_cwd=sessions.get)
    tool = registry.get("create_terminal")
    tool.set_context("tui", "selected")
    result = json.loads(await tool.execute(provider="claude-code", name="worker", task=task))
    if task == "/unrelated/task":
        assert result["error"]["code"] == "task_worktree_mismatch"
        assert not seen
    else:
        assert result["handle"] == "term_new"
        assert seen == [
            {
                "worktree_id": "repo::/selected/task",
                "command": ["claude"],
                "title": "worker",
                "session_id": "tui:selected",
            }
        ]


@pytest.mark.parametrize("has_context", [True, False])
async def test_registered_create_refuses_missing_session_cwd(has_context):
    from raven.agent.tools.registry import ToolRegistry
    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.terminal_tools import register_terminal_tools

    registry = ToolRegistry()
    register_terminal_tools(registry, Dispatcher(), session_cwd=lambda session: None)
    tool = registry.get("create_terminal")
    if has_context:
        tool.set_context("tui", "missing")
    result = json.loads(await tool.execute(provider="claude", name="worker", task="/tmp/invented"))
    assert result["error"]["code"] == "session_cwd_missing"
    assert "session has no cwd" in result["error"]["message"]


def test_task_worktree_never_falls_back_to_process_cwd():
    from raven.rpc.terminal_tools import task_worktree

    with pytest.raises(TerminalError) as error:
        task_worktree()
    assert error.value.code == "session_cwd_missing"


@pytest.mark.parametrize("reason", ["startup_pending", "permission"])
@pytest.mark.parametrize("outcome", ["success", "timeout", "blocked", "retry_blocked"])
async def test_send_waits_for_dialog_and_retries_once(reason, outcome):
    calls = []
    sends = []

    async def rpc(method, params):
        calls.append((method, params))
        if method == "agents.resolve":
            return {"unique": True, "candidates": [{"agent": candidate()}]}
        if method == "terminal.show":
            return {"terminal": {**candidate()["binding"], "connected": True, "writable": True, "orphaned": False}}
        if method == "terminal.wait":
            return {"wait": {"satisfied": outcome in {"success", "retry_blocked"}, "timedOut": outcome == "timeout"}}
        if method == "terminal.send":
            sends.append(dict(params))
            if len(sends) == 1 or outcome == "retry_blocked":
                raise TerminalError("agent_prompt_blocked", "Blocked", {"reason": reason, "bytesWritten": 0})
            return {"send": {"accepted": True}}
        raise AssertionError(method)

    tool = SendTerminalTool(rpc)
    tool.set_context("tui", "selected")
    result = json.loads(await tool.execute(to="worker", text="hello", require_ack=True))
    assert [p for m, p in calls if m == "terminal.wait"] == [
        {"handle": "term_test", "for": "tui-idle", "timeout_ms": 120000}
    ]
    if outcome in {"success", "retry_blocked"}:
        assert len(sends) == 2
        assert sends[0] == sends[1]
        assert sends[1]["session_id"] == "tui:selected"
    else:
        assert len(sends) == 1
    if outcome == "success":
        assert result["accepted"] is True
    else:
        assert result["error"]["code"] == "agent_prompt_blocked"
        assert "human" in result["error"]["message"]
        assert "terminal tab" in result["error"]["message"]
        assert "retry later" in result["error"]["message"]


@pytest.mark.parametrize(
    "code,data",
    [
        ("agent_prompt_stalled", {"reason": "permission"}),
        ("agent_prompt_blocked", {"reason": "other"}),
        ("agent_prompt_blocked", None),
    ],
)
async def test_send_does_not_retry_other_failures(code, data):
    calls = []

    async def rpc(method, params):
        calls.append(method)
        if method == "agents.resolve":
            return {"unique": True, "candidates": [{"agent": candidate()}]}
        if method == "terminal.show":
            return {"terminal": {**candidate()["binding"], "connected": True, "writable": True, "orphaned": False}}
        if method == "terminal.send":
            raise TerminalError(code, "Original failure", data)
        raise AssertionError(method)

    result = json.loads(await SendTerminalTool(rpc).execute(to="worker", text="hello"))
    assert result["error"]["code"] == code
    assert result["error"]["message"] == "Original failure"
    assert calls.count("terminal.send") == 1
    assert "terminal.wait" not in calls


async def test_send_never_retries_dialog_after_partial_write():
    calls = []

    async def rpc(method, params):
        calls.append(method)
        if method == "agents.resolve":
            return {"unique": True, "candidates": [{"agent": candidate()}]}
        if method == "terminal.show":
            return {"terminal": {**candidate()["binding"], "connected": True, "writable": True, "orphaned": False}}
        if method == "terminal.send":
            raise TerminalError("agent_prompt_blocked", "Permission", {"reason": "permission", "bytesWritten": 128})
        raise AssertionError(method)

    result = json.loads(await SendTerminalTool(rpc).execute(to="worker", text="hello"))
    assert result["error"]["code"] == "agent_prompt_blocked"
    assert "verify delivery" in result["error"]["message"]
    assert result["error"]["data"]["bytesWritten"] == 128
    assert calls.count("terminal.send") == 1
    assert "terminal.wait" not in calls


@pytest.mark.parametrize("force", [False, True])
async def test_send_only_forces_when_explicitly_requested(force):
    sent = []

    async def rpc(method, params):
        if method == "agents.resolve":
            return {"unique": True, "candidates": [{"agent": candidate()}]}
        if method == "terminal.show":
            return {"terminal": {**candidate()["binding"], "connected": True, "writable": True, "orphaned": False}}
        sent.append(params)
        return {"send": {"accepted": True}}

    assert json.loads(await SendTerminalTool(rpc).execute(to="worker", text="hello", force=force))["accepted"]
    assert sent[0].get("force", False) is force
    assert "human confirms" in SendTerminalTool.parameters["properties"]["force"]["description"]


@pytest.mark.parametrize("failure_at", ["resolve", "show", "send"])
async def test_send_reports_stale_binding_with_last_handle(failure_at):
    agent = candidate()
    if failure_at == "resolve":
        agent["exitedAt"] = 123

    async def rpc(method, params):
        if method == "agents.resolve":
            return {"unique": failure_at != "resolve", "candidates": [{"agent": agent}]}
        if method == "terminal.show" and failure_at != "show":
            return {"terminal": {**agent["binding"], "connected": True, "writable": True, "orphaned": False}}
        raise TerminalError("terminal_not_found", "Old handle")

    result = json.loads(await SendTerminalTool(rpc).execute(to="worker", text="hello"))
    assert result["error"]["code"] == "agent_binding_stale"
    assert result["error"]["data"]["handle"] == "term_test"


@pytest.mark.parametrize("resume_session_id", [None, "native-previous"])
async def test_create_after_host_restart_renews_identity_generation(tmp_path, resume_session_id):
    from types import SimpleNamespace

    from raven.agent.registry.identity import IdentityRegistry
    from raven.contracts.terminal import TerminalRecord
    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.subscriptions import SubscriptionEmitter
    from raven.rpc.terminal_services import TerminalServices

    def rows():
        return [{"name": "generic", "kind": "builtin"}, {"name": "coder", "preset": "codex"}]

    path = tmp_path / "identities.json"
    old = TerminalRecord(worktree_id=f"repo::{tmp_path}", worktree_path=str(tmp_path))
    registry = IdentityRegistry(path, config_rows=rows)
    registry.register("worker", kind_ref="coder", binding=old)
    terminals = {}

    def show(handle):
        if handle not in terminals:
            raise TerminalError("terminal_not_found", "Host restarted")
        return terminals[handle]

    async def create(**params):
        record = TerminalRecord(worktree_id=params["worktree_id"], worktree_path=str(tmp_path), liveness="live")
        terminals[record.handle] = record
        return record

    host = SimpleNamespace(show=show, create=create)
    service = TerminalServices(SubscriptionEmitter(AsyncMock()), AsyncMock(), host=host, identities=registry)
    assert not registry.resolve("worker")["unique"]
    assert json.loads(path.read_text())["records"][0]["exited_at"] is not None
    dispatcher = Dispatcher()
    service.register(dispatcher)

    async def rpc(method, params):
        response = await dispatcher.dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if "error" in response:
            raise TerminalError(
                response["error"].get("data", {}).get("code", response["error"]["message"]), "Old host handle"
            )
        assert "result" in response, response
        return response["result"]

    tool = CreateTerminalTool(
        rpc,
        lambda provider, unattended=False, **resume: ("coder", ["codex"]),
        lambda task, session: f"repo::{tmp_path}",
    )
    result = json.loads(await tool.execute(provider="codex", name="worker", resume_session_id=resume_session_id))
    renewed = registry.show("worker")
    assert result["handle"] != old.handle
    assert renewed.binding_generation == 2
    assert renewed.binding.handle == result["handle"]
    assert renewed.exited_at is None
    assert registry.resolve("worker")["unique"]
    refused = json.loads(await tool.execute(provider="codex", name="worker"))
    assert refused["error"]["code"] == "agent_name_exists"
    assert len(terminals) == 1


@pytest.mark.parametrize("provider,operation", [("claude", "--resume"), ("codex", "resume")])
@pytest.mark.parametrize("unattended", [False, True])
def test_provider_resume_argv(configured_terminal_kinds, provider, operation, unattended):
    from raven.rpc.terminal_tools import provider_command

    preset = "claude_code" if provider == "claude" else "codex"
    configured_terminal_kinds.append({"name": "worker", "preset": preset})
    flag = "--dangerously-skip-permissions" if provider == "claude" else "--dangerously-bypass-approvals-and-sandbox"
    assert provider_command(provider, unattended=unattended, resume_session_id="native-session") == (
        "worker",
        [provider, operation, "native-session", *([flag] if unattended else [])],
    )


@pytest.mark.parametrize("session_id", ["", "  ", "-last", " --last", "bad\x00id"])
def test_provider_resume_rejects_invalid_ids(configured_terminal_kinds, session_id):
    from raven.rpc.terminal_tools import provider_command

    configured_terminal_kinds.append({"name": "worker", "preset": "codex"})
    with pytest.raises(TerminalError) as error:
        provider_command("codex", resume_session_id=session_id)
    assert error.value.code == "invalid_params"


@pytest.mark.parametrize("liveness", ["live", "unverifiable", "exited", "missing"])
async def test_resume_checks_old_host_before_creating(liveness):
    from unittest.mock import Mock

    provider = Mock(return_value=("codex", ["codex", "resume", "native-session"]))
    calls = []

    async def rpc(method, params):
        calls.append((method, params))
        if method == "agents.resolve":
            return {"unique": False, "candidates": [{"agent": {**candidate(), "exitedAt": 123}}]}
        if method == "terminal.show":
            if liveness == "missing":
                raise TerminalError("terminal_not_found", "Previous host gone")
            return {"terminal": {"liveness": liveness}}
        if method == "terminal.create":
            return {"terminal": {"handle": "new", "incarnationId": "new-incarnation"}}
        return {}

    tool = CreateTerminalTool(rpc, provider, lambda task, session: "repo::/selected")
    tool.set_context("tui", "creator")
    result = json.loads(await tool.execute(provider="codex", name="worker", resume_session_id="native-session"))
    provider.assert_called_once_with("codex", unattended=False, resume_session_id="native-session")
    if liveness in {"live", "unverifiable"}:
        assert result["error"]["code"] == "agent_name_exists"
        assert not any(method == "terminal.create" for method, _ in calls)
    else:
        assert result["handle"] == "new"
        assert calls[-1][1]["session_key"] == "tui:creator"
        assert calls[-1][1]["task_ref"] == "repo::/selected"
