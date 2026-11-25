"""Tests for BackgroundSettle: reading Claude Code's raw SDK messages to decide when a run's work is over."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from loguru import logger

from raven.acp_client import acp_agent, background_settle
from raven.acp_client.acp_agent import _SESSION_STATE, AcpAgentBackend, _TurnCollector
from raven.acp_client.background_settle import BackgroundSettle, wake_marker
from raven.acp_client.capabilities import CapabilitySnapshot, snapshot_fingerprint
from raven.acp_client.pool import _SessionRouter, close_pool, get_pool
from raven.acp_client.unprompted import UnpromptedRecorder
from raven.agent.subagent import activity
from raven.agent.subagent.backends import build_third_party_backend
from raven.agent.subagent.instances import InstanceRegistry
from raven.config.schema import ThirdPartyAcpSubagentConfig
from raven.utils.commands import command_quote
from tests.acp_frames import CLAUDE_SETTLE_BASH_MONITOR, CLAUDE_SETTLE_SUBAGENT

_STUB = Path(__file__).with_name("acp_stub_server.py")
_CLAUDE_AGENT = "@agentclientprotocol/claude-agent-acp"


@pytest.fixture(autouse=True)
async def _no_pooled_connections():
    _SESSION_STATE.clear()
    yield
    await close_pool()


def _settle_config(name: str, timeout: int | None = None, **env: str) -> ThirdPartyAcpSubagentConfig:
    return ThirdPartyAcpSubagentConfig(
        name=name,
        command=f"{command_quote(sys.executable)} {command_quote(str(_STUB))}",
        env={"ACP_STUB_MODE": "settle", "ACP_STUB_AGENT_NAME": _CLAUDE_AGENT, **env},
        ready_timeout_ms=15000,
        timeout=timeout,
    )


def _settle_backend(name: str, timeout: int | None = None, **env: str) -> AcpAgentBackend:
    return build_third_party_backend(_settle_config(name, timeout, **env))


async def _stderr_after_a_run(backend: AcpAgentBackend, workspace: Path) -> str:
    await backend.run("ping", task_id="t1", workspace=workspace, executor=None)
    return "".join(connection.client.stderr_tail() for connection in get_pool().live(backend.name))


def _replay(frames: list[tuple[float, str, dict[str, Any]]]) -> tuple[BackgroundSettle, list[tuple[float, bool]]]:
    tool_names: dict[str, str] = {}
    settle = BackgroundSettle(lambda call_id: tool_names.get(call_id) == "Monitor")
    checks: list[tuple[float, bool]] = []
    for t, method, params in frames:
        if method == "session/update":
            update = params["update"]
            if update["sessionUpdate"] == "tool_call":
                tool_names[update["toolCallId"]] = update["_meta"]["claudeCode"]["toolName"]
        elif method == "answer":
            settle.prompt_answered()
        elif method == background_settle.RAW_MESSAGE_METHOD:
            settle.apply(params["message"], t)
        checks.append((t, settle.done(t)))
    return settle, checks


def _state(state: str) -> dict[str, Any]:
    return {"type": "system", "subtype": "session_state_changed", "state": state}


def _level(*tasks: dict[str, Any]) -> dict[str, Any]:
    return {"type": "system", "subtype": "background_tasks_changed", "tasks": list(tasks)}


def _started(task_id: str, call_id: str, description: str = "") -> dict[str, Any]:
    return {
        "type": "system",
        "subtype": "task_started",
        "task_id": task_id,
        "tool_use_id": call_id,
        "description": description or task_id,
    }


def _task(task_id: str, description: str = "", **extra: Any) -> dict[str, Any]:
    return {"task_id": task_id, "task_type": "local_bash", "description": description or task_id, **extra}


def _never_monitor(call_id: str) -> bool:
    return False


def _idle_with_live_task(description: str = "t1 job") -> BackgroundSettle:
    settle = BackgroundSettle(_never_monitor)
    settle.apply(_state("running"), 0.0)
    settle.apply(_started("t1", "c1", description), 1.0)
    settle.apply(_level(_task("t1", description)), 1.0)
    settle.prompt_answered()
    settle.apply(_state("idle"), 5.0)
    return settle


def test_bash_run_is_done_only_after_the_last_idle():
    settle, checks = _replay(CLAUDE_SETTLE_BASH_MONITOR)
    first_done = next(t for t, done in checks if done)
    assert first_done == pytest.approx(110.67, abs=0.01)  # last idle; the answer came at 35.71
    assert settle.reporting


def test_monitor_task_does_not_hold_the_run():
    settle, _ = _replay(CLAUDE_SETTLE_BASH_MONITOR)
    assert settle.running_tasks() == []  # the Monitor task is still in the live set here


def test_subagent_run_is_done_at_the_answer_after_the_idle():
    settle, checks = _replay(CLAUDE_SETTLE_SUBAGENT)
    assert next(t for t, done in checks if done) == pytest.approx(116.11, abs=0.01)


def test_wake_after_the_answer_reports_the_ended_task():
    wakes: list[list[str] | None] = []
    tool_names: dict[str, str] = {}
    settle = BackgroundSettle(lambda call_id: tool_names.get(call_id) == "Monitor")
    answered = False
    for t, method, params in CLAUDE_SETTLE_BASH_MONITOR:
        if method == "session/update":
            update = params["update"]
            if update["sessionUpdate"] == "tool_call":
                tool_names[update["toolCallId"]] = update["_meta"]["claudeCode"]["toolName"]
        elif method == "answer":
            settle.prompt_answered()
            answered = True
        elif method == background_settle.RAW_MESSAGE_METHOD:
            result = settle.apply(params["message"], t)
            if answered and result is not None:
                wakes.append(result)
    assert wakes == [[]]

    settle = _idle_with_live_task("Sleep 40s then print marker")
    settle.apply(_level(), 10.0)
    assert settle.apply(_state("running"), 10.044) == ["Sleep 40s then print marker"]


def test_task_ending_while_idle_waits_for_the_wake():
    settle = _idle_with_live_task()
    settle.apply(_level(), 10.0)
    assert settle.done(10.04) is False
    settle.apply(_state("running"), 10.044)
    settle.apply(_state("idle"), 12.0)
    assert settle.done(12.0) is True


def test_task_ending_while_idle_with_no_wake_is_done_after_15s():
    settle = _idle_with_live_task()
    settle.apply(_level(), 10.0)
    assert settle.done(24.9) is False
    assert settle.done(25.1) is True


def test_unwoken_bound_is_read_at_call_time(monkeypatch: pytest.MonkeyPatch):
    settle = _idle_with_live_task()
    settle.apply(_level(), 10.0)
    monkeypatch.setattr(background_settle, "UNWOKEN_END_S", 1.0)
    assert settle.done(11.1) is True


def test_a_task_from_an_earlier_run_does_not_hold():
    settle = BackgroundSettle(_never_monitor)
    settle.apply(_state("running"), 0.0)
    settle.apply(_level(_task("t-old")), 1.0)
    settle.prompt_answered()
    settle.apply(_state("idle"), 5.0)
    assert settle.running_tasks() == []
    assert settle.done(5.0) is True


def test_monitor_named_after_its_task_started():
    tool_names: dict[str, str] = {}
    settle = BackgroundSettle(lambda call_id: tool_names.get(call_id) == "Monitor")
    settle.apply(_state("running"), 0.0)
    settle.apply(_started("t2", "c2"), 1.0)
    settle.apply(_level(_task("t2")), 1.0)
    settle.prompt_answered()
    settle.apply(_state("idle"), 5.0)
    assert settle.done(5.0) is False
    tool_names["c2"] = "Monitor"
    assert settle.done(5.0) is True


def test_requires_action_is_not_done():
    settle = BackgroundSettle(_never_monitor)
    settle.apply(_state("running"), 0.0)
    settle.prompt_answered()
    settle.apply(_state("requires_action"), 5.0)
    assert settle.state == "requires_action"
    assert settle.done(5.0) is False


def test_ambient_task_does_not_hold():
    settle = BackgroundSettle(_never_monitor)
    settle.apply(_state("running"), 0.0)
    settle.apply(_started("t3", "c3"), 1.0)
    settle.apply(_level(_task("t3", ambient=True)), 1.0)
    settle.prompt_answered()
    settle.apply(_state("idle"), 5.0)
    assert settle.done(5.0) is True


def test_no_state_message_means_not_reporting():
    settle = BackgroundSettle(_never_monitor)
    settle.apply(_started("t1", "c1"), 1.0)
    settle.apply(_level(_task("t1")), 1.0)
    assert settle.reporting is False
    assert settle.state is None


def test_usage_sums_results_not_the_answer():
    settle, _ = _replay(CLAUDE_SETTLE_SUBAGENT)
    assert settle.usage_counts() == {
        "inputTokens": 4 + 2 + 0,
        "outputTokens": 173 + 11 + 0,
        "cachedReadTokens": 34051 + 34617,
        "cachedWriteTokens": 34614 + 2969,
        "totalTokens": 6 + 184 + 68668 + 37583,
    }


def test_usage_is_none_without_a_result():
    assert BackgroundSettle(_never_monitor).usage_counts() is None


def test_wake_marker_text():
    assert (
        wake_marker("Claude Code", ["Sleep 40s then print marker"])
        == "[Claude Code resumed on its own: Sleep 40s then print marker ended]"
    )
    assert wake_marker("Claude Code", []) == "[Claude Code resumed on its own: a background event]"


def test_a_permission_prompt_inside_a_wake_turn_adds_no_second_marker():
    settle = _idle_with_live_task()
    settle.apply(_level(), 10.0)
    assert settle.apply(_state("running"), 10.044) == ["t1 job"]
    assert settle.apply(_state("requires_action"), 11.0) is None
    assert settle.apply(_state("running"), 12.0) is None


def test_a_task_that_ended_during_a_turn_is_not_named_by_a_later_wake():
    settle = BackgroundSettle(_never_monitor)
    settle.apply(_state("running"), 0.0)
    settle.apply(_started("t1", "c1", "first job"), 1.0)
    settle.apply(_level(_task("t1", "first job")), 1.0)
    settle.prompt_answered()
    settle.apply(_level(), 6.0)
    settle.apply(_state("idle"), 7.0)
    assert settle.apply(_state("running"), 20.0) == []


@pytest.mark.parametrize(
    "message",
    [
        "result",
        None,
        ["type", "result"],
        {"type": "result", "usage": "lots"},
        {"type": "result", "usage": {"output_tokens": "many", "input_tokens": None, "cache_read_input_tokens": [1]}},
        {"type": "system", "subtype": "background_tasks_changed", "tasks": "none"},
        {"type": "system", "subtype": "background_tasks_changed", "tasks": [None, 3, {"task_id": 7}]},
        {"type": "system", "subtype": "task_started", "task_id": ["x"]},
        {"type": "system", "subtype": "session_state_changed", "state": 5},
    ],
)
def test_malformed_raw_messages_are_ignored(message: Any):
    settle = BackgroundSettle(_never_monitor)
    assert settle.apply(message, 1.0) is None
    counts = settle.usage_counts()
    assert counts is None or counts["outputTokens"] == 0
    assert settle.running_tasks() == []


_EXPECTED_META = {"claudeCode": {"emitRawSDKMessages": background_settle.RAW_MESSAGE_FILTER}}


async def test_session_new_carries_the_opt_in_on_the_wire(tmp_path):
    tail = await _stderr_after_a_run(_settle_backend("settle-new"), tmp_path)
    assert f"stub: session/new meta={json.dumps(_EXPECTED_META, sort_keys=True)}" in tail


async def test_another_agent_sends_no_meta(tmp_path):
    backend = _settle_backend("settle-other", ACP_STUB_AGENT_NAME="stub-agent")
    tail = await _stderr_after_a_run(backend, tmp_path)
    assert "meta=" not in tail


class _RecordingClient:
    prompting = False

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def request(self, method: str, params: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        self.calls.append((method, params))
        return {"sessionId": "fresh-1"} if method == "session/new" else {}


class _KnownSession:
    async def lookup(self, *args: Any, **kwargs: Any) -> str:
        return "known-1"


async def _open(snapshot: Any, *, with_meta: bool = True, fresh: bool = False) -> list[tuple[str, dict[str, Any]]]:
    backend = _settle_backend("settle-direct")
    backend._snapshot = snapshot
    backend._registry = _KnownSession()
    client = _RecordingClient()
    if fresh:
        backend._registry = SimpleNamespace(lookup=_none)
    meta = backend_meta() if with_meta else None
    await backend._open_session(
        client,
        cwd="/work",
        skey="s",
        handle="h",
        budget=5.0,
        mcp_servers=[],
        router=_SessionRouter("settle-direct"),
        session_meta=meta,
    )
    return client.calls


async def _none(*args: Any, **kwargs: Any) -> None:
    return None


def backend_meta() -> dict[str, Any]:
    from raven.acp_client.acp_dialects import ClaudeCodeDialect

    return ClaudeCodeDialect().session_open_meta()


def _snapshot(*, load: bool, resume: bool) -> Any:
    return SimpleNamespace(can_load=load, can_resume=resume, agent_name="claude")


@pytest.fixture
def _quiet_session_bookkeeping(monkeypatch):
    async def _noop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(AcpAgentBackend, "_settle_session", _noop)
    for name in ("_relearn_menus", "_note_session_model", "_note_session_effort", "_note_session_mode"):
        monkeypatch.setattr(AcpAgentBackend, name, lambda self, *a, **k: None)


@pytest.mark.usefixtures("_quiet_session_bookkeeping")
async def test_new_load_and_resume_carry_the_same_meta():
    new_calls = await _open(_snapshot(load=True, resume=True), fresh=True)
    load_calls = await _open(_snapshot(load=True, resume=True))
    resume_calls = await _open(_snapshot(load=False, resume=True))
    assert [m for m, _ in new_calls] == ["session/new"]
    assert [m for m, _ in load_calls] == ["session/load"]
    assert [m for m, _ in resume_calls] == ["session/resume"]
    metas = [params["_meta"] for _, params in new_calls + load_calls + resume_calls]
    assert metas == [_EXPECTED_META] * 3


@pytest.mark.usefixtures("_quiet_session_bookkeeping")
async def test_no_meta_is_sent_when_the_dialect_offers_none():
    calls = await _open(_snapshot(load=True, resume=True), with_meta=False)
    new_calls = await _open(_snapshot(load=True, resume=True), with_meta=False, fresh=True)
    assert all("_meta" not in params for _, params in calls + new_calls)


async def _feed(frames: list[tuple[float, str, dict[str, Any]]], agent: str = "Claude Code"):
    from raven.acp_client.acp_dialects import ClaudeCodeDialect

    seen: list[str] = []

    async def sink(text: str) -> None:
        seen.append(text)

    collector = _TurnCollector(on_delta=sink, dialect=ClaudeCodeDialect(), agent=agent)
    for _, method, params in frames:
        if method == "answer":
            collector.prompt_answered()
        else:
            await collector(method, params)
    return collector, seen


async def test_collector_reply_is_the_last_wake_turn():
    collector, _ = await _feed(CLAUDE_SETTLE_BASH_MONITOR)
    assert collector.reply == "final: BG_DONE"
    assert collector.closing_text == "final: BG_DONE"


async def test_each_wake_turn_gets_a_marker_row():
    collector, _ = await _feed(CLAUDE_SETTLE_BASH_MONITOR)
    users = [e["text"] for e in collector.events if e["t"] == "user"]
    assert users == ["[Claude Code resumed on its own: a background event]"]


async def test_a_speaking_wake_is_not_glued_to_the_text_before_it():
    _, seen = await _feed(CLAUDE_SETTLE_BASH_MONITOR)
    assert "started\n\nevent: PING_1\n\nevent: PING_2" in "".join(seen)


async def test_turn_counts_come_from_the_raw_results():
    collector, _ = await _feed(CLAUDE_SETTLE_BASH_MONITOR)
    assert collector.turn_counts({"outputTokens": 999})["outputTokens"] == 249 + 8 + 8 + 169


async def test_a_raw_message_before_any_answer_adds_no_marker():
    collector, _ = await _feed(CLAUDE_SETTLE_SUBAGENT)
    assert [e for e in collector.events if e["t"] == "user"] == []


async def test_every_raw_message_sets_settle_changed_and_a_bad_one_does_not_raise():
    collector, _ = await _feed([])
    assert not collector.settle_changed.is_set()
    await collector(background_settle.RAW_MESSAGE_METHOD, {"message": "garbage"})
    assert collector.settle_changed.is_set()
    await collector(background_settle.RAW_MESSAGE_METHOD, {})


async def test_turn_counts_fall_back_without_raw_results():
    collector, _ = await _feed([])
    assert collector.turn_counts({}) == collector.usage


def _chunk(text: str) -> tuple[float, str, dict[str, Any]]:
    update = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}
    return (0.0, "session/update", {"sessionId": "s", "update": update})


async def test_text_after_the_answer_starts_a_new_message_once():
    answer = (0.0, "answer", {})
    collector, _ = await _feed([_chunk("started"), answer, _chunk("next"), answer, _chunk(" more")])
    assert collector.reply == "next more"


def _scenario(name: str, scenario: str, **env: str) -> AcpAgentBackend:
    return _settle_backend(name, ACP_STUB_SETTLE=scenario, **env)


async def _go(backend: AcpAgentBackend, workspace: Path) -> str:
    return await backend.run("go", task_id="t", workspace=workspace, executor=None)


def _stderr(backend: AcpAgentBackend) -> str:
    return "".join(connection.client.stderr_tail() for connection in get_pool().live(backend.name))


@pytest.fixture
def unprompted_updates(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The sessions whose ``session/update`` reached the connection's resident sink."""
    seen: list[str] = []
    original = UnpromptedRecorder.__call__

    async def spy(self: UnpromptedRecorder, method: str, params: dict[str, Any]) -> None:
        if method == "session/update":
            seen.append(str(params.get("sessionId")))
        await original(self, method, params)

    monkeypatch.setattr(UnpromptedRecorder, "__call__", spy)
    return seen


