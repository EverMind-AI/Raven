"""The two budgets a product may put on a turn: a wall clock, and a rerun.

Both are supplied as data on the turn's hook metadata rather than read from any
config the loop knows, because the loop serves every agent and must not know one
of them. So the first thing each test does is say what a product asked for, and
the last thing several of them check is that an agent asking for nothing is
bounded exactly as it was before either budget existed.

The rerun is the conditional one: a turn that produced no answer is run again
from the original question. It never salvages the failed attempt -- squeezing an
answer out of the wreckage was measured to turn a detectable zero into a
confident wrong one, and to empty this trigger while doing it.
"""

from __future__ import annotations

import pytest

from raven.agent.loop import TURN_BUDGETS_KEY, AgentLoop, TurnBudgets, turn_budgets, turn_path
from raven.agent.loop.bundles import EngineWiring, HostWiring, ToolWiring, TurnPolicy
from raven.config.raven import CheckpointConfig, RuntimeConfig
from raven.contracts.loop_hooks import AgentHook, HookDecision
from raven.providers.base import LLMProvider, LLMResponse
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


class _Budgeted(AgentHook):
    """A product asking the loop for bounds, the way the research flow does."""

    def __init__(self, **budgets) -> None:
        self._budgets = budgets

    @property
    def name(self) -> str:
        return "Budgeted"

    async def before_user_inbound(self, ctx) -> HookDecision:
        ctx.metadata[TURN_BUDGETS_KEY] = dict(self._budgets)
        return HookDecision()


class _Present(AgentHook):
    """Registered and does nothing. The turn's metadata record is written for hooks
    to read, so a loop with none has nobody to write it for."""

    @property
    def name(self) -> str:
        return "Present"


class _Scripted(LLMProvider):
    """Answers from a script of attempts, each a fixed list of replies.

    One plan is consumed per attempt and the next begins where the last ended, so
    ``attempts_used`` counts how many times the turn actually ran rather than how
    many calls it made. The seed each plan opened on is recorded, which is how the
    "starts from the question" test reads what the rerun re-ran.
    """

    def __init__(self, *attempts: list[LLMResponse]) -> None:
        super().__init__(api_key="test")
        self._attempts = list(attempts)
        self._plan = 0
        self._step = 0
        self.calls = 0
        self.seeds: list[list[dict]] = []

    @property
    def attempts_used(self) -> int:
        return self._plan + (1 if self._step else 0)

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7, **_kw):
        self.calls += 1
        plan = self._attempts[min(self._plan, len(self._attempts) - 1)]
        if self._step == 0:
            self.seeds.append([dict(m) for m in messages])
        out = plan[min(self._step, len(plan) - 1)]
        self._step += 1
        if self._step >= len(plan):
            self._plan += 1
            self._step = 0
        return out

    def get_default_model(self) -> str:
        return "stub"


#: How many empty replies the loop's own empty-response recovery spends before it
#: gives up. A turn that spends them ends with ``status="error"`` and no answer,
#: which is the shape every dead-end test below needs.
_RECOVERY_BUDGET = 3


def _dead() -> list[LLMResponse]:
    """An attempt that answers nothing at all and exhausts empty-response recovery."""
    return [LLMResponse(content="", finish_reason="stop")] * _RECOVERY_BUDGET


def _answers(text: str) -> list[LLMResponse]:
    return [LLMResponse(content=text, finish_reason="stop")]


def _loop(tmp_path, provider, hooks=(), max_iterations: int = 10) -> AgentLoop:
    loop = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model="stub",
        policy=TurnPolicy(max_iterations=max_iterations),
        host=HostWiring(hooks=list(hooks)),
        tools=ToolWiring(restrict_to_workspace=True),
        engine=EngineWiring(runtime_config=RuntimeConfig(checkpoint=CheckpointConfig(policy="never"))),
    )

    async def _noop(**_kw) -> None:
        return None

    loop._start_executor = _noop
    loop._connect_mcp = _noop
    return loop


def _req(text: str = "who won?") -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="cli", chat_id="c1", sender_id="u", chat_type=ChatType.DM),
        text=text,
    )


@pytest.mark.asyncio
async def test_a_dead_turn_runs_again_and_the_second_answer_wins(tmp_path):
    provider = _Scripted(_dead(), _answers("Alice Smith won it."))
    agent = _loop(tmp_path, provider, [_Budgeted(dead_end_retries=1)])

    out = await agent._process_message(_req(), session_key="s1")

    assert out[0] == "Alice Smith won it."
    assert provider.attempts_used == 2, "the turn ran twice"


