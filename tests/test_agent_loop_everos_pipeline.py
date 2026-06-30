"""Deterministic full-turn coverage of the channel -> memory -> EverOS pipeline.

Drives the **real** ``AgentLoop._process_message`` with a stub LLM provider
and a fake :class:`MemoryBackend`, pinning the integration seam that only the
``real_llm``-gated e2e (``tests/integration/test_everos_channel_e2e.py``)
otherwise exercises — but here without an LLM, so it runs in normal CI.

Memory model under test:
  - **recall** is on-miss: the ``# Memory`` segment queries EverOS (``user_id``)
    only when native long-term surfaces nothing relevant; a cold workspace is a
    miss, so recall fires. The skill-router lane (``agent_id``) is independent.
  - **store** is NOT per-turn. Native short-term compaction owns the per-turn
    context budget; EverOS long-term consolidation is fired only when the
    session crosses ``everos_consolidation_threshold_pct`` of the context
    window, as a force-flushed background task, and again by the nightly run.
  - **feedback** still fires per-turn (FB-1).

Plus the resilience contract: no backend = silent legacy mode, and a
store/feedback exception must not derail the turn.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from raven.agent.loop import AgentLoop
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest
from raven.memory_engine.backend import Memory
from raven.providers.base import LLMProvider, LLMResponse

_USER_MEMO = "MEMO_user_prefers_terse_answers"
_AGENT_SKILL_BODY = "SKILLBODY_always_verify_a_backup_with_diff"
_AGENT_SKILL_ID = "sk-verify-backup"
_AGENT_SKILL_NAME = "verify-backup"


class _StubProvider(LLMProvider):
    """Returns a fixed assistant message and records the prompt it saw."""

    def __init__(self) -> None:
        super().__init__(api_key="test")
        self.seen_messages: list[dict] = []

    async def chat(self, messages, tools=None, model=None, max_tokens=4096,
                   temperature=0.7, reasoning_effort=None, tool_choice=None):
        self.seen_messages = messages
        return LLMResponse(content="ok", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"

    def prompt_text(self) -> str:
        """Flattened text of every message the LLM was handed this turn."""
        return "\n".join(str(m.get("content")) for m in self.seen_messages)


class _FakeBackend:
    """Captures the three MemoryBackend seams and serves canned recall hits."""

    def __init__(self) -> None:
        self.recall_calls: list[dict[str, Any]] = []
        self.store_calls: list[dict[str, Any]] = []
        self.feedback_calls: list[dict[str, Any]] = []
        self.store_raises: Exception | None = None
        self.feedback_raises: Exception | None = None

    async def start(self) -> None: pass
    async def stop(self) -> None: pass

    async def recall(self, query, *, user_id=None, agent_id=None, top_k):
        self.recall_calls.append({
            "query": query, "user_id": user_id, "agent_id": agent_id, "top_k": top_k,
        })
        if user_id is not None:
            return [Memory(text=_USER_MEMO, score=1.0)]
        elif agent_id is not None:
            return [Memory(
                text=_AGENT_SKILL_BODY, score=0.9,
                metadata={"id": _AGENT_SKILL_ID, "name": _AGENT_SKILL_NAME},
            )]
        return []

    async def store(self, session_id, messages, *, force_flush=False):
        self.store_calls.append({
            "session_id": session_id, "messages": messages, "force_flush": force_flush,
        })
        if self.store_raises is not None:
            raise self.store_raises

    async def feedback(self, signals):
        self.feedback_calls.append(signals)
        if self.feedback_raises is not None:
            raise self.feedback_raises


def _make_agent(
    workspace: Path,
    *,
    backend=None,
    everos_threshold_pct: int = 80,
    context_window_tokens: int = 65_536,
    nightly_hour: int | None = 0,
) -> AgentLoop:
    return AgentLoop(
        provider=_StubProvider(),
        workspace=workspace,
        model="stub",
        max_iterations=2,
        restrict_to_workspace=True,
        backend=backend,
        everos_consolidation_threshold_pct=everos_threshold_pct,
        everos_nightly_consolidation_hour=nightly_hour,
        context_window_tokens=context_window_tokens,
    )


def _msg(content: str = "how do I back up a config file safely?") -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="mock", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
        text=content,
    )


async def _drain_everos(agent: AgentLoop) -> None:
    """Await any background EverOS consolidation tasks spawned this turn."""
    while agent._everos_tasks:
        await asyncio.gather(*list(agent._everos_tasks))


# ---------------------------------------------------------------------------
# Happy path — recall (on-miss) -> inject -> feedback; NO per-turn store
# ---------------------------------------------------------------------------


async def test_full_turn_recalls_injects_and_feeds_back(tmp_path: Path) -> None:
    backend = _FakeBackend()
    agent = _make_agent(tmp_path, backend=backend)

    out = await agent._process_message(_msg())
    assert out is not None
    await _drain_everos(agent)

    # 1) recall fired on BOTH lanes (cold workspace = native miss -> user lane
    #    falls back to EverOS; skill-router agent lane is independent).
    assert any(
        c["user_id"] is not None and c["agent_id"] is None
        for c in backend.recall_calls
    ), backend.recall_calls
    assert any(
        c["agent_id"] is not None and c["user_id"] is None
        for c in backend.recall_calls
    ), backend.recall_calls
    assert all("back up" in c["query"] for c in backend.recall_calls)

    # 2) recalled user memory + everos skill landed in the prompt the LLM saw.
    prompt = agent.provider.prompt_text()
    assert _USER_MEMO in prompt
    assert _AGENT_SKILL_BODY in prompt

    # 3) a normal sub-threshold turn does NOT consolidate to EverOS (native
    #    short-term owns the turn; EverOS is the threshold/nightly long-term path).
    assert backend.store_calls == []

    # 4) FB-1: feedback fired with the everos native id only (prefix stripped).
    assert len(backend.feedback_calls) == 1
    sig = backend.feedback_calls[0]
    assert sig["kind"] == "skill_usage"
    assert sig["session_id"] == "mock:c1"
    assert sig["injected"] == [_AGENT_SKILL_ID]


# ---------------------------------------------------------------------------
# Threshold-gated long-term consolidation (P4)
# ---------------------------------------------------------------------------


async def test_below_threshold_does_not_consolidate(tmp_path: Path) -> None:
    backend = _FakeBackend()
    agent = _make_agent(tmp_path, backend=backend)
    session = agent.sessions.get_or_create("mock:c1")
    session.record({"role": "user", "content": "hi"})
    session.record({"role": "assistant", "content": "hello"})
    # Estimated prompt well under 80% of the window.
    agent.memory_consolidator.estimate_session_prompt_tokens = lambda s: (10, "test")

    await agent._maybe_consolidate_everos(session)
    await _drain_everos(agent)
    assert backend.store_calls == []


async def test_above_threshold_consolidates_with_force_flush(tmp_path: Path) -> None:
    backend = _FakeBackend()
    agent = _make_agent(tmp_path, backend=backend, context_window_tokens=1000)
    session = agent.sessions.get_or_create("mock:c1")
    session.record({"role": "user", "content": "hi"})
    session.record({"role": "assistant", "content": "hello"})
    # Estimated prompt at/over the 80% threshold (800).
    agent.memory_consolidator.estimate_session_prompt_tokens = lambda s: (900, "test")

    await agent._maybe_consolidate_everos(session)
    await _drain_everos(agent)

    assert len(backend.store_calls) == 1
    call = backend.store_calls[0]
    assert call["session_id"] == "mock:c1"
    assert call["force_flush"] is True
    assert len(call["messages"]) == 2
    # The consolidated watermark advanced to the full message count.
    assert session.metadata["last_everos_stored"] == 2


async def test_consolidate_is_incremental_and_idempotent(tmp_path: Path) -> None:
    backend = _FakeBackend()
    agent = _make_agent(tmp_path, backend=backend)
    session = agent.sessions.get_or_create("mock:c1")
    session.record({"role": "user", "content": "first"})

    await agent._consolidate_everos(session)
    assert len(backend.store_calls) == 1
    assert session.metadata["last_everos_stored"] == 1

    # No new messages -> no second store.
    await agent._consolidate_everos(session)
    assert len(backend.store_calls) == 1

    # New message -> only the unsent tail is sent.
    session.record({"role": "assistant", "content": "second"})
    await agent._consolidate_everos(session)
    assert len(backend.store_calls) == 2
    assert [m["content"] for m in backend.store_calls[1]["messages"]] == ["second"]


# ---------------------------------------------------------------------------
# Resilience — no backend, and backend failures must not derail the turn
# ---------------------------------------------------------------------------


async def test_no_backend_turn_completes_silently(tmp_path: Path) -> None:
    agent = _make_agent(tmp_path, backend=None)
    out = await agent._process_message(_msg())
    assert out is not None  # legacy mode: pipeline runs, no backend seams


async def test_consolidation_failure_does_not_raise(tmp_path: Path) -> None:
    backend = _FakeBackend()
    backend.store_raises = RuntimeError("everos down")
    agent = _make_agent(tmp_path, backend=backend)
    session = agent.sessions.get_or_create("mock:c1")
    session.record({"role": "user", "content": "hi"})

    # Exception is swallowed; the watermark is NOT advanced so the next run retries.
    await agent._consolidate_everos(session)
    assert len(backend.store_calls) == 1
    assert session.metadata.get("last_everos_stored", 0) == 0


async def test_feedback_failure_does_not_break_turn(tmp_path: Path) -> None:
    backend = _FakeBackend()
    backend.feedback_raises = RuntimeError("telemetry sink down")
    agent = _make_agent(tmp_path, backend=backend)

    out = await agent._process_message(_msg())
    assert out is not None  # best-effort telemetry; failure isolated
    assert len(backend.feedback_calls) == 1


# ---------------------------------------------------------------------------
# Nightly offline consolidation (P5)
# ---------------------------------------------------------------------------


def test_seconds_until_local_hour(tmp_path: Path) -> None:
    from datetime import datetime

    agent = _make_agent(tmp_path)
    agent._now_fn = lambda: datetime(2026, 6, 29, 22, 0, 0)  # 22:00 local
    assert agent._seconds_until_local_hour(0) == 2 * 3600    # next 00:00
    assert agent._seconds_until_local_hour(23) == 3600       # 23:00 today
    assert agent._seconds_until_local_hour(22) == 24 * 3600  # already past -> tomorrow


async def test_nightly_consolidates_all_sessions(tmp_path: Path) -> None:
    backend = _FakeBackend()
    agent = _make_agent(tmp_path, backend=backend)
    for key, txt in [("mock:c1", "a"), ("mock:c2", "b")]:
        s = agent.sessions.get_or_create(key)
        s.record({"role": "user", "content": txt})
        agent.sessions.save(s)

    await agent._consolidate_all_sessions()

    assert len(backend.store_calls) == 2
    assert all(c["force_flush"] for c in backend.store_calls)
    assert {c["session_id"] for c in backend.store_calls} == {"mock:c1", "mock:c2"}


async def test_nightly_loop_noop_without_backend_or_hour(tmp_path: Path) -> None:
    # No backend -> returns immediately (must not hang on the infinite loop).
    await _make_agent(tmp_path, backend=None).run_nightly_everos_consolidation()
    # hour=None -> returns immediately even with a backend wired.
    backend = _FakeBackend()
    await _make_agent(
        tmp_path, backend=backend, nightly_hour=None,
    ).run_nightly_everos_consolidation()
    assert backend.store_calls == []


# ---------------------------------------------------------------------------
# Smoke — the full memory lifecycle in one flow (stub LLM, fake backend)
# ---------------------------------------------------------------------------


async def test_smoke_full_memory_lifecycle(tmp_path: Path) -> None:
    """below-threshold turn = native-only (no everos) -> cross 80% = intra-day
    force-flush -> nightly run consolidates only the new tail (incremental)."""
    backend = _FakeBackend()
    agent = _make_agent(tmp_path, backend=backend, context_window_tokens=1000)
    session = agent.sessions.get_or_create("mock:c1")
    session.record({"role": "user", "content": "day1 first"})
    agent.sessions.save(session)

    # 1) below 80% (estimate 100 < 800): no EverOS consolidation.
    agent.memory_consolidator.estimate_session_prompt_tokens = lambda s: (100, "t")
    await agent._maybe_consolidate_everos(session)
    await _drain_everos(agent)
    assert backend.store_calls == []

    # 2) cross 80% (estimate 900 >= 800): intra-day force-flush of the tail.
    agent.memory_consolidator.estimate_session_prompt_tokens = lambda s: (900, "t")
    await agent._maybe_consolidate_everos(session)
    await _drain_everos(agent)
    assert len(backend.store_calls) == 1
    assert backend.store_calls[0]["force_flush"] is True

    # 3) more conversation, then the nightly run: only the unsent tail is sent
    #    (watermark persisted across the save/reload the nightly path does).
    session.record({"role": "assistant", "content": "day1 more"})
    agent.sessions.save(session)
    await agent._consolidate_all_sessions()
    assert len(backend.store_calls) == 2
    assert [m["content"] for m in backend.store_calls[1]["messages"]] == ["day1 more"]
