"""SubagentManager concurrency gate.

Isolates the gate: build_executor and _run_subagent_inner are stubbed, so the
test drives only the Semaphore in _run_subagent (no real VM, no real LLM). A
stubbed inner holds each subagent inside the gate on an Event, letting the test
observe the concurrent peak.

Also covers: a subagent reuses the main LiteLLMProvider instance verbatim
(SubagentManager.provider), so the api_key that instance was constructed
with reaches acompletion on the subagent's own chat_with_retry() calls too —
acompletion is mocked, so this stays "no real LLM".
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from raven.agent.subagent import manager as manager_mod
from raven.agent.subagent.manager import SubagentManager
from raven.config.schema import AgentDefaults
from raven.providers.base import LLMResponse, ToolCallRequest
from raven.providers.litellm_provider import LiteLLMProvider
from raven.sandbox import ExecResult, SandboxExecutor


class _StubProvider:
    def get_default_model(self) -> str:
        return "stub-model"


class _DummyExecutor:
    async def __aenter__(self) -> "_DummyExecutor":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _RecordingExecutor(SandboxExecutor):
    def __init__(self) -> None:
        self.commands: list[str] = []

    @property
    def is_sandboxed(self) -> bool:
        return False

    async def exec(self, command: str, **kwargs) -> ExecResult:
        self.commands.append(command)
        return ExecResult(stdout="ok", stderr="", exit_code=0)


class _DeleteRetryProvider(_StubProvider):
    def __init__(self) -> None:
        self.responses = [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(id="call-a", name="exec", arguments={"command": "rm file.txt"}),
                    ToolCallRequest(
                        id="call-b",
                        name="exec",
                        arguments={"command": 'bash -c "rm file.txt"'},
                    ),
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="Retried through a shell wrapper.", finish_reason="stop"),
        ]

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        return self.responses.pop(0)


async def _settle(predicate, *, tries: int = 2000) -> None:
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never reached")


def _make_manager(max_concurrent: int) -> SubagentManager:
    return SubagentManager(
        provider=_StubProvider(),
        workspace=Path("/tmp"),
        max_concurrent=max_concurrent,
    )


async def _drive(monkeypatch, *, max_concurrent: int, spawn_n: int) -> int:
    """Spawn spawn_n subagents against a gate of max_concurrent; return the peak
    number that were ever inside the gate at once."""
    mgr = _make_manager(max_concurrent)
    monkeypatch.setattr(manager_mod, "build_executor", lambda *a, **k: _DummyExecutor())

    state = {"current": 0, "peak": 0}
    release = asyncio.Event()

    async def _stub_inner(task_id, task, label, origin, executor, provider, model) -> None:
        state["current"] += 1
        state["peak"] = max(state["peak"], state["current"])
        await release.wait()
        state["current"] -= 1

    monkeypatch.setattr(mgr, "_run_subagent_inner", _stub_inner)

    for i in range(spawn_n):
        await mgr.spawn(task=f"task-{i}")
    tasks = list(mgr._running_tasks.values())

    # Wait until the gate is saturated, then let any erroneous extra entrant
    # (which would push current past the cap) surface before asserting.
    await _settle(lambda: state["current"] == max_concurrent)
    await asyncio.sleep(0)
    peak = state["peak"]

    release.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    return peak


async def test_gate_caps_concurrent_subagents(monkeypatch):
    peak = await _drive(monkeypatch, max_concurrent=2, spawn_n=5)
    assert peak == 2


async def test_gate_of_one_serializes_subagents(monkeypatch):
    peak = await _drive(monkeypatch, max_concurrent=1, spawn_n=4)
    assert peak == 1


def test_the_dispatch_gate_is_the_one_spawns_wait_on() -> None:
    """DAG nodes are handed this same object, so `max_concurrent_subagents`
    counts every sub-agent in flight rather than granting each tool its own
    allowance. A copy sized the same would silently double the real cap."""
    mgr = _make_manager(max_concurrent=3)
    assert mgr.dispatch_gate is mgr._gate


async def test_announce_dag_result_addresses_the_originating_conversation() -> None:
    """A backgrounded DAG returns before its graph does, so this is the only
    path its outcome takes back to the agent -- and it has to land in the chat
    that asked."""
    mgr = _make_manager(max_concurrent=1)
    submitted: list[object] = []
    mgr.set_submit(submitted.append)

    await mgr.announce_dag_result(
        "20260101T000000Z-abcd1234",
        "DAG run 20260101T000000Z-abcd1234 finished: 2 completed",
        {"channel": "web", "chat_id": "default", "session_key": "web:sess1"},
    )

    assert len(submitted) == 1
    req = submitted[0]
    assert req.conversation == "web:sess1"
    assert req.source.channel == "web"
    assert req.source.chat_id == "default"
    assert "20260101T000000Z-abcd1234" in req.text
    assert "2 completed" in req.text


async def test_announce_dag_result_delivers_the_summary_verbatim() -> None:
    """The announce carries the run's own summary and nothing else.

    A graph's deliverable is its terminal node outputs, so any framing or
    retell-in-two-sentences instruction wrapped around them is lossy. The
    untrusted fence is the one exception: node output is attacker-influenceable
    and this arrives shaped like an inbound message, not like a tool result.
    """
    mgr = _make_manager(max_concurrent=1)
    submitted: list[object] = []
    mgr.set_submit(submitted.append)
    summary = "DAG run r1 finished: 1 completed, 0 failed, 0 skipped (of 1).\n\n### report\nthe deliverable"

    await mgr.announce_dag_result("r1", summary, {"channel": "web", "chat_id": "d", "session_key": "web:s1"})

    # Asserted structurally, not against a second wrap_untrusted call: the fence
    # mints a fresh nonce each time, so two wraps of one string never compare
    # equal. Everything between the markers must be the summary, unaltered.
    lines = submitted[0].text.splitlines()
    assert lines[0].startswith("[BEGIN UNTRUSTED subagent ")
    assert lines[-1].startswith("[END UNTRUSTED subagent ")
    assert "\n".join(lines[1:-1]) == summary


async def test_announce_dag_result_without_a_submit_does_not_raise() -> None:
    """The announce runs in a background task; an entry point that never wired
    the spine must lose the result, not kill the task with an assertion."""
    mgr = _make_manager(max_concurrent=1)
    await mgr.announce_dag_result("run-1", "summary", {"channel": "cli", "chat_id": "direct", "session_key": "cli"})


async def test_subagent_stops_after_terminal_shell_decision(monkeypatch, tmp_path):
    provider = _DeleteRetryProvider()
    manager = SubagentManager(provider=provider, workspace=tmp_path)
    executor = _RecordingExecutor()
    announcements: list[dict[str, str]] = []

    async def _capture_announcement(task_id, label, task, result, origin, status) -> None:
        announcements.append({"result": result, "status": status})

    monkeypatch.setattr(manager, "_announce_result", _capture_announcement)

    await manager._run_subagent_inner(
        "task-a",
        "delete file.txt",
        "delete",
        {"channel": "tui", "chat_id": "default", "session_key": "tui:session-a"},
        executor,
        manager.provider,
        manager.model,
    )

    assert executor.commands == []
    assert len(provider.responses) == 1
    assert announcements == [
        {
            "result": manager_mod.ABORTED_ACTION_RESULT,
            "status": "error",
        }
    ]


@pytest.mark.parametrize("bad", [0, -1])
def test_max_concurrent_subagents_must_be_positive(bad):
    with pytest.raises(ValidationError):
        AgentDefaults(max_concurrent_subagents=bad)


@pytest.mark.parametrize("bad", [0, -1])
def test_max_subagent_spawns_per_hour_must_be_positive(bad):
    with pytest.raises(ValidationError):
        AgentDefaults(max_subagent_spawns_per_hour=bad)


def _fixed_clock(monkeypatch, start: float = 1000.0) -> list[float]:
    """Pin manager's monotonic clock to a mutable value (advance via holder[0])."""
    holder = [start]
    monkeypatch.setattr(manager_mod.time, "monotonic", lambda: holder[0])
    return holder