@pytest.mark.asyncio
async def test_a_live_turn_is_never_re_run(tmp_path):
    """The budget is a ceiling on dead turns, not a repeat count."""
    provider = _Scripted(_answers("Alice Smith won it."))
    agent = _loop(tmp_path, provider, [_Budgeted(dead_end_retries=1)])

    out = await agent._process_message(_req(), session_key="s1")

    assert out[0] == "Alice Smith won it."
    assert provider.attempts_used == 1


@pytest.mark.asyncio
async def test_an_agent_that_asks_for_nothing_is_never_re_run(tmp_path):
    """Every other agent on this loop. A dead turn stays dead, exactly as before."""
    provider = _Scripted(_dead())
    agent = _loop(tmp_path, provider)

    out = await agent._process_message(_req(), session_key="s1")

    assert provider.attempts_used == 1
    assert "produced no answer" in out[0], "the dead turn was returned as it was"


@pytest.mark.asyncio
async def test_switching_it_off_restores_the_old_behaviour(tmp_path):
    provider = _Scripted(_dead(), _answers("Alice Smith won it."))
    agent = _loop(tmp_path, provider, [_Budgeted(dead_end_retries=0)])

    await agent._process_message(_req(), session_key="s1")

    assert provider.attempts_used == 1


@pytest.mark.asyncio
async def test_the_budget_is_a_ceiling_not_a_loop(tmp_path):
    """Three dead attempts, one retry allowed: the turn stops after the second."""
    provider = _Scripted(_dead(), _dead(), _dead())
    agent = _loop(tmp_path, provider, [_Budgeted(dead_end_retries=1)])

    out = await agent._process_message(_req(), session_key="s1")

    assert provider.attempts_used == 2
    assert "produced no answer" in out[0], "still dead after the one retry it was allowed"


@pytest.mark.asyncio
async def test_the_retry_starts_from_the_question_not_from_the_wreckage(tmp_path):
    """The whole value of a rerun is a fresh start. Handed the failed attempt's
    transcript it would inherit the dead end it is meant to escape, so the seed is
    copied before the first attempt rather than rebuilt from a list the loop has
    since appended to."""
    provider = _Scripted(_dead(), _answers("Alice Smith won it."))
    agent = _loop(tmp_path, provider, [_Budgeted(dead_end_retries=1)])

    await agent._process_message(_req(), session_key="s1")

    first, second = provider.seeds[0], provider.seeds[-1]
    assert len(second) == len(first)
    assert [m.get("role") for m in second] == [m.get("role") for m in first]
    assert not any(m.get("role") == "tool" for m in second)


@pytest.mark.asyncio
async def test_the_reason_filter_narrows_the_trigger(tmp_path):
    """A product that only wants stranded turns re-run must not get every dead one."""
    provider = _Scripted(_dead(), _answers("Alice Smith won it."))
    agent = _loop(tmp_path, provider, [_Budgeted(dead_end_retries=1, dead_end_reasons=["refusal_string"])])

    await agent._process_message(_req(), session_key="s1")

    assert provider.attempts_used == 1, "no reason matched, so nothing was re-run"


@pytest.mark.asyncio
async def test_a_spent_wall_clock_stops_the_retry(tmp_path, monkeypatch):
    """Each attempt gets its own clock, which is safe only while the first attempt
    died early -- the usual case, because a failing run is a shortcut. When the
    first attempt instead SPENT the budget, a retry would hand back a fresh copy of
    the clock that had just fired, so the turn stops with whatever it has.

    The clock is a counter rather than a wait: one tick per reading, which puts the
    loop's own per-iteration readings inside the budget and the turn-level one past
    it. That split is the whole branch, and it cannot be reached by waiting for real
    seconds in a test.
    """
    ticks = iter(range(1000))
    monkeypatch.setattr(turn_path, "monotonic", lambda: float(next(ticks)))
    provider = _Scripted(_dead(), _answers("Alice Smith won it."))
    agent = _loop(tmp_path, provider, [_Budgeted(dead_end_retries=1, wall_clock_seconds=5.0)])

    await agent._process_message(_req(), session_key="s1")

    assert provider.attempts_used == 1, "the spent clock stopped the rerun"
    # Not vacuous: the first attempt must have run to empty-response exhaustion. If
    # the loop's own clock had cut it short instead, the turn would have wrapped up
    # with an answer and there would have been no dead end to decline to re-run.
    assert provider.calls == _RECOVERY_BUDGET


@pytest.mark.asyncio
async def test_a_budget_that_is_not_set_does_not_block_the_retry(tmp_path):
    """The other direction of the same branch: no clock is not a spent clock."""
    provider = _Scripted(_dead(), _answers("Alice Smith won it."))
    agent = _loop(tmp_path, provider, [_Budgeted(dead_end_retries=1)])

    await agent._process_message(_req(), session_key="s1")

    assert provider.attempts_used == 2


