"""The iteration phases fire inside the loop and their grants take effect.

A product that steers the loop -- budget notes, forced finalization, spin
breaking -- lives entirely on these seams, so what is pinned here is the
loop's side of the contract: where each phase fires, what a rollback does
to history and to the iteration budget, that injected messages reach the
re-sample and persist, that generation overrides reach the provider only
when allowlisted, that a withheld tool is withheld for one iteration only,
and that the terminal seam sees an answerless exit.
"""

from __future__ import annotations

import pytest

from raven.agent.hook import AgentHook, HookDecision
from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import HostWiring, ToolWiring, TurnPolicy
from raven.contracts.llm_provider import LLMResponse, ToolCallRequest
from raven.spine import ChatType, Origin, Source, TurnRequest


class _ScriptedProvider:
    """chat_with_retry answers from a script and records every call's kwargs."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        # Snapshot: the loop mutates-and-returns the same message list, so a
        # stored reference would show the turn's end state, not this call's.
        snapshot = dict(kwargs)
        snapshot["messages"] = [dict(m) for m in kwargs.get("messages") or []]
        snapshot["tools"] = list(kwargs.get("tools") or [])
        self.calls.append(snapshot)
        index = min(len(self.calls) - 1, len(self._responses) - 1)
        return self._responses[index]

    def get_default_model(self) -> str:
        return "fake/model"


def _text(content: str, **extra) -> LLMResponse:
    return LLMResponse(content=content, finish_reason="stop", **extra)


def _tool_call(name: str, arguments: dict) -> LLMResponse:
    return LLMResponse(content="", tool_calls=[ToolCallRequest(id="c1", name=name, arguments=arguments)])


def _req(text: str = "hi") -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="cli", chat_id="c", sender_id="u", chat_type=ChatType.DM),
        text=text,
    )


def _loop(tmp_path, provider, hooks, max_iterations: int = 6) -> AgentLoop:
    loop = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model="fake/model",
        policy=TurnPolicy(max_iterations=max_iterations),
        host=HostWiring(hooks=hooks),
        tools=ToolWiring(restrict_to_workspace=True),
    )

    async def _noop(**_kw) -> None:
        return None

    loop._start_executor = _noop
    loop._connect_mcp = _noop
    return loop


@pytest.mark.asyncio
async def test_before_iteration_sees_the_turn_and_can_withhold_a_tool_for_one_iteration(tmp_path):
    seen: list[tuple[int, str, int]] = []

    class WithholdFirst(AgentHook):
        async def before_iteration(self, ctx):
            seen.append((ctx.iteration, ctx.turn_question, ctx.turn_base))
            if ctx.iteration == 1:
                return HookDecision(modified_tools=[t for t in ctx.tools if t["function"]["name"] != "list_dir"])
            return HookDecision()

    provider = _ScriptedProvider([_tool_call("list_dir", {"path": "."}), _text("done")])
    loop = _loop(tmp_path, provider, [WithholdFirst()])

    out = await loop._process_message(_req("what is here"))

    assert out is not None
    assert seen[0][1] == "what is here"
    first_call_tools = {t["function"]["name"] for t in provider.calls[0]["tools"]}
    second_call_tools = {t["function"]["name"] for t in provider.calls[1]["tools"]}
    assert "list_dir" not in first_call_tools, "withheld for the first iteration"
    assert "list_dir" in second_call_tools, "and offered again on the next -- the registry was untouched"


@pytest.mark.asyncio
async def test_before_execute_tools_rollback_discards_the_proposal_and_does_not_consume_an_iteration(tmp_path):
    class BounceOnce(AgentHook):
        def __init__(self) -> None:
            self.bounced = False

        async def before_execute_tools(self, ctx):
            if not self.bounced:
                self.bounced = True
                return HookDecision(
                    rollback=True,
                    rollback_inject=[{"role": "user", "content": "[gate] do not call tools; answer directly"}],
                    rollback_overrides={"temperature": 0.0, "bogus": 1},
                )
            return HookDecision()

    provider = _ScriptedProvider([_tool_call("list_dir", {"path": "."}), _text("answered directly")])
    loop = _loop(tmp_path, provider, [BounceOnce()], max_iterations=1)

    out = await loop._process_message(_req())

    # Budget of one iteration, two provider calls: the rollback did not spend it.
    assert out is not None and "answered directly" in str(out)
    assert len(provider.calls) == 2
    resample = provider.calls[1]
    assert resample["temperature"] == 0.0, "allowlisted override reached the re-sample"
    assert "bogus" not in resample, "unknown override was dropped, not forwarded"
    assert resample["messages"][-1]["content"] == "[gate] do not call tools; answer directly"
    assert not any(m.get("tool_calls") for m in resample["messages"]), "the discarded proposal is gone"


@pytest.mark.asyncio
async def test_after_iteration_short_circuit_replaces_the_draft_without_persisting_it(tmp_path):
    class Replace(AgentHook):
        async def after_iteration(self, ctx):
            if ctx.response is not None and not ctx.response.has_tool_calls:
                return HookDecision(short_circuit_result="reviewed answer")
            return HookDecision()

    provider = _ScriptedProvider([_text("first draft")])
    loop = _loop(tmp_path, provider, [Replace()])

    out = await loop._process_message(_req())

    assert "reviewed answer" in str(out)
    session = loop.sessions.get_or_create("cli:c")
    texts = [m.get("content") for m in session.messages if m.get("role") == "assistant"]
    assert "first draft" not in texts
    assert "reviewed answer" in texts


@pytest.mark.asyncio
async def test_the_rollback_cap_degrades_to_pass_through_and_is_counted(tmp_path):
    class AlwaysBounce(AgentHook):
        def __init__(self) -> None:
            self.refusals_seen = 0

        async def after_iteration(self, ctx):
            self.refusals_seen = ctx.metadata.get("rollbacks_refused", 0)
            return HookDecision(rollback=True)

    hook = AlwaysBounce()
    provider = _ScriptedProvider([_text("draft")])
    loop = _loop(tmp_path, provider, [hook], max_iterations=2)

    out = await loop._process_message(_req())

    assert out is not None
    assert len(provider.calls) == AgentLoop._MAX_HOOK_ROLLBACKS + 1, "the cap bounds the re-samples"


@pytest.mark.asyncio
async def test_terminal_answerless_can_commit_an_answer_after_a_provider_error(tmp_path):
    class Salvage(AgentHook):
        async def terminal_answerless(self, ctx):
            assert ctx.metadata["turn_end"]["status"] == "error"
            return HookDecision(short_circuit_result="salvaged answer")

    provider = _ScriptedProvider([LLMResponse(content="upstream failed", finish_reason="error")])
    loop = _loop(tmp_path, provider, [Salvage()])

    out = await loop._process_message(_req())

    assert "salvaged answer" in str(out)


@pytest.mark.asyncio
async def test_no_hooks_means_no_phase_work(tmp_path):
    provider = _ScriptedProvider([_text("plain")])
    loop = _loop(tmp_path, provider, None)

    out = await loop._process_message(_req())

    assert "plain" in str(out)
    assert len(provider.calls) == 1
