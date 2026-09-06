"""Iteration-phase hook dispatch inside the agent loop.

Pins the wiring of the three ReAct-iteration phases (``before_iteration`` /
``before_execute_tools`` / ``after_iteration``) into ``_run_agent_loop``:

- firing order and per-phase context fields (iteration / messages / tools /
  response);
- one ``AgentHookContext`` per turn — ``ctx.metadata`` carries observer state
  across iterations;
- short-circuit semantics: the value becomes the turn's reply and is persisted
  to history; for ``before_execute_tools`` the blocked tool calls never execute
  and the tool-call assistant message never persists (dangling tool_calls are
  rejected by strict providers);
- a raising hook is isolated and the turn still completes;
- rollback semantics: pop the iteration's messages, re-sample without
  consuming an iteration, apply allowlisted generation overrides to the
  re-sample call only, and degrade to pass-through past the per-turn cap.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from raven.agent.hook import AgentHook, CompositeHook, HookDecision
from raven.agent.loop import AgentLoop
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


class _ToolThenFinalProvider(LLMProvider):
    """One tool-call iteration, then a plain final answer."""

    def __init__(self):
        super().__init__(api_key="test")
        self.chat_calls = 0

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
        self.chat_calls += 1
        if self.chat_calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="c1", name="no_such_tool", arguments={})],
                finish_reason="tool_calls",
            )
        return LLMResponse(content="final answer", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


class _FinalOnlyProvider(LLMProvider):
    def __init__(self):
        super().__init__(api_key="test")
        self.chat_calls = 0

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
        self.chat_calls += 1
        return LLMResponse(content="raw answer", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


class _RecorderHook(AgentHook):
    def __init__(self):
        self.calls: list[tuple] = []

    async def before_iteration(self, ctx):
        ctx.metadata["seen"] = ctx.metadata.get("seen", 0) + 1
        self.calls.append(("before_iteration", ctx.iteration, ctx.response is None, ctx.tools is not None))
        return HookDecision()

    async def before_execute_tools(self, ctx):
        self.calls.append(("before_execute_tools", ctx.iteration, ctx.response is not None))
        return HookDecision()

    async def after_iteration(self, ctx):
        self.calls.append(("after_iteration", ctx.iteration, ctx.response is not None, ctx.metadata.get("seen")))
        return HookDecision()


class _ShortCircuitHook(AgentHook):
    def __init__(self, phase: str, reply: str):
        self._phase = phase
        self._reply = reply

    async def before_iteration(self, ctx):
        if self._phase == "before_iteration":
            return HookDecision(short_circuit_result=self._reply)
        return HookDecision()

    async def before_execute_tools(self, ctx):
        if self._phase == "before_execute_tools":
            return HookDecision(short_circuit_result=self._reply)
        return HookDecision()

    async def after_iteration(self, ctx):
        if self._phase == "after_iteration":
            return HookDecision(short_circuit_result=self._reply)
        return HookDecision()


class _SpinThenGoodProvider(LLMProvider):
    """Degenerate first response, good second; records call temperatures."""

    def __init__(self):
        super().__init__(api_key="test")
        self.temps: list = []

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
        self.temps.append(temperature)
        if len(self.temps) == 1:
            return LLMResponse(content="spin spin spin", finish_reason="stop")
        return LLMResponse(content="good answer", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


class _ConstantProvider(LLMProvider):
    def __init__(self):
        super().__init__(api_key="test")
        self.chat_calls = 0

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
        self.chat_calls += 1
        return LLMResponse(content="same answer", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


class _RollbackOnceHook(AgentHook):
    """Requests a single rollback from the given phase, then passes through."""

    def __init__(self, phase: str, overrides: dict | None = None):
        self._phase = phase
        self._overrides = overrides

    def _decide(self, phase, ctx):
        if phase == self._phase and not ctx.metadata.get("rolled"):
            ctx.metadata["rolled"] = True
            return HookDecision(rollback=True, rollback_overrides=self._overrides)
        return HookDecision()

    async def before_execute_tools(self, ctx):
        return self._decide("before_execute_tools", ctx)

    async def after_iteration(self, ctx):
        return self._decide("after_iteration", ctx)


class _AlwaysRollbackHook(AgentHook):
    async def after_iteration(self, ctx):
        return HookDecision(rollback=True)


class _RaisingHook(AgentHook):
    async def before_iteration(self, ctx):
        raise RuntimeError("boom")

    async def before_execute_tools(self, ctx):
        raise RuntimeError("boom")

    async def after_iteration(self, ctx):
        raise RuntimeError("boom")


def _make_agent(provider, workspace, hook, max_iterations=4):
    return AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        max_iterations=max_iterations,
        restrict_to_workspace=True,
        hooks=CompositeHook([hook]),
    )


@pytest.mark.asyncio
async def test_phases_fire_per_iteration_with_shared_ctx(workspace):
    recorder = _RecorderHook()
    agent = _make_agent(_ToolThenFinalProvider(), workspace, recorder)

    final, _, _, outcome = await agent._run_agent_loop([{"role": "user", "content": "go"}])

    assert final == "final answer"
    assert outcome.status == "completed"
    assert recorder.calls == [
        ("before_iteration", 1, True, True),
        ("before_execute_tools", 1, True),
        ("after_iteration", 1, True, 1),
        ("before_iteration", 2, True, True),
        ("after_iteration", 2, True, 2),
    ]


@pytest.mark.asyncio
async def test_before_iteration_short_circuit_skips_llm(workspace):
    provider = _FinalOnlyProvider()
    agent = _make_agent(provider, workspace, _ShortCircuitHook("before_iteration", "halted by gate"))

    final, _, messages, outcome = await agent._run_agent_loop([{"role": "user", "content": "go"}])

    assert final == "halted by gate"
    assert provider.chat_calls == 0
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == "halted by gate"
    assert outcome.status == "completed"


@pytest.mark.asyncio
async def test_before_execute_tools_short_circuit_blocks_execution(workspace):
    provider = _ToolThenFinalProvider()
    agent = _make_agent(provider, workspace, _ShortCircuitHook("before_execute_tools", "blocked by audit"))
    executed: list[str] = []
    orig_execute = agent.tools.execute

    async def spy(name, arguments):
        executed.append(name)
        return await orig_execute(name, arguments)

    agent.tools.execute = spy

    final, _, messages, _ = await agent._run_agent_loop([{"role": "user", "content": "go"}])

    assert final == "blocked by audit"
    assert executed == []
    assert not any(m.get("tool_calls") for m in messages)
    assert not any(m.get("role") == "tool" for m in messages)
    assert messages[-1]["content"] == "blocked by audit"


@pytest.mark.asyncio
async def test_after_iteration_short_circuit_replaces_final(workspace):
    agent = _make_agent(_FinalOnlyProvider(), workspace, _ShortCircuitHook("after_iteration", "replaced answer"))

    final, _, messages, _ = await agent._run_agent_loop([{"role": "user", "content": "go"}])

    assert final == "replaced answer"
    assert messages[-1]["content"] == "replaced answer"
    assert not any(m.get("content") == "raw answer" for m in messages)


@pytest.mark.asyncio
async def test_raising_hook_is_isolated(workspace):
    agent = _make_agent(_ToolThenFinalProvider(), workspace, _RaisingHook())

    final, _, _, outcome = await agent._run_agent_loop([{"role": "user", "content": "go"}])

    assert final == "final answer"
    assert outcome.status == "completed"


@pytest.mark.asyncio
async def test_after_iteration_rollback_resamples_final_with_overrides(workspace):
    provider = _SpinThenGoodProvider()
    hook = _RollbackOnceHook("after_iteration", overrides={"temperature": 1.0, "bogus_key": 1})
    # max_iterations=1: completing in two LLM calls proves the rolled-back
    # iteration was not billed against the budget.
    agent = _make_agent(provider, workspace, hook, max_iterations=1)

    final, _, messages, outcome = await agent._run_agent_loop([{"role": "user", "content": "go"}])

    assert final == "good answer"
    assert outcome.status == "completed"
    assert len(provider.temps) == 2
    assert provider.temps[1] == 1.0
    assert not any("spin" in str(m.get("content")) for m in messages)


@pytest.mark.asyncio
async def test_before_execute_tools_rollback_skips_execution(workspace):
    provider = _ToolThenFinalProvider()
    agent = _make_agent(provider, workspace, _RollbackOnceHook("before_execute_tools"), max_iterations=1)
    executed: list[str] = []
    orig_execute = agent.tools.execute

    async def spy(name, arguments):
        executed.append(name)
        return await orig_execute(name, arguments)

    agent.tools.execute = spy

    final, _, messages, _ = await agent._run_agent_loop([{"role": "user", "content": "go"}])

    assert final == "final answer"
    assert executed == []
    assert not any(m.get("tool_calls") for m in messages)
    assert not any(m.get("role") == "tool" for m in messages)


@pytest.mark.asyncio
async def test_after_iteration_rollback_pops_executed_tool_messages(workspace):
    provider = _ToolThenFinalProvider()
    agent = _make_agent(provider, workspace, _RollbackOnceHook("after_iteration"), max_iterations=1)
    executed: list[str] = []
    orig_execute = agent.tools.execute

    async def spy(name, arguments):
        executed.append(name)
        return await orig_execute(name, arguments)

    agent.tools.execute = spy

    final, _, messages, _ = await agent._run_agent_loop([{"role": "user", "content": "go"}])

    assert final == "final answer"
    assert executed == ["no_such_tool"]
    assert not any(m.get("role") == "tool" for m in messages)
    assert not any(m.get("tool_calls") for m in messages)


@pytest.mark.asyncio
async def test_rollback_cap_bounds_resampling(workspace):
    provider = _ConstantProvider()
    agent = _make_agent(provider, workspace, _AlwaysRollbackHook(), max_iterations=3)

    final, _, _, outcome = await agent._run_agent_loop([{"role": "user", "content": "go"}])

    assert final == "same answer"
    assert outcome.status == "completed"
    assert provider.chat_calls == AgentLoop._MAX_HOOK_ROLLBACKS + 1


def test_the_cap_clears_the_sum_of_the_installed_observers_budgets():
    """The cap is a backstop against a misbehaving observer, never a budget.

    Every gate books its bounce before the loop can refuse it, so a cap below the
    within-budget worst case turns into phantom entries in the ledger - a
    ``bounces`` count for a rollback that never ran, readable only by joining
    ``rollbacks_refused``. The number was justified by an enumeration in the
    comment beside it, that enumeration omitted the verify gate, and dr@3.4's
    shape bar was the sixth claimant on a cap sized for four.
    """
    worst_case = {
        "loopscan": 2,          # installed by default
        "dup_query": 2,         # installed by default
        "spin_breaker": 1,
        "force_finalize": 1,
        "verify_gate": 1,
        "report_shape": 1,      # dr@3.4
    }
    # RefusalObserver also rolls back (budget 1) but nothing installs it outside
    # tests; the day something does, it belongs in this sum.
    assert AgentLoop._MAX_HOOK_ROLLBACKS >= sum(worst_case.values()), (
        "a within-budget terminal gate can now be refused; raise the cap and "
        "extend the enumeration in the comment beside it"
    )


class _RecordingTerminalHook(AgentHook):
    """Answers at the terminal seam; records what context it was handed."""

    def __init__(self, answer: str | None):
        self._answer = answer
        self.calls = 0
        self.seen_turn_end: dict | None = None

    @property
    def name(self) -> str:
        return "RecordingTerminalHook"

    async def terminal_answerless(self, ctx):
        self.calls += 1
        self.seen_turn_end = dict(ctx.metadata.get("turn_end") or {})
        return HookDecision(short_circuit_result=self._answer) if self._answer else HookDecision()


class _DeadCallProvider(LLMProvider):
    """A provider error on the first call — the shape that ends a turn with no
    visible answer and no iteration hook consulted: the error response is
    deliberately not persisted, so history stops at whatever came before."""

    async def chat(self, messages, tools=None, model=None, **kwargs):
        return LLMResponse(content="Error calling LLM: upstream exploded", finish_reason="error")

    def get_default_model(self) -> str:
        return "stub"


class _ThinkOnlyProvider(LLMProvider):
    """Terminates with reasoning and no answer — non-empty content, no answer."""

    async def chat(self, messages, tools=None, model=None, **kwargs):
        return LLMResponse(content="still weighing the candidates</think>", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_cls", [_DeadCallProvider, _ThinkOnlyProvider])
async def test_terminal_seam_fires_when_the_turn_has_no_visible_answer(workspace, provider_cls):
    hook = _RecordingTerminalHook("Ada founded X in 1992.")
    agent = AgentLoop(
        provider=provider_cls(api_key="test"),
        workspace=workspace,
        model="stub",
        max_iterations=3,
        hooks=CompositeHook([hook]),
        restrict_to_workspace=True,
    )

    final, _, messages, _ = await agent._run_agent_loop([{"role": "user", "content": "q"}])

    assert hook.calls == 1
    assert hook.seen_turn_end["answerless"] is True
    assert final == "Ada founded X in 1992."
    # Persisted, so a trajectory reader and the user see the same final answer.
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == "Ada founded X in 1992."
    assert messages[-1]["observers"]["turn_end"]["salvaged"] is True


@pytest.mark.asyncio
async def test_terminal_seam_silent_when_an_answer_was_produced(workspace):
    hook = _RecordingTerminalHook("should not be used")
    agent = AgentLoop(
        provider=_ToolThenFinalProvider(),
        workspace=workspace,
        model="stub",
        max_iterations=4,
        hooks=CompositeHook([hook]),
        restrict_to_workspace=True,
    )

    await agent._run_agent_loop([{"role": "user", "content": "q"}])

    assert hook.calls == 0


@pytest.mark.asyncio
async def test_terminal_seam_fail_open_leaves_the_turn_alone(workspace):
    hook = _RecordingTerminalHook(None)
    agent = AgentLoop(
        provider=_ThinkOnlyProvider(api_key="test"),
        workspace=workspace,
        model="stub",
        max_iterations=3,
        hooks=CompositeHook([hook]),
        restrict_to_workspace=True,
    )

    final, _, messages, _ = await agent._run_agent_loop([{"role": "user", "content": "q"}])

    assert hook.calls == 1
    assert final == "still weighing the candidates</think>"
    assert "salvaged" not in messages[-1].get("observers", {}).get("turn_end", {})


class _InjectingRollbackHook(AgentHook):
    """Rolls back once and injects both roles, like the verify gate does."""

    async def after_iteration(self, ctx):
        if ctx.metadata.get("rolled"):
            return HookDecision()
        ctx.metadata["rolled"] = True
        return HookDecision(
            rollback=True,
            rollback_inject=[
                {"role": "assistant", "content": "the rejected draft"},
                {"role": "user", "content": "A reviewer rejected the draft above."},
            ],
        )


class _CountingBounceHook(AgentHook):
    """Rolls back once and records what before_iteration could read each time."""

    def __init__(self) -> None:
        self.seen: list[tuple[int | None, int]] = []

    async def before_iteration(self, ctx):
        self.seen.append((ctx.iteration, ctx.metadata.get("hook_rollbacks", 0)))
        return HookDecision()

    async def after_iteration(self, ctx):
        if ctx.metadata.get("rolled"):
            return HookDecision()
        ctx.metadata["rolled"] = True
        return HookDecision(rollback=True)


@pytest.mark.asyncio
async def test_an_honoured_rollback_is_counted_where_the_next_iteration_can_read_it(workspace):
    """The re-sample carries the same iteration number, so a hook scoped to the
    turn boundary (``AskUserGate``) cannot tell it from the first sampling by
    ``ctx.iteration`` alone. The loop records the honoured count beside
    ``rollbacks_refused``."""
    hook = _CountingBounceHook()
    agent = _make_agent(_ConstantProvider(), workspace, hook, max_iterations=3)

    await agent._run_agent_loop([{"role": "user", "content": "go"}])

    assert hook.seen == [(1, 0), (1, 1)]


@pytest.mark.asyncio
async def test_injected_user_turns_are_marked_flow_synthetic(workspace):
    """A hook rollback is the harness re-prompting itself.

    Marked at the application site rather than in each observer, so it holds
    for the verify gate, the finalize gate, the spin breaker and anything added
    later. The capture path reads this mark to keep harness text off the user
    track; the assistant half is genuine model output and stays unmarked.
    """
    agent = _make_agent(_ConstantProvider(), workspace, _InjectingRollbackHook(), max_iterations=2)

    _, _, messages, _ = await agent._run_agent_loop([{"role": "user", "content": "go"}])

    wanted = ("the rejected draft", "A reviewer rejected the draft above.")
    injected = [m for m in messages if m.get("content") in wanted]
    assert len(injected) == 2, messages
    by_role = {m["role"]: m for m in injected}
    assert by_role["user"].get("_flow_synthetic") is True
    assert "_flow_synthetic" not in by_role["assistant"]
    # The real user turn is not marked: it did not arrive via a rollback.
    real = [m for m in messages if m.get("content") == "go"]
    assert real and "_flow_synthetic" not in real[0]