@pytest.mark.asyncio
async def test_a_spent_wall_clock_wraps_up_rather_than_going_silent(tmp_path):
    """A deadline must not be a way for a turn to end with nothing. It leaves through
    the same wrap-up path the iteration cap uses, so the reader gets the best partial
    answer rather than silence."""
    provider = _Scripted([LLMResponse(content="", finish_reason="stop")])
    agent = _loop(tmp_path, provider, [_Present()])
    meta: dict = {TURN_BUDGETS_KEY: {"wall_clock_seconds": 0.0001}}

    final, _used, _msgs, outcome = await agent._run_agent_loop(
        [{"role": "user", "content": "who won?"}], hook_metadata=meta
    )

    assert outcome.status == "interrupted"
    assert final, "the turn is never silent"
    assert meta["turn_end"]["stopped_by"] == "wall_clock"
    assert meta["turn_end"]["wall_clock_budget_s"] == 0


@pytest.mark.asyncio
async def test_a_turn_with_no_budget_records_no_stop(tmp_path):
    """Every other agent on this loop: no budget written, nothing bounded, and
    ``stopped_by`` says the model finished on its own."""
    provider = _Scripted(_answers("Alice Smith won it."))
    agent = _loop(tmp_path, provider, [_Present()])
    meta: dict = {}

    final, _used, _msgs, outcome = await agent._run_agent_loop(
        [{"role": "user", "content": "who won?"}], hook_metadata=meta
    )

    assert final == "Alice Smith won it."
    assert outcome.status == "completed"
    assert meta["turn_end"]["stopped_by"] is None
    assert meta["turn_end"]["wall_clock_budget_s"] is None


@pytest.mark.asyncio
async def test_the_iteration_cap_names_itself(tmp_path):
    """``stopped_by`` exists because ``status`` cannot answer this: the cap and a
    spent clock both land on ``interrupted`` and both produce a wrap-up that reads
    like an ordinary answer."""
    provider = _Scripted([LLMResponse(content="", finish_reason="stop")])
    agent = _loop(tmp_path, provider, [_Present()], max_iterations=2)
    meta: dict = {}

    _final, _used, _msgs, outcome = await agent._run_agent_loop(
        [{"role": "user", "content": "who won?"}], hook_metadata=meta
    )

    assert outcome.status == "interrupted"
    assert meta["turn_end"]["stopped_by"] == "iteration_cap"


@pytest.mark.asyncio
async def test_the_turn_end_record_carries_only_scalars_a_reader_keeps(tmp_path):
    """Ints, not floats. An observer chain that keeps values by type drops a float
    without a word, so a budget recorded as one would simply not be there."""
    provider = _Scripted(_answers("Alice Smith won it."))
    agent = _loop(tmp_path, provider, [_Present()])
    meta: dict = {TURN_BUDGETS_KEY: {"wall_clock_seconds": 3600.5}}

    await agent._run_agent_loop([{"role": "user", "content": "who won?"}], hook_metadata=meta)

    record = meta["turn_end"]
    assert isinstance(record["wall_clock_budget_s"], int)
    assert isinstance(record["turn_elapsed_s"], int)
    assert all(v is None or isinstance(v, (int, str, bool)) for v in record.values())


# --------------------------------------------------------------------------- #
# The budget reader: a loop reading a dict a plugin wrote must not break on it  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "metadata",
    [None, {}, {TURN_BUDGETS_KEY: None}, {TURN_BUDGETS_KEY: "unbounded"}, {TURN_BUDGETS_KEY: []}],
)
def test_nothing_readable_means_unbounded(metadata):
    """The default is what every agent that writes nothing gets, so it has to be the
    behaviour those agents had before this key existed."""
    assert turn_budgets(metadata) == TurnBudgets()


@pytest.mark.parametrize(
    "raw",
    [
        {"wall_clock_seconds": True, "dead_end_retries": True},
        {"wall_clock_seconds": "3600", "dead_end_retries": "1"},
        {"wall_clock_seconds": 0, "dead_end_retries": 0},
        {"wall_clock_seconds": -5, "dead_end_retries": -1},
    ],
)
def test_a_value_that_is_not_a_budget_leaves_the_turn_unbounded(raw):
    """``bool`` is an ``int``, so a switch left in a number's place would otherwise
    read as one retry or a one-second deadline: a misconfiguration that ENDS turns
    rather than one that is ignored. The same goes for a zero or a negative, which
    name no budget."""
    assert turn_budgets({TURN_BUDGETS_KEY: raw}) == TurnBudgets()


def test_a_well_formed_budget_is_read_whole():
    budgets = turn_budgets(
        {TURN_BUDGETS_KEY: {"wall_clock_seconds": 3600, "dead_end_retries": 2, "dead_end_reasons": ["stranded"]}}
    )

    assert budgets == TurnBudgets(wall_clock_seconds=3600.0, dead_end_retries=2, dead_end_reasons=("stranded",))