async def test_bash_run_returns_the_final_answer_once(tmp_path, unprompted_updates):
    out = await _go(_scenario("settle-bash", "bash"), tmp_path)
    assert out == "final: done"
    assert unprompted_updates == []


async def test_monitor_does_not_hold_the_run(tmp_path):
    started = time.monotonic()
    out = await _go(_scenario("settle-monitor", "bash_monitor"), tmp_path)
    assert out == "final: done"
    assert time.monotonic() - started < 5


async def test_task_ending_while_idle_waits_for_the_wake(tmp_path):
    assert await _go(_scenario("settle-idle-end", "idle_end"), tmp_path) == "final: done"


async def test_unwoken_task_end_returns_after_the_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(background_settle, "UNWOKEN_END_S", 0.5)
    assert await _go(_scenario("settle-nowake", "idle_end_nowake"), tmp_path) == "started"


async def test_no_state_message_returns_on_the_answer(tmp_path):
    started = time.monotonic()
    assert await _go(_scenario("settle-no-state", "no_state"), tmp_path) == "pong"
    assert time.monotonic() - started < 2


async def test_ceiling_returns_with_a_notice(tmp_path, monkeypatch):
    monkeypatch.setattr(background_settle, "SETTLE_CEILING_S", 1.0)
    out = await _go(_scenario("settle-ceiling", "forever"), tmp_path)
    assert out == (
        "started\n\n[raven] settle-ceiling still had background work running after 0 min: sleep job. "
        "Anything it reports later arrives as its own report."
    )


