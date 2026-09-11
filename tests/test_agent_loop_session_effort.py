"""A session's mode may move the reasoning effort of its turns' model calls.

The loop reads its per-session policy once at the turn's start; when the policy
names a reasoning effort, every model call of that turn -- the ReAct steps, the
wrap-up after an exhausted budget and the head summary a compaction pays for --
carries it as an explicit argument.
A policy that names none passes nothing, so the provider's configured default
applies exactly as before, and another session's turns are untouched.
"""

from __future__ import annotations

import pytest

from raven.agent.loop import AgentLoop, compaction
from raven.agent.loop.bundles import EngineWiring, ToolWiring, TurnPolicy
from raven.config.raven import CheckpointConfig, RuntimeConfig
from raven.config.schema import CompactionConfig
from raven.contracts.llm_provider import LLMResponse, ToolCallRequest
from raven.providers.base import LLMProvider
from raven.spine import ChatType, Origin, Source, TurnRequest


class _ScriptedProvider:
    """chat_with_retry answers from a script and records every call's kwargs."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        snapshot = dict(kwargs)
        snapshot["messages"] = [dict(m) for m in kwargs.get("messages") or []]
        self.calls.append(snapshot)
        index = min(len(self.calls) - 1, len(self._responses) - 1)
        return self._responses[index]

    def get_default_model(self) -> str:
        return "fake/model"


def _text(content: str) -> LLMResponse:
    return LLMResponse(content=content, finish_reason="stop")


def _tool_call(name: str, arguments: dict) -> LLMResponse:
    return LLMResponse(content="", tool_calls=[ToolCallRequest(id="c1", name=name, arguments=arguments)])


def _req(chat_id: str = "c") -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="cli", chat_id=chat_id, sender_id="u", chat_type=ChatType.DM),
        text="hi",
    )


def _loop(tmp_path, provider, max_iterations: int = 6) -> AgentLoop:
    loop = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model="fake/model",
        policy=TurnPolicy(max_iterations=max_iterations),
        tools=ToolWiring(restrict_to_workspace=True),
    )

    async def _noop(**_kw) -> None:
        return None

    loop._start_executor = _noop
    loop._connect_mcp = _noop
    return loop


@pytest.mark.asyncio
async def test_a_session_policys_effort_reaches_every_model_call(tmp_path):
    provider = _ScriptedProvider([_tool_call("list_dir", {"path": "."}), _text("done")])
    loop = _loop(tmp_path, provider)
    loop.set_session_policy("cli:c", mode="medium", reasoning_effort="medium")

    out = await loop._process_message(_req())

    assert out is not None
    assert len(provider.calls) == 2
    assert [call["reasoning_effort"] for call in provider.calls] == ["medium", "medium"]


@pytest.mark.asyncio
async def test_a_policy_naming_no_effort_passes_none_so_the_configured_default_stands(tmp_path):
    provider = _ScriptedProvider([_text("done")])
    loop = _loop(tmp_path, provider)
    loop.set_session_policy("cli:c", mode="high")

    await loop._process_message(_req())

    assert "reasoning_effort" not in provider.calls[0], "an explicit None would override the provider's sentinel"


@pytest.mark.asyncio
async def test_another_session_runs_on_the_defaults(tmp_path):
    provider = _ScriptedProvider([_text("done")])
    loop = _loop(tmp_path, provider)
    loop.set_session_policy("cli:c", mode="max", reasoning_effort="max")

    await loop._process_message(_req(chat_id="d"))

    assert "reasoning_effort" not in provider.calls[0]


@pytest.mark.asyncio
async def test_the_exhaustion_wrap_up_call_runs_at_the_sessions_effort(tmp_path):
    """The wrap-up after an exhausted iteration budget is a model call of the
    same turn, so it pays the turn's effort too."""
    provider = _ScriptedProvider([_tool_call("list_dir", {"path": "."})] * 3 + [_text("summary")])
    loop = _loop(tmp_path, provider, max_iterations=1)
    loop.set_session_policy("cli:c", mode="max", reasoning_effort="max")

    out = await loop._process_message(_req())

    assert out is not None
    wrap_up = provider.calls[-1]
    assert wrap_up["tools"] is None, "the wrap-up withholds the tools"
    assert wrap_up["reasoning_effort"] == "max"


