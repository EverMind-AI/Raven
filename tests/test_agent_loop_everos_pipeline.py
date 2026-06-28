"""Deterministic full-turn coverage of the channel -> memory -> EverOS pipeline.

Drives the **real** ``AgentLoop._process_message`` with a stub LLM provider
and a fake :class:`MemoryBackend`, pinning the integration seam that only the
``real_llm``-gated e2e (``tests/integration/test_everos_channel_e2e.py``)
otherwise exercises — but here without an LLM, so it runs in normal CI.

One channel turn must, in order:
  1. recall on BOTH lanes during context assembly
     (``user_id`` for the # Memory segment, ``agent_id`` for EverosSkillSource);
  2. inject the recalled user memory + everos skill into the prompt the LLM sees;
  3. after the turn, ``backend.store(session_key, turn_slice)`` (AG-1);
  4. after the turn, ``backend.feedback`` with the injected everos native ids only (FB-1).

Plus the resilience contract: no backend = silent legacy mode, and a
store/feedback exception must not derail the turn (the turn is already saved).
"""

from __future__ import annotations

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

    async def store(self, session_id, messages):
        self.store_calls.append({"session_id": session_id, "messages": messages})
        if self.store_raises is not None:
            raise self.store_raises

    async def feedback(self, signals):
        self.feedback_calls.append(signals)
        if self.feedback_raises is not None:
            raise self.feedback_raises


def _make_agent(workspace: Path, *, backend=None) -> AgentLoop:
    return AgentLoop(
        provider=_StubProvider(),
        workspace=workspace,
        model="stub",
        max_iterations=2,
        restrict_to_workspace=True,
        backend=backend,
    )


def _msg(content: str = "how do I back up a config file safely?") -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="mock", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
        text=content,
    )


# ---------------------------------------------------------------------------
# Happy path — one turn exercises recall -> inject -> store -> feedback
# ---------------------------------------------------------------------------


async def test_full_turn_recalls_injects_stores_and_feeds_back(tmp_path: Path) -> None:
    backend = _FakeBackend()
    agent = _make_agent(tmp_path, backend=backend)

    out = await agent._process_message(_msg())
    assert out is not None

    # 1) recall fired on BOTH lanes, both carrying the user's query.
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

    # 3) AG-1: the turn slice was forwarded to the backend exactly once.
    assert len(backend.store_calls) == 1
    call = backend.store_calls[0]
    assert call["session_id"] == "mock:c1"
    roles = [m.get("role") for m in call["messages"]]
    assert "user" in roles and "assistant" in roles

    # 4) FB-1: feedback fired with the everos native id only (prefix stripped).
    assert len(backend.feedback_calls) == 1
    sig = backend.feedback_calls[0]
    assert sig["kind"] == "skill_usage"
    assert sig["session_id"] == "mock:c1"
    assert sig["injected"] == [_AGENT_SKILL_ID]


# ---------------------------------------------------------------------------
# Resilience — no backend, and backend failures must not derail the turn
# ---------------------------------------------------------------------------


async def test_no_backend_turn_completes_silently(tmp_path: Path) -> None:
    agent = _make_agent(tmp_path, backend=None)
    out = await agent._process_message(_msg())
    assert out is not None  # legacy mode: pipeline runs, no backend seams


async def test_store_failure_does_not_break_turn(tmp_path: Path) -> None:
    backend = _FakeBackend()
    backend.store_raises = RuntimeError("everos down")
    agent = _make_agent(tmp_path, backend=backend)

    out = await agent._process_message(_msg())
    assert out is not None  # exception swallowed; turn already saved
    assert len(backend.store_calls) == 1  # store was attempted


async def test_feedback_failure_does_not_break_turn(tmp_path: Path) -> None:
    backend = _FakeBackend()
    backend.feedback_raises = RuntimeError("telemetry sink down")
    agent = _make_agent(tmp_path, backend=backend)

    out = await agent._process_message(_msg())
    assert out is not None  # best-effort telemetry; failure isolated
    assert len(backend.feedback_calls) == 1
