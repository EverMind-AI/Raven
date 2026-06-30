"""SubagentManager concurrency gate.

Isolates the gate: build_executor and _run_subagent_inner are stubbed, so the
test drives only the Semaphore in _run_subagent (no real VM, no real LLM). A
stubbed inner holds each subagent inside the gate on an Event, letting the test
observe the concurrent peak.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from raven.agent.subagent import manager as manager_mod
from raven.agent.subagent.manager import SubagentManager
from raven.config.schema import AgentDefaults


class _StubProvider:
    def get_default_model(self) -> str:
        return "stub-model"


class _DummyExecutor:
    async def __aenter__(self) -> "_DummyExecutor":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


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

    async def _stub_inner(task_id, task, label, origin, executor) -> None:
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

    clock[0] += manager_mod._SPAWN_WINDOW_SECONDS + 1     # first spawn ages out
    assert "started" in await mgr.spawn(task="c")         # recovered


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