async def test_ceiling_with_no_task_left_says_it_was_still_working(tmp_path, monkeypatch):
    monkeypatch.setattr(background_settle, "SETTLE_CEILING_S", 1.0)
    out = await _go(_scenario("settle-ceiling-empty", "idle_end_nowake"), tmp_path)
    assert out == (
        "started\n\n[raven] settle-ceiling-empty was still working after 0 min. "
        "Anything it reports later arrives as its own report."
    )


@pytest.fixture
def _one_second_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acp_agent, "_prompt_deadline", lambda configured: 1)


@pytest.mark.usefixtures("_one_second_deadline")
async def test_deadline_with_no_task_left_says_it_was_still_working(tmp_path):
    out = await _go(_scenario("settle-deadline-empty", "idle_end_nowake"), tmp_path)
    assert out == (
        "started\n\n[raven] settle-deadline-empty was still working at its deadline. "
        "Anything it reports later arrives as its own report."
    )


@pytest.mark.usefixtures("_one_second_deadline")
async def test_deadline_bounds_prompt_and_settle_together(tmp_path):
    started = time.monotonic()
    out = await _go(_scenario("settle-deadline", "forever"), tmp_path)
    assert time.monotonic() - started < 10
    assert out == (
        "started\n\n[raven] settle-deadline still had background work running at its deadline: sleep job. "
        "Anything it reports later arrives as its own report."
    )