def _stub_mgr(monkeypatch, **kw) -> SubagentManager:
    monkeypatch.setattr(manager_mod, "build_executor", lambda *a, **k: _DummyExecutor())
    mgr = SubagentManager(provider=_StubProvider(), workspace=Path("/tmp"), **kw)

    async def _noop_inner(*a, **k) -> None:  # complete immediately, no VM
        return None

    monkeypatch.setattr(mgr, "_run_subagent_inner", _noop_inner)
    return mgr


async def test_spawn_rate_limit_refuses_within_window(monkeypatch):
    """N spawns/window allowed; the next is refused even as concurrency frees up."""
    _fixed_clock(monkeypatch)
    mgr = _stub_mgr(monkeypatch, max_spawns_per_hour=2)

    assert "started" in await mgr.spawn(task="a")
    assert "started" in await mgr.spawn(task="b")
    await asyncio.gather(*mgr._running_tasks.values(), return_exceptions=True)

    r3 = await mgr.spawn(task="c")
    assert "Spawn refused" in r3
    assert "2 per hour" in r3


async def test_spawn_rate_limit_recovers_after_window(monkeypatch):
    """Older spawns age out of the rolling window, so the limit auto-recovers
    without any explicit /stop."""
    clock = _fixed_clock(monkeypatch)
    mgr = _stub_mgr(monkeypatch, max_spawns_per_hour=1)

    assert "started" in await mgr.spawn(task="a")
    assert "Spawn refused" in await mgr.spawn(task="b")  # second within window

    clock[0] += manager_mod._SPAWN_WINDOW_SECONDS + 1  # first spawn ages out
    assert "started" in await mgr.spawn(task="c")  # recovered


