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


async def test_subagent_stops_after_terminal_shell_decision(monkeypatch, tmp_path):
    provider = _DeleteRetryProvider()
    manager = SubagentManager(provider=provider, workspace=tmp_path)
    executor = _RecordingExecutor()
    announcements: list[dict[str, str]] = []

    monkeypatch.setattr(manager, "_build_subagent_prompt", lambda: "system")

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
            "result": manager_mod._ABORTED_ACTION_RESULT,
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


def test_build_subagent_prompt_does_not_start_skill_watcher(monkeypatch):
    """_build_subagent_prompt uses a transient ContextBuilder just for
    _build_runtime_context; it must not leave a skill-catalog file watcher
    running behind it (one leaked watchfiles/inotify thread per spawn)."""
    import raven.agent.context as context_mod

    calls = []
    real_init = context_mod.ContextBuilder.__init__

    def _spy_init(self, workspace, *args, **kwargs):
        calls.append(kwargs.get("start_watcher", True))
        return real_init(self, workspace, *args, **kwargs)

    monkeypatch.setattr(context_mod.ContextBuilder, "__init__", _spy_init)

    mgr = _make_manager(max_concurrent=1)
    mgr._build_subagent_prompt()

    assert calls == [False]


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