async def test_connection_lost_during_settle_returns_the_reply(tmp_path):
    out = await _go(_scenario("settle-dies", "dies"), tmp_path)
    assert out == "started\n\n[raven] the connection to settle-dies closed before its background work ended."


def _seen_by_settle(monkeypatch: pytest.MonkeyPatch, seen: list[_TurnCollector]) -> None:
    settle = AcpAgentBackend._settle

    async def entered(self: AcpAgentBackend, **kwargs: Any) -> str | None:
        seen.append(kwargs["collector"])
        return await settle(self, **kwargs)

    monkeypatch.setattr(AcpAgentBackend, "_settle", entered)


async def test_a_wake_in_the_answer_s_own_read_burst_counts_as_after_the_answer(tmp_path, monkeypatch):
    seen: list[_TurnCollector] = []
    _seen_by_settle(monkeypatch, seen)
    out = await _go(_scenario("settle-burst", "burst"), tmp_path)
    assert out == "final: burst"
    assert [e["text"] for e in seen[0].events if e["t"] == "user"] == [
        "[settle-burst resumed on its own: sleep job ended]"
    ]


async def test_an_error_inside_the_settle_returns_the_reply_it_has(tmp_path, monkeypatch):
    done = BackgroundSettle.done
    calls: list[float] = []

    def fails_once(self: BackgroundSettle, now: float) -> bool:
        calls.append(now)
        if len(calls) == 1:
            raise RuntimeError("settle state broke")
        return done(self, now)

    monkeypatch.setattr(BackgroundSettle, "done", fails_once)
    logs: list[str] = []
    sink = logger.add(lambda message: logs.append(str(message)), level="INFO")
    try:
        out = await _go(_scenario("settle-error", "forever"), tmp_path)
    finally:
        logger.remove(sink)
    assert out == "started"
    assert any("settle-error" in line and "settle ended: error" in line for line in logs)
    assert any("settle state broke" in line for line in logs)