async def test_spawn_rate_limit_is_per_session(monkeypatch):
    """One session hitting the limit must not throttle others."""
    _fixed_clock(monkeypatch)
    mgr = _stub_mgr(monkeypatch, max_spawns_per_hour=1)

    assert "started" in await mgr.spawn(task="a", session_key="sessA")
    assert "Spawn refused" in await mgr.spawn(task="a2", session_key="sessA")
    assert "started" in await mgr.spawn(task="b", session_key="sessB")  # unaffected


async def test_cancel_by_session_clears_spawn_history(monkeypatch):
    """Session teardown drops its rate-limit history (bounds the dict)."""
    _fixed_clock(monkeypatch)
    mgr = _stub_mgr(monkeypatch, max_spawns_per_hour=1)

    assert "started" in await mgr.spawn(task="a", session_key="sessA")
    assert "Spawn refused" in await mgr.spawn(task="a2", session_key="sessA")
    await asyncio.gather(*mgr._running_tasks.values(), return_exceptions=True)

    await mgr.cancel_by_session("sessA")
    assert "sessA" not in mgr._session_spawn_times


async def test_cancel_by_session_cancels_live_task(monkeypatch):
    """cancel_by_session cancels a still-running asyncio.Task registered under
    the session (not just the rate-limit bookkeeping)."""
    mgr = _make_manager(max_concurrent=1)
    monkeypatch.setattr(manager_mod, "build_executor", lambda *a, **k: _DummyExecutor())

    entered = asyncio.Event()
    release = asyncio.Event()

    async def _blocking_inner(task_id, task, label, origin, executor, provider, model) -> None:
        entered.set()
        await release.wait()  # never set — keeps the task live until cancelled

    monkeypatch.setattr(mgr, "_run_subagent_inner", _blocking_inner)

    assert "started" in await mgr.spawn(task="long", session_key="sessLive")
    await _settle(entered.is_set)
    assert mgr.get_running_count() == 1
    (live_task,) = list(mgr._running_tasks.values())

    cancelled = await mgr.cancel_by_session("sessLive")
    assert cancelled == 1
    assert live_task.cancelled()
    await _settle(lambda: mgr.get_running_count() == 0)


async def test_default_subagent_instance_is_live_while_in_flight(monkeypatch):
    """A built-in (agent=None) spawn must key `_instance_tasks` the same way
    `_write_spawn_status` keys its registry row -- otherwise `live_handles`
    never reports it and a genuinely running instance reads as dead."""
    mgr = _make_manager(max_concurrent=1)
    monkeypatch.setattr(manager_mod, "build_executor", lambda *a, **k: _DummyExecutor())

    entered = asyncio.Event()
    release = asyncio.Event()

    async def _blocking_inner(task_id, task, label, origin, executor, provider, model) -> None:
        entered.set()
        await release.wait()

    monkeypatch.setattr(mgr, "_run_subagent_inner", _blocking_inner)

    assert "started" in await mgr.spawn(task="long", session_key="sessLive", instance="handle-x")
    await _settle(entered.is_set)

    assert mgr.live_handles("sessLive") == {(manager_mod.RAVEN_LOOP_AGENT, "handle-x")}

    release.set()
    await asyncio.gather(*mgr._running_tasks.values(), return_exceptions=True)