class _CompactingProvider(LLMProvider):
    """A main loop that blows past the compaction line, and a summary desk that
    records the effort every head summary was asked for."""

    def __init__(self) -> None:
        super().__init__(api_key="test")
        self.main_efforts: list[str | None] = []
        self.summary_efforts: list[str | None] = []

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
        if messages and messages[0].get("content") == compaction.SUMMARY_INSTRUCTIONS:
            self.summary_efforts.append(reasoning_effort)
            return LLMResponse(content="Compacted handoff brief. " * 60, finish_reason="stop")
        self.main_efforts.append(reasoning_effort)
        if len(self.main_efforts) == 1:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="c1", name="no_such_tool", arguments={})],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 4990, "completion_tokens": 10},
            )
        return LLMResponse(content="answer after compaction", finish_reason="stop")

    def get_default_model(self) -> str:
        return "fake/model"


def _seeded_transcript(rounds: int = 6) -> list[dict]:
    messages: list[dict] = [
        {"role": "system", "content": "sys prompt"},
        {"role": "user", "content": "the task statement"},
    ]
    for i in range(rounds):
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": f"s{i}", "type": "function", "function": {"name": "no_such_tool", "arguments": "{}"}}
                ],
            }
        )
        messages.append({"role": "tool", "tool_call_id": f"s{i}", "content": f"seed evidence {i} " * 40})
    return messages


@pytest.mark.asyncio
async def test_the_compaction_summary_opts_out_of_the_sessions_effort(tmp_path):
    """The one model call of a turn that does not take the session's effort.

    It used to, on the reasoning that a head summary is a model call of the
    same turn like the ReAct steps around it. They are not alike: a step thinks
    in order to decide, a handoff brief is a transcript read back, and a
    reasoning model pays for thinking out of the same budget as the brief.
    Measured, two summary calls at the session's effort came back with an empty
    body having spent the whole budget before the brief began, and compaction
    degraded to blind elision for the rest of that run.

    Deliberately narrow: the ReAct steps still run at the session's effort, and
    so does the watch-work judgement below, which is a judgement rather than a
    transcription.
    """
    provider = _CompactingProvider()
    loop = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model="fake/model",
        policy=TurnPolicy(max_iterations=12),
        tools=ToolWiring(restrict_to_workspace=True),
        engine=EngineWiring(
            context_window_tokens=1000,
            compaction_config=CompactionConfig(enabled=True, reserved_tokens=100, preserve_recent_tokens=300),
            runtime_config=RuntimeConfig(checkpoint=CheckpointConfig(policy="never")),
        ),
    )
    loop.set_session_policy("acp:s1", mode="max", reasoning_effort="max")

    final, _used, _messages, outcome = await loop._run_agent_loop(_seeded_transcript(), session_key="acp:s1")

    assert final == "answer after compaction" and outcome.status == "completed"
    assert provider.summary_efforts == [compaction.SUMMARY_REASONING_EFFORT], (
        "a max session must not spend the summary's budget on thinking"
    )
    assert set(provider.main_efforts) == {"max"}, "only the summary opts out"


@pytest.mark.asyncio
async def test_the_compaction_summary_names_the_floor_even_with_no_policy_effort(tmp_path):
    """Unstated is not the same as little. The run this fix comes from named no
    effort at all, so the backend picked -- and what it picked spent the whole
    summary budget. The floor is stated outright rather than left open, which
    does override a provider default configured for the turn's own calls.
    """
    provider = _CompactingProvider()
    loop = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model="fake/model",
        policy=TurnPolicy(max_iterations=12),
        tools=ToolWiring(restrict_to_workspace=True),
        engine=EngineWiring(
            context_window_tokens=1000,
            compaction_config=CompactionConfig(enabled=True, reserved_tokens=100, preserve_recent_tokens=300),
            runtime_config=RuntimeConfig(checkpoint=CheckpointConfig(policy="never")),
        ),
    )
    loop.set_session_policy("acp:s1", mode="high")

    await loop._run_agent_loop(_seeded_transcript(), session_key="acp:s1")

    assert provider.summary_efforts == [compaction.SUMMARY_REASONING_EFFORT]


@pytest.mark.asyncio
async def test_the_watch_work_judgement_is_handed_the_sessions_effort(tmp_path):
    """The turn path hands the pinned effort to the watch-work helper for
    every tool result it judges, so the in-turn classifier is not the one
    model call of a max session that runs at the default."""
    provider = _ScriptedProvider([_tool_call("list_dir", {"path": "."}), _text("done")])
    loop = _loop(tmp_path, provider)
    loop.set_session_policy("cli:c", mode="max", reasoning_effort="max")
    handed: list[str | None] = []

    async def spy(state, name, args, result, message, reasoning_effort=None):
        handed.append(reasoning_effort)
        return ""

    loop._note_watch_work = spy

    out = await loop._process_message(_req())

    assert out is not None
    assert handed == ["max"], handed