def _stateful(name: str, registry: InstanceRegistry, **env: str) -> AcpAgentBackend:
    cfg = _settle_config(name, **env)
    snapshot = CapabilitySnapshot(
        agent=name,
        fingerprint=snapshot_fingerprint(cfg),
        status="ready",
        detail="",
        measured_at_ms=1,
        can_resume=True,
        can_load=True,
    )
    return AcpAgentBackend(name=name, command=cfg.command, env=dict(cfg.env), snapshot=snapshot, registry=registry)


async def test_cancel_during_settle_sends_cancel_and_keeps_the_binding(tmp_path, monkeypatch):
    registry = InstanceRegistry(path=tmp_path / "instances.json")
    backend = _stateful("settle-cancel", registry, ACP_STUB_SETTLE="forever")
    settling = asyncio.Event()
    settle = AcpAgentBackend._settle

    async def entered(self: AcpAgentBackend, **kwargs: Any) -> str | None:
        settling.set()
        return await settle(self, **kwargs)

    monkeypatch.setattr(AcpAgentBackend, "_settle", entered)
    run = asyncio.create_task(
        backend.run("go", task_id="t", workspace=tmp_path, session_key="s", instance="work", executor=None)
    )
    await asyncio.wait_for(settling.wait(), timeout=10)
    await asyncio.sleep(0.5)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    bound = await registry.lookup("s", "settle-cancel", "work", kind="acp")
    assert bound is not None, "the cancelled settle cost the first turn its binding"
    for _ in range(40):
        if f"stub: session/cancel {bound}" in _stderr(backend):
            break
        await asyncio.sleep(0.05)
    assert f"stub: session/cancel {bound}" in _stderr(backend)