async def test_cancel_by_instance_stops_a_live_default_subagent(monkeypatch):
    """The stop key a later task wires to `cancel_by_instance` must actually
    find a built-in spawn, not silently no-op because it was never indexed."""
    mgr = _make_manager(max_concurrent=1)
    monkeypatch.setattr(manager_mod, "build_executor", lambda *a, **k: _DummyExecutor())

    entered = asyncio.Event()
    release = asyncio.Event()

    async def _blocking_inner(task_id, task, label, origin, executor, provider, model) -> None:
        entered.set()
        await release.wait()  # never set -- keeps the task live until cancelled

    monkeypatch.setattr(mgr, "_run_subagent_inner", _blocking_inner)

    assert "started" in await mgr.spawn(task="long", session_key="sessLive", instance="handle-x")
    await _settle(entered.is_set)

    cancelled = await mgr.cancel_by_instance("sessLive", manager_mod.RAVEN_LOOP_AGENT, "handle-x")
    assert cancelled is True
    await _settle(lambda: mgr.get_running_count() == 0)


async def test_announce_result_routes_to_tui_session_key(monkeypatch):
    """TUI origins pass an authoritative session_key distinct from channel:chat_id
    (chat_id falls back to "default" while the live subscription is keyed by
    session_key); the re-injected TurnRequest must land on that session, not on
    the derived channel:chat_id."""
    mgr = _make_manager(max_concurrent=1)
    submitted = []
    mgr.set_submit(lambda req: submitted.append(req))

    await mgr._announce_result(
        task_id="t1",
        label="label",
        task="task",
        result="result",
        origin={"channel": "tui", "chat_id": "default", "session_key": "tui:sess123"},
        status="ok",
    )

    assert len(submitted) == 1
    assert submitted[0].conversation == "tui:sess123"


async def test_announce_result_routes_non_tui_origin_unchanged(monkeypatch):
    """Non-TUI origins (e.g. a channel with a real chat_id) still announce on
    their existing channel:chat_id conversation — no regression."""
    mgr = _make_manager(max_concurrent=1)
    submitted = []
    mgr.set_submit(lambda req: submitted.append(req))

    await mgr._announce_result(
        task_id="t2",
        label="label",
        task="task",
        result="result",
        origin={"channel": "whatsapp", "chat_id": "12345", "session_key": "whatsapp:12345"},
        status="ok",
    )

    assert len(submitted) == 1
    assert submitted[0].conversation == "whatsapp:12345"


def test_build_subagent_prompt_does_not_start_skill_watcher(monkeypatch, tmp_path):
    """build_subagent_prompt uses a transient ContextBuilder just for
    _build_runtime_context; it must not leave a skill-catalog file watcher
    running behind it (one leaked watchfiles/inotify thread per spawn)."""
    import raven.agent.context as context_mod
    from raven.agent.subagent.backends.raven_loop import build_subagent_prompt

    calls = []
    real_init = context_mod.ContextBuilder.__init__

    def _spy_init(self, workspace, *args, **kwargs):
        calls.append(kwargs.get("start_watcher", True))
        return real_init(self, workspace, *args, **kwargs)

    monkeypatch.setattr(context_mod.ContextBuilder, "__init__", _spy_init)

    build_subagent_prompt(tmp_path, tmp_path / "session")

    assert calls == [False]


def test_build_subagent_prompt_hides_orchestration_skills(tmp_path):
    """The catalog handed to a sub-agent must not advertise a skill whose
    procedure needs a tool this backend never registers — the DAG skill tells
    the reader to call run_subagent_dag, which only the main agent has."""
    from raven.agent.subagent.backends.raven_loop import build_subagent_prompt

    prompt = build_subagent_prompt(tmp_path, tmp_path / "session", ["read_file", "exec"])

    assert "subagent-dag-orchestration" not in prompt
    assert "run_subagent_dag" not in prompt
    # The filter is targeted, not a blanket catalog suppression.
    assert "weather" in prompt


def test_build_subagent_prompt_gates_on_the_declaration_not_a_skill_name(tmp_path):
    """The rule is `requires.tools`, not a hardcoded name list: a sub-agent that
    somehow did hold run_subagent_dag would see the guide, and any future
    tool-gated skill is covered without editing this backend."""
    from raven.agent.subagent.backends.raven_loop import build_subagent_prompt

    prompt = build_subagent_prompt(tmp_path, tmp_path / "session", ["read_file", "run_subagent_dag"])

    assert "subagent-dag-orchestration" in prompt


