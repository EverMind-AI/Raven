"""A tool call whose arguments the model could not finish must never execute.

`json_repair` closes a truncated object, so the parameters parse and look
runnable while their values have silently lost their tails -- a write_file that
drops the rest of the file, an edit_file whose old_string now matches the wrong
span. The model sees success and builds on a corrupted workspace. Refusing is
strictly better: the error is visible and recoverable.

Truncation and mangling need opposite remedies, so the loop separates them: a
truncated call retries with a doubled output budget (only more tokens can
finish it), a mangled-but-complete one resamples (more tokens cannot help).

Classification lives in ``raven.providers.tool_args`` as pure functions tested
in isolation; this file covers the loop's side effects.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from raven.providers.tool_args import ToolArgsLimits
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


class _ScriptedProvider(LLMProvider):
    """Replays a fixed list of responses and records each call's token budget."""

    def __init__(self, responses: list[LLMResponse]):
        super().__init__(api_key="test")
        self._responses = responses
        self.calls = 0
        self.max_tokens_seen: list[int] = []

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
        self.max_tokens_seen.append(max_tokens)
        index = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[index]

    def get_default_model(self) -> str:
        return "stub"


def _truncated_write() -> LLMResponse:
    """A write_file whose content was cut off by the output budget."""
    return LLMResponse(
        content=None,
        tool_calls=[
            ToolCallRequest(
                id="c1",
                name="write_file",
                arguments={"path": "a.py", "content": "def f():\n    ret"},
                arguments_truncated=True,
                arguments_repaired=True,
            )
        ],
        finish_reason="tool_calls",
    )


def _mangled_exec() -> LLMResponse:
    return LLMResponse(
        content=None,
        tool_calls=[ToolCallRequest(id="c1", name="exec", arguments={"command": "ls"}, arguments_repaired=True)],
        finish_reason="tool_calls",
    )


def _make_agent(workspace: Path, provider: LLMProvider, limits: ToolArgsLimits | None = None) -> AgentLoop:
    loop = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        max_iterations=12,
        restrict_to_workspace=True,
        tool_args=limits,
    )

    async def _noop() -> None:
        return None

    loop._start_executor = _noop
    loop._connect_mcp = _noop
    return loop


async def _collect(_ev) -> None:
    return None


def _drain() -> list:
    return []


async def _run(agent: AgentLoop) -> object:
    return await agent.run_turn(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="hi",
        ),
        _collect,
        _drain,
    )


async def test_a_truncated_write_never_reaches_the_filesystem(workspace):
    """The load-bearing assertion: no file appears even after the ladder ends."""
    provider = _ScriptedProvider([_truncated_write()])
    limits = ToolArgsLimits(truncation_max_retries=2, malformed_max_resamples=0, max_tokens_ceiling=32768)

    await _run(_make_agent(workspace, provider, limits))

    assert not (workspace / "a.py").exists()


async def test_a_truncated_call_doubles_the_output_budget_on_each_retry(workspace):
    done = LLMResponse(content="done", finish_reason="stop")
    provider = _ScriptedProvider([_truncated_write(), _truncated_write(), done])
    limits = ToolArgsLimits(truncation_max_retries=2, malformed_max_resamples=0, max_tokens_ceiling=32768)

    await _run(_make_agent(workspace, provider, limits))

    base = provider.max_tokens_seen[0]
    assert provider.max_tokens_seen[1] == base * 2
    assert provider.max_tokens_seen[2] == base * 4


async def test_the_doubling_stops_at_the_ceiling(workspace):
    provider = _ScriptedProvider([_truncated_write()])
    limits = ToolArgsLimits(truncation_max_retries=3, malformed_max_resamples=0, max_tokens_ceiling=9000)

    await _run(_make_agent(workspace, provider, limits))

    assert max(provider.max_tokens_seen) <= 9000


async def test_a_mangled_call_resamples_without_raising_the_budget(workspace):
    """More tokens cannot fix a formatting slip, so the budget must stay put."""
    done = LLMResponse(content="done", finish_reason="stop")
    provider = _ScriptedProvider([_mangled_exec(), _mangled_exec(), done])
    limits = ToolArgsLimits(truncation_max_retries=0, malformed_max_resamples=2, max_tokens_ceiling=32768)

    await _run(_make_agent(workspace, provider, limits))

    assert len(set(provider.max_tokens_seen)) == 1


async def test_a_clean_tool_call_is_executed_normally(workspace):
    """The guard must not fire on healthy calls."""
    good = LLMResponse(
        content=None,
        tool_calls=[ToolCallRequest(id="c1", name="write_file", arguments={"path": "b.py", "content": "x = 1\n"})],
        finish_reason="tool_calls",
    )
    provider = _ScriptedProvider([good, LLMResponse(content="done", finish_reason="stop")])

    await _run(_make_agent(workspace, provider))

    assert (workspace / "b.py").read_text() == "x = 1\n"
    assert len(set(provider.max_tokens_seen)) == 1