async def test_cancel_during_settle_books_the_usage_spent_so_far(tmp_path, monkeypatch):
    booked: list[dict[str, Any]] = []

    async def book(counts: dict[str, Any], **kwargs: Any) -> None:
        booked.append(counts)

    monkeypatch.setattr(activity, "note_ledger_usage", book)
    settling = asyncio.Event()
    settle = AcpAgentBackend._settle

    async def entered(self: AcpAgentBackend, **kwargs: Any) -> str | None:
        settling.set()
        return await settle(self, **kwargs)

    monkeypatch.setattr(AcpAgentBackend, "_settle", entered)
    run = asyncio.create_task(_go(_scenario("settle-cancel-usage", "forever_spent"), tmp_path))
    await asyncio.wait_for(settling.wait(), timeout=10)
    await asyncio.sleep(0.5)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    assert len(booked) == 1
    assert (booked[0]["prompt_tokens"], booked[0]["completion_tokens"]) == (3 + 7, 5 + 11)


async def test_quiet_background_work_stamps_the_run_alive(tmp_path, monkeypatch):
    monkeypatch.setattr(background_settle, "ALIVE_EVERY_S", 0.2)
    monkeypatch.setattr(background_settle, "SETTLE_CEILING_S", 1.0)
    stamps: list[float] = []
    note_alive = activity.note_alive

    def spy(run: Any) -> None:
        if sys._getframe(1).f_code.co_name == "_settle":
            stamps.append(time.monotonic())
        note_alive(run)

    monkeypatch.setattr(activity, "note_alive", spy)
    await _go(_scenario("settle-alive", "forever"), tmp_path)
    assert len(stamps) >= 3


async def test_another_agent_returns_on_the_answer(tmp_path):
    started = time.monotonic()
    out = await _go(_scenario("settle-not-claude", "forever", ACP_STUB_AGENT_NAME="stub-agent"), tmp_path)
    assert out == "started"
    assert time.monotonic() - started < 2