def test_build_subagent_prompt_without_a_tool_list_gates_everything_tool_bound(tmp_path):
    """A sub-agent prompt is always built next to its own registry, so "no names"
    means "no tools" — the opposite of the main agent's segment builder, where an
    unanswerable tool lookup has to degrade to showing everything."""
    from raven.agent.subagent.backends.raven_loop import build_subagent_prompt

    prompt = build_subagent_prompt(tmp_path, tmp_path / "session")

    assert "subagent-dag-orchestration" not in prompt
    assert "weather" in prompt  # declares no requires.tools


async def test_dag_tool_refuses_to_run_inside_a_subagent(tmp_path):
    """Backstop for the tool layer: even if a future backend registers the DAG
    tool on a sub-agent, the call fails loudly instead of fanning out."""
    from raven.agent.subagent.backends.base import IN_SUBAGENT_RUN
    from raven.agent.subagent_dag.tool import SubAgentDagTool
    from raven.agent.tools.base import ToolResult

    tool = SubAgentDagTool(workspace=tmp_path)

    token = IN_SUBAGENT_RUN.set(True)
    try:
        blocked = await tool.execute(nodes=[])
    finally:
        IN_SUBAGENT_RUN.reset(token)

    assert blocked.startswith("Error:")
    assert "not available inside a sub-agent run" in blocked

    # Same call from the main agent's context is accepted (empty graph → no-op).
    # A refusal is a plain error string; an accepted run comes back as a
    # ToolResult carrying the transcript label alongside the model text.
    assert isinstance(await tool.execute(nodes=[]), ToolResult)


async def test_raven_loop_backend_marks_the_subagent_context(tmp_path):
    """RavenLoopBackend.run is what sets the flag the DAG guard reads, and it
    must restore the previous value so the main agent keeps its own tools."""
    from raven.agent.subagent.backends.base import IN_SUBAGENT_RUN
    from raven.agent.subagent.backends.raven_loop import RavenLoopBackend

    backend = RavenLoopBackend(provider=object(), model="m", agent_home=tmp_path)
    seen: list[bool] = []

    async def _probe(task, **kwargs):
        seen.append(IN_SUBAGENT_RUN.get())
        return "done"

    backend._run = _probe

    assert IN_SUBAGENT_RUN.get() is False
    await backend.run("t", task_id="1", workspace=tmp_path, executor=None)

    assert seen == [True]
    assert IN_SUBAGENT_RUN.get() is False


RAVEN_ROW = ("s1", "raven", "notes")


async def test_default_subagent_gets_a_registry_row(tmp_path, monkeypatch):
    from raven.agent.subagent import manager as manager_mod
    from raven.agent.subagent.instances import InstanceRegistry

    registry = InstanceRegistry(tmp_path / "reg.json")
    monkeypatch.setattr(manager_mod, "get_registry", lambda: registry)

    await manager_mod._write_spawn_status("s1", None, "notes", "running")

    rows = registry.list_instances("s1")
    assert [(r["sessionKey"], r["agent"], r["handle"]) for r in rows] == [RAVEN_ROW]
    # `upsert_spawn` hardcodes kind="cli" (instances.py:115). Semantically off for
    # the built-in sub-agent, but deliberately unchanged: the web RPC's
    # reconciliation branches on kind == "cli" to decide whether to consult
    # live_handles, and a new kind would silently stop reconciling these rows.
    assert rows[0]["kind"] == "cli"


def _stub_manager() -> SimpleNamespace:
    return SimpleNamespace()


def test_instance_is_accepted_for_the_default_subagent():
    from raven.agent.tools.spawn import SpawnTool

    tool = SpawnTool(manager=_stub_manager())
    assert tool._reject_useless_instance(None, "refactor-auth") is None


async def test_subagent_reuses_main_provider_and_forwards_api_key(monkeypatch):
    """A subagent runs in-process against the exact provider instance the main
    agent was built with (manager.provider), not a fresh one — so an api_key
    set only on the main instance must still reach acompletion for the
    subagent's own chat_with_retry() calls.

    A reported spawn-subagent 401 could not be reproduced by reading the
    code (chat()/chat_stream() already pass api_key explicitly to
    acompletion), but nothing in the test suite actually asserted that
    kwarg ever arrived -- this pins it down.
    """
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="ok", tool_calls=None),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )

    monkeypatch.setattr(
        "raven.providers.litellm_provider.acompletion",
        fake_acompletion,
    )

    provider = LiteLLMProvider(api_key="k-main", default_model="openai/gpt-4o")
    manager = SubagentManager(provider=provider, workspace=Path("/tmp"))

    assert manager.provider is provider

    response = await manager.provider.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}],
        model="openai/gpt-4o",
    )

    assert response.finish_reason != "error"
    assert captured["api_key"] == "k-main"


