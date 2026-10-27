"""The loop's half of the reply room and of a refused prompt.

Each request carries a ``max_tokens`` sized to the room beside its prompt once
the model's whole ceiling no longer fits there; a refusal is learned from and
offered to the history strategy before the loop elides anything; every
answered request is reported to the engine so a strategy can replay it; and the
in-turn window passes leave what a strategy marked alone.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import EngineWiring, ToolWiring, TurnPolicy
from raven.agent.window import compaction, shrink
from raven.config.raven import CheckpointConfig, RuntimeConfig
from raven.context_engine import headroom
from raven.context_engine.history.markers import HISTORY_CHECKPOINT_KEY, HISTORY_PINNED_KEY
from raven.contracts.context import OverflowOutcome
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from raven.utils.tokens import estimate_prompt_tokens

_WINDOW = 16_000
_CEILING = 8_192


class _Recording(LLMProvider):
    def __init__(self, script: list[LLMResponse]):
        super().__init__(api_key="test")
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7, **kw):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools, "max_tokens": max_tokens})
        return self._script.pop(0)

    def get_default_model(self) -> str:
        return "stub"


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture(autouse=True)
def _forget_learned_headroom():
    headroom.reset()
    yield
    headroom.reset()


def _agent(
    workspace: Path, provider: LLMProvider, monkeypatch, iterations: int = 4, window: int = _WINDOW
) -> AgentLoop:
    agent = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=iterations),
        tools=ToolWiring(restrict_to_workspace=True),
        engine=EngineWiring(
            context_window_tokens=window,
            runtime_config=RuntimeConfig(checkpoint=CheckpointConfig(policy="never")),
        ),
    )
    monkeypatch.setattr(agent, "_wire_output_ceiling", lambda model=None: _CEILING)
    return agent


def _prompt(words: int) -> list[dict[str, Any]]:
    return [{"role": "system", "content": "sys"}, {"role": "user", "content": "word " * words}]


@pytest.mark.asyncio
async def test_a_short_prompt_goes_out_with_the_ceiling_it_always_had(workspace, monkeypatch):
    from raven.providers.base import send_max_tokens

    provider = _Recording([LLMResponse(content="ok", finish_reason="stop")])
    # Wide, because the default tool surface alone is several thousand tokens.
    agent = _agent(workspace, provider, monkeypatch, window=200_000)

    await agent._run_agent_loop(_prompt(10))

    assert provider.calls[0]["max_tokens"] == send_max_tokens(provider.generation, "stub", allow_fetch=False)


@pytest.mark.asyncio
async def test_a_prompt_near_the_window_carries_the_room_beside_it(workspace, monkeypatch):
    provider = _Recording([LLMResponse(content="ok", finish_reason="stop")])
    agent = _agent(workspace, provider, monkeypatch)

    await agent._run_agent_loop(_prompt(10_000))

    call = provider.calls[0]
    estimate = estimate_prompt_tokens(call["messages"], call["tools"])
    expected = max(4_096, _WINDOW - int(estimate * 1.05))
    assert expected < _CEILING, "the prompt has to be near enough the window for this to test anything"
    assert call["max_tokens"] == expected


@pytest.mark.asyncio
async def test_every_answered_request_is_reported_to_the_engine(workspace, monkeypatch):
    provider = _Recording(
        [
            LLMResponse(content="", tool_calls=[ToolCallRequest(id="c1", name="no_such_tool", arguments={})]),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )
    agent = _agent(workspace, provider, monkeypatch)
    heard: list[tuple[str, int, str, object]] = []
    monkeypatch.setattr(
        agent.context_engine,
        "note_request",
        lambda key, messages, *, model, tools=None: heard.append((key, len(messages), model, tools)),
        raising=False,
    )

    await agent._run_agent_loop(_prompt(10), session_key="s1")

    assert [(k, n) for k, n, _, _ in heard] == [
        ("s1", len(provider.calls[0]["messages"])),
        ("s1", len(provider.calls[1]["messages"])),
    ]
    assert all(model for _, _, model, _ in heard)
    assert [tools for _, _, _, tools in heard] == [provider.calls[0]["tools"], provider.calls[1]["tools"]]


def _overflow() -> LLMResponse:
    return LLMResponse(content="This model's maximum context length (8192 tokens) was exceeded", finish_reason="error")


@pytest.mark.asyncio
async def test_a_refusal_is_learned_and_the_strategys_reclaim_is_retried(workspace, monkeypatch):
    provider = _Recording([_overflow(), LLMResponse(content="answered", finish_reason="stop")])
    agent = _agent(workspace, provider, monkeypatch)
    asked: list[tuple[str, int]] = []

    async def reclaim(key, messages, *, attempt, window):
        asked.append((key, attempt))
        return OverflowOutcome(messages=[messages[0], messages[-1]], reclaimed_tokens=100)

    monkeypatch.setattr(agent.context_engine, "on_context_overflow", reclaim)
    initial = [*_prompt(10)[:1], {"role": "user", "content": "old"}, {"role": "assistant", "content": "old answer"}]
    initial.append({"role": "user", "content": "now"})

    final, _, _, _ = await agent._run_agent_loop(initial, session_key="s1")

    assert final == "answered"
    assert asked == [("s1", 1)]
    assert headroom.required_headroom("stub") > 0, "the refusal taught the reply room"
    assert [m["content"] for m in provider.calls[1]["messages"]] == ["sys", "now"]


@pytest.mark.asyncio
async def test_a_strategy_that_declines_leaves_the_refusal_to_the_loops_elision(workspace, monkeypatch):
    provider = _Recording(
        [
            *[
                LLMResponse(content="", tool_calls=[ToolCallRequest(id=f"c{i}", name="no_such_tool", arguments={})])
                for i in range(4)
            ],
            _overflow(),
            LLMResponse(content="answered", finish_reason="stop"),
        ]
    )
    agent = _agent(workspace, provider, monkeypatch, iterations=8)
    asked: list[int] = []

    async def decline(key, messages, *, attempt, window):
        asked.append(attempt)
        return None

    monkeypatch.setattr(agent.context_engine, "on_context_overflow", decline)

    final, _, _, _ = await agent._run_agent_loop(_prompt(10), session_key="s1")

    assert final == "answered"
    assert asked == [1]
    assert shrink.ELIDED_TOOL_OUTPUT in [m.get("content") for m in provider.calls[-1]["messages"]]


def test_the_head_summary_never_takes_what_a_strategy_marked():
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "q"},
        {"role": "user", "content": "the checkpoint", HISTORY_CHECKPOINT_KEY: True},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "p"}], HISTORY_PINNED_KEY: True},
        {"role": "tool", "tool_call_id": "p", "content": "pinned body", HISTORY_PINNED_KEY: True},
        {"role": "assistant", "content": "later"},
    ]
    assert compaction.protected_prefix_end(messages) == 5
    unmarked = [{k: v for k, v in m.items() if not k.startswith("_history")} for m in messages]
    assert compaction.protected_prefix_end(unmarked) == 2


def test_the_overflow_elision_keeps_a_pinned_result():
    messages: list[dict[str, Any]] = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    messages.append({"role": "tool", "tool_call_id": "p", "content": "pinned body", HISTORY_PINNED_KEY: True})
    for i in range(4):
        messages.append({"role": "tool", "tool_call_id": f"t{i}", "content": f"body {i}"})

    shrunk, elided = shrink.emergency_shrink(messages)

    assert elided == 1
    assert shrunk[2]["content"] == "pinned body"
    assert shrunk[3]["content"] == shrink.ELIDED_TOOL_OUTPUT


def test_the_overflow_elision_keeps_a_record_that_carries_a_ref():
    messages: list[dict[str, Any]] = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    messages.append({"role": "tool", "tool_call_id": "r", "content": "[tool:exec 40,000 chars] [[ref session/x@0#3]]"})
    for i in range(4):
        messages.append({"role": "tool", "tool_call_id": f"t{i}", "content": f"body {i}"})

    shrunk, elided = shrink.emergency_shrink(messages)

    assert elided == 1
    assert "[[ref session/x@0#3]]" in shrunk[2]["content"]


@pytest.mark.asyncio
async def test_a_failing_engine_hook_costs_its_say_not_the_turn(workspace, monkeypatch):
    provider = _Recording(
        [
            *[
                LLMResponse(content="", tool_calls=[ToolCallRequest(id=f"c{i}", name="no_such_tool", arguments={})])
                for i in range(4)
            ],
            _overflow(),
            LLMResponse(content="answered", finish_reason="stop"),
        ]
    )
    agent = _agent(workspace, provider, monkeypatch, iterations=8)

    async def broken(*a, **k):
        raise RuntimeError("the strategy broke")

    def broken_note(*a, **k):
        raise RuntimeError("the strategy broke")

    monkeypatch.setattr(agent.context_engine, "on_context_overflow", broken)
    monkeypatch.setattr(agent.context_engine, "note_request", broken_note, raising=False)

    final, _, _, _ = await agent._run_agent_loop(_prompt(10), session_key="s1")

    assert final == "answered"


@pytest.mark.asyncio
async def test_a_streamed_overflow_reaches_the_recovery_instead_of_ending_the_turn(workspace, monkeypatch):
    from raven.providers.streaming import stream_llm_call

    class _Streaming(_Recording):
        async def chat_stream(self, messages, tools=None, model=None, **kw):
            self.calls.append(
                {"messages": [dict(m) for m in messages], "tools": tools, "max_tokens": kw.get("max_tokens")}
            )
            step = self._script.pop(0)
            if isinstance(step, Exception):
                raise step
            yield {"type": "content", "delta": step.content}

    provider = _Streaming([RuntimeError("This model's maximum context length (8192 tokens) was exceeded")])
    response = await stream_llm_call(provider, messages=[{"role": "user", "content": "x"}], tools=None, model="stub")
    assert isinstance(response, LLMResponse)
    assert response.finish_reason == "error"
    assert response.error_classification is not None and response.error_classification.should_compress


def test_a_long_retrieved_body_that_opens_on_a_ref_is_still_given_up():
    messages: list[dict[str, Any]] = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    retrieved = "[[ref session/x@0#3]] role=tool name=read_file chars=40,000\n" + "body " * 2000
    messages.append({"role": "tool", "tool_call_id": "r", "content": retrieved})
    for i in range(3):
        messages.append({"role": "tool", "tool_call_id": f"t{i}", "content": f"body {i}"})

    shrunk, elided = shrink.emergency_shrink(messages)

    assert elided == 1
    assert shrunk[2]["content"] == shrink.ELIDED_TOOL_OUTPUT


@pytest.mark.asyncio
async def test_the_overflow_tells_the_lesson_whether_the_refused_request_was_narrowed(workspace, monkeypatch):
    from raven.contracts.harness import WindowPressure, WindowState

    agent = _agent(workspace, _Recording([]), monkeypatch)
    memory = agent.harness.memory
    monkeypatch.setattr(memory, "_output_ceiling", lambda model=None: _CEILING)
    # The prompt alone decides it here: the default tool surface is several
    # thousand tokens, which on this window would narrow every request.
    monkeypatch.setattr(memory, "_tool_definitions", lambda: [])
    lessons: list[bool] = []
    real = headroom.learn_from_overflow

    def spy(*a, narrowed=False, **k):
        lessons.append(narrowed)
        return real(*a, narrowed=narrowed, **k)

    monkeypatch.setattr(headroom, "learn_from_overflow", spy)
    for words in (10, 10_000):
        await memory.shrink(
            _prompt(words), pressure=WindowPressure.OVERFLOW, state=WindowState(image_window=2), model="stub"
        )

    assert lessons == [False, True]


@pytest.mark.asyncio
async def test_the_wrap_up_on_an_exhausted_turn_is_sized_like_any_call(workspace, monkeypatch):
    provider = _Recording([LLMResponse(content="wrapped up", finish_reason="stop")])
    agent = _agent(workspace, provider, monkeypatch)

    await agent._synthesize_final_on_exhaustion(_prompt(10_000), "stub", None, fallback="static")

    call = provider.calls[0]
    assert call["max_tokens"] < _CEILING, "near the window the wrap-up asks only for the room beside it"


def test_the_after_turn_pipeline_reads_the_envelope_without_its_recall():
    from raven.agent.loop.turn_path import _without_scratch
    from raven.context_engine.scratch import TURN_SCRATCH_KEY

    scratch = "[Turn Scratch — recalled for this turn only, not conversation history]\n\n# Memory\n\n- a recalled fact"
    envelope = {"role": "user", "content": f"the question\n\n{scratch}", TURN_SCRATCH_KEY: scratch}

    out = _without_scratch([envelope, {"role": "assistant", "content": "answer"}])

    assert out[0]["content"] == "the question"
    assert TURN_SCRATCH_KEY not in out[0]
    assert TURN_SCRATCH_KEY in envelope, "the record itself is left alone"


@pytest.mark.asyncio
async def test_a_reclaim_that_drops_the_turns_question_is_refused(workspace, monkeypatch):
    """Whatever an engine hands back, a retry that no longer carries the turn's
    own question would answer something else; the loop's elision runs instead."""
    provider = _Recording(
        [
            *[
                LLMResponse(content="", tool_calls=[ToolCallRequest(id=f"c{i}", name="no_such_tool", arguments={})])
                for i in range(4)
            ],
            _overflow(),
            LLMResponse(content="answered", finish_reason="stop"),
        ]
    )
    agent = _agent(workspace, provider, monkeypatch, iterations=8)

    async def drops_the_question(key, messages, *, attempt, window):
        return OverflowOutcome(messages=[m for m in messages if m.get("role") != "user"], reclaimed_tokens=50)

    monkeypatch.setattr(agent.context_engine, "on_context_overflow", drops_the_question)
    initial = [{"role": "system", "content": "sys"}, {"role": "user", "content": "the turn's question"}]

    final, _, _, _ = await agent._run_agent_loop(initial, session_key="s1")

    assert final == "answered"
    retry = provider.calls[-1]["messages"]
    assert any(m.get("content") == "the turn's question" for m in retry)
    assert shrink.ELIDED_TOOL_OUTPUT in [m.get("content") for m in retry]
