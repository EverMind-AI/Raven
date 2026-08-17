"""Tests for how a multi-call turn's usage is accounted.

Two calibers, deliberately kept apart:

- ``usage_sink`` and ``TurnOutcome.usage`` carry the turn's **last** LLM call.
  That is what they have always carried and what published numbers came from, so
  they keep that meaning; redefining them in place would put "last call" and
  "turn total" in one column when old and new runs are read together.
- ``TurnOutcome.usage_totals`` carries the **sum** over the turn's calls, summed
  at the provider boundary, with the call count beside it.

The defect these pin: the loop only ever exposed the last-call caliber, so any
per-turn token or cost figure under-reported by roughly the turn's call count.
Same root cause as the ``rb_segment_cost`` defect found independently on the
recovery line; same fix shape, so the two instruments stay readable together.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest

# A model litellm prices locally, so cost_usd is non-zero and its accumulation
# is actually observable (conftest stubs the OpenRouter fetch to {}).
PRICED_MODEL = "gpt-4o-mini"


class ScriptedToolProvider(LLMProvider):
    """Replays a script of (content, tool_calls, usage) so one turn spans
    several LLM calls: tool-calling responses, then a plain reply."""

    def __init__(self, model: str, script: list[tuple[str | None, list[ToolCallRequest], dict | None]]):
        super().__init__(api_key="test")
        self._model = model
        self._script = script
        self.calls = 0

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
        content, tool_calls, usage = self._script[min(self.calls, len(self._script) - 1)]
        self.calls += 1
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason="tool_calls" if tool_calls else "stop",
            usage=usage,
        )

    def get_default_model(self) -> str:
        return self._model


def _usage(prompt: int, completion: int) -> dict:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def _read_file_call(call_id: str, path: str) -> list[ToolCallRequest]:
    return [ToolCallRequest(id=call_id, name="read_file", arguments={"path": path})]


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "a.txt").write_text("alpha\n", encoding="utf-8")
        yield ws


def _req() -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
        text="hi",
    )


def _agent(workspace: Path, provider: LLMProvider, model: str = "stub") -> AgentLoop:
    return AgentLoop(
        provider=provider,
        workspace=workspace,
        model=model,
        max_iterations=5,
        context_window_tokens=40000,
        restrict_to_workspace=True,
    )


async def _run_turn(agent: AgentLoop):
    async def _emit(_event):
        return None

    def _drain():
        return []

    return await agent.run_turn(_req(), _emit, _drain, stream=False)


# --- the two calibers ---


@pytest.mark.asyncio
async def test_totals_sum_over_every_call_while_usage_stays_last_call(workspace):
    provider = ScriptedToolProvider(
        "stub",
        [
            (None, _read_file_call("c1", "a.txt"), _usage(1000, 100)),
            (None, _read_file_call("c2", "a.txt"), _usage(2000, 200)),
            ("done", [], _usage(3000, 300)),
        ],
    )

    outcome = await _run_turn(_agent(workspace, provider))

    assert provider.calls == 3
    # The sum.
    assert outcome.usage_totals.prompt_tokens == 6000
    assert outcome.usage_totals.completion_tokens == 600
    assert outcome.usage_totals.total_tokens == 6600
    assert outcome.usage_totals.calls == 3
    # Unchanged meaning: the last call only.
    assert outcome.usage.prompt_tokens == 3000
    assert outcome.usage.completion_tokens == 300
    assert outcome.usage.total_tokens == 3300


@pytest.mark.asyncio
async def test_the_two_calibers_agree_on_a_single_call_turn(workspace):
    """With one call there is nothing to sum, so both calibers coincide -- which
    is why a single-call sample cannot reveal this defect."""
    provider = ScriptedToolProvider("stub", [("done", [], _usage(1000, 100))])

    outcome = await _run_turn(_agent(workspace, provider))

    assert provider.calls == 1
    assert outcome.usage.prompt_tokens == outcome.usage_totals.prompt_tokens == 1000
    assert outcome.usage_totals.calls == 1


@pytest.mark.asyncio
async def test_context_gauge_stays_point_in_time(workspace):
    """The gauge must not accumulate: a summed context_used reads past 100% on
    any multi-call turn."""
    provider = ScriptedToolProvider(
        "stub",
        [
            (None, _read_file_call("c1", "a.txt"), _usage(1000, 100)),
            ("done", [], _usage(3000, 300)),
        ],
    )
    sink: dict = {}

    await _agent(workspace, provider)._process_message(_req(), session_key="s1", usage_sink=sink)

    assert sink["context_used"] == 3300
    assert sink["context_max"] == 40000
    assert sink["context_percent"] == 8


@pytest.mark.asyncio
async def test_cost_totals_accumulate(workspace):
    """Compared against a one-call turn on the same priced model rather than a
    hardcoded amount, so a price-table update cannot break it."""
    per_call = _usage(1000, 100)

    two = ScriptedToolProvider(
        PRICED_MODEL,
        [(None, _read_file_call("c1", "a.txt"), per_call), ("done", [], per_call)],
    )
    one = ScriptedToolProvider(PRICED_MODEL, [("done", [], per_call)])

    two_out = await _run_turn(_agent(workspace, two, PRICED_MODEL))
    one_out = await _run_turn(_agent(workspace, one, PRICED_MODEL))

    assert one_out.usage_totals.cost_usd > 0, f"{PRICED_MODEL} must be priced for this test to mean anything"
    assert two_out.usage_totals.cost_usd == pytest.approx(2 * one_out.usage_totals.cost_usd)
    # The last-call field keeps its old meaning: one call's cost, not two.
    assert two_out.usage.total_tokens == one_out.usage.total_tokens


@pytest.mark.asyncio
async def test_a_call_with_no_usage_is_counted_not_passed_over(workspace):
    """A provider that reports no usage must not look like a cheap turn."""
    provider = ScriptedToolProvider(
        "stub",
        [
            (None, _read_file_call("c1", "a.txt"), None),
            ("done", [], _usage(1000, 100)),
        ],
    )

    outcome = await _run_turn(_agent(workspace, provider))

    assert outcome.usage_totals.calls == 2
    assert outcome.usage_totals.calls_without_usage == 1
    assert outcome.usage_totals.prompt_tokens == 1000


@pytest.mark.asyncio
async def test_zero_calls_reads_as_not_measured(workspace):
    """A default UsageTotals reports zero calls, which a reader must be able to
    tell apart from a turn that genuinely spent nothing."""
    from raven.spine import UsageTotals

    assert UsageTotals().calls == 0
    assert UsageTotals().total_tokens == 0


@pytest.mark.asyncio
async def test_sink_keys_stay_within_the_wire_contract(workspace):
    """message.complete.payload.usage is validated against tui_rpc's
    UsageSnapshot (extra="forbid"), and TS clients validate with
    additionalProperties: false. The totals deliberately do NOT go in the sink --
    that is why they are on TurnOutcome instead."""
    from raven.tui_rpc.models import UsageSnapshot

    provider = ScriptedToolProvider(
        "stub",
        [(None, _read_file_call("c1", "a.txt"), _usage(1000, 100)), ("done", [], _usage(5, 5))],
    )
    sink: dict = {}

    await _agent(workspace, provider)._process_message(_req(), session_key="s1", usage_sink=sink)

    UsageSnapshot.model_validate(sink)