from raven.providers.base import LLMProvider, LLMResponse  # noqa: E402


class _WhitelistStubProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test")

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        return LLMResponse(content="done", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


async def test_raven_loop_backend_tools_allow_filters_the_registry(tmp_path, monkeypatch):
    """A per-role whitelist decides what gets registered at all: a withheld
    tool never reaches the registry, so it never appears in the LLM's tools
    field -- restriction by absence, not by instruction."""
    from raven.agent.subagent.backends.raven_loop import RavenLoopBackend
    from raven.agent.tools.registry import ToolRegistry

    registered: list[list[str]] = []
    real = ToolRegistry.register

    def _spy(self, tool):
        real(self, tool)
        registered[-1].append(tool.name)

    monkeypatch.setattr(ToolRegistry, "register", _spy)

    async def _names(**kw) -> list[str]:
        registered.append([])
        backend = RavenLoopBackend(provider=_WhitelistStubProvider(), model="stub", agent_home=tmp_path, **kw)
        await backend.run("task", task_id="t1", workspace=tmp_path, executor=None)
        return registered[-1]

    full = await _names()
    only_read = await _names(tools_allow=["read_file"])
    none_at_all = await _names(tools_allow=[])

    assert {"read_file", "write_file", "exec", "web_fetch"} <= set(full)
    assert only_read == ["read_file"]
    assert none_at_all == []


def test_build_subagent_prompt_skills_allow_narrows_the_menu(tmp_path):
    """None keeps the full catalog, [] hides the menu, a list shows only the
    named skills."""
    from raven.agent.subagent.backends.raven_loop import build_subagent_prompt

    default = build_subagent_prompt(tmp_path, tmp_path / "s", ["read_file"])
    named = build_subagent_prompt(tmp_path, tmp_path / "s", ["read_file"], skills_allow=["weather"])
    hidden = build_subagent_prompt(tmp_path, tmp_path / "s", ["read_file"], skills_allow=[])

    assert "weather" in default
    assert "weather" in named
    assert "weather" not in hidden


def test_build_subagent_prompt_skills_allow_stacks_with_the_tool_filter(tmp_path):
    """Whitelisting a skill does not smuggle it past the requires.tools gate:
    a skill whose procedure needs a tool this backend lacks stays hidden even
    when named."""
    from raven.agent.subagent.backends.raven_loop import build_subagent_prompt

    prompt = build_subagent_prompt(
        tmp_path,
        tmp_path / "s",
        ["read_file"],
        skills_allow=["subagent-dag-orchestration", "weather"],
    )

    assert "subagent-dag-orchestration" not in prompt
    assert "weather" in prompt


def _spawn_origin(agent: str | None, instance: str | None) -> dict[str, Any]:
    return {
        "channel": "web",
        "chat_id": "default",
        "session_key": "web:sess1",
        "agent": agent,
        "instance": instance,
        "handle": instance or "abcd1234",
    }


async def test_announcement_carries_the_instance_handle() -> None:
    mgr = _make_manager(max_concurrent=1)
    submitted: list[Any] = []
    mgr.set_submit(submitted.append)

    await mgr._announce_result(
        "abcd1234", "Refactor", "do it", "done", _spawn_origin("claude_code", "refactor-auth-a3f9c1"), "ok"
    )

    assert "Instance handle: refactor-auth-a3f9c1" in submitted[0].text


async def test_announcement_omits_the_handle_line_for_a_stateless_call() -> None:
    mgr = _make_manager(max_concurrent=1)
    submitted: list[Any] = []
    mgr.set_submit(submitted.append)

    await mgr._announce_result("abcd1234", "One shot", "do it", "done", _spawn_origin("oneshot", None), "ok")

    assert "Instance handle" not in submitted[0].text


async def test_announcement_no_longer_tells_the_model_to_drop_technical_detail() -> None:
    """The old wording made the handle a forbidden 'technical detail', which
    would have had the model discard the thing it was just handed."""
    mgr = _make_manager(max_concurrent=1)
    submitted: list[Any] = []
    mgr.set_submit(submitted.append)

    await mgr._announce_result("abcd1234", "Summarize", "do it", "done", _spawn_origin(None, "summarize-a1b2c3"), "ok")

    assert 'Do not mention technical details like "subagent" or task IDs' not in submitted[0].text
    assert "out of what you say to the user" in submitted[0].text
