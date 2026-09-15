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


class _Watches(_Budgeted):
    """A product's hook that also keeps the turn's end record. ``after_send`` is
    handed the turn's own metadata dict, which is where the loop leaves it."""

    def __init__(self, **budgets) -> None:
        super().__init__(**budgets)
        self.turn_end: dict = {}

    async def after_send(self, ctx) -> HookDecision:
        self.turn_end = dict(ctx.metadata.get("turn_end") or {})
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
async def test_the_rerun_tells_the_reader_the_turn_is_starting_over(tmp_path):
    """The reader has already watched one attempt produce nothing, so silence while
    the whole turn runs again reads as a hang. What the line may not do is name the
    one product that asked for the budget, because the loop serves every agent, or
    call this the first attempt, because the budget allows more reruns than one.
    """
    notes: list[str] = []

    async def _progress(text: str) -> None:
        notes.append(text)

    provider = _Scripted(_dead(), _answers("Alice Smith won it."))
    agent = _loop(tmp_path, provider, [_Budgeted(dead_end_retries=1)])

    await agent._process_message(_req(), session_key="s1", on_progress=_progress)

    spoken = [n for n in notes if "again" in n]
    assert len(spoken) == 1, f"the rerun said nothing, or said it twice: {notes}"
    assert "research" not in spoken[0].lower(), "the loop names no agent"
    assert "first attempt" not in spoken[0].lower(), "nor a rerun number it cannot know"


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
    # Whole messages, not a shape: the seed is copied one dict deep, so a first
    # attempt that reached into one of them would leave the rerun opening on a
    # transcript that matches this on role and length and not on what it says.
    assert second == first, "the rerun opened on something other than the question"
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
    """A turn whose budget is already gone stops with what it has rather than
    starting an attempt that would break on its own first iteration.

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
async def test_the_rerun_continues_the_turn_s_episode_numbering(tmp_path):
    """``EpisodeStart.index`` is the 0-based step within the TURN, and the loop
    numbers episodes from an iteration count that restarts every time the turn is
    run again. The TUI keys episode rows and their fold state by this index, so two
    attempts numbering from zero would not merely look odd: the rerun's first step
    would collide with the first attempt's and share its rendering state.
    """
    seen: list[int] = []

    async def _episode(index: int) -> None:
        seen.append(index)

    provider = _Scripted(_dead(), _answers("Alice Smith won it."))
    agent = _loop(tmp_path, provider, [_Budgeted(dead_end_retries=1)])

    await agent._process_message(_req(), session_key="s1", on_episode_start=_episode)

    assert provider.attempts_used == 2, "the rerun ran, so both attempts are in there"
    assert seen == list(range(len(seen))), f"the rerun restarted the numbering: {seen}"


@pytest.mark.asyncio
async def test_the_rerun_spends_the_turn_s_clock_not_a_fresh_copy(tmp_path, monkeypatch):
    """Three-second model calls, a ten-second budget, and two dead attempts. Given a
    clock per attempt the turn runs to eighteen seconds -- nearly two budgets -- because
    the first attempt stopped just under the limit and the rerun started a new one. The
    documented overrun is one budget plus at most one iteration, and it is the turn that
    is bounded, not the attempt.

    The clock reads off the work done rather than off how many times it is read, so the
    test says what a model call costs and nothing depends on how often the loop looks.
    """
    provider = _Scripted(_dead(), _dead())
    monkeypatch.setattr(turn_path, "monotonic", lambda: 3.0 * provider.calls)
    agent = _loop(tmp_path, provider, [_Budgeted(dead_end_retries=1, wall_clock_seconds=10.0)])

    await agent._process_message(_req(), session_key="s1")

    assert provider.attempts_used == 2, "the rerun did start, so the clock was under test"
    # Five calls: the first attempt's three, which reach nine seconds and die there; the
    # one iteration the rerun was still entitled to begin at nine, which is the overrun
    # the budget documents; and the wrap-up that overrun produces. Given a clock per
    # attempt the rerun instead receives ten fresh seconds, spends its own three calls,
    # and the turn returns at eighteen.
    assert provider.calls == 5, f"the rerun was handed a fresh budget: the turn ran to {3.0 * provider.calls}s"


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
    assert meta["turn_end"]["attempt"] == 1, "the turn ran once, and the record says which run"


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
async def test_the_turn_end_record_says_which_attempt_it_counted(tmp_path, monkeypatch):
    """The record holds two scopes at once, deliberately: ``iterations`` is this
    attempt's, because that is what the loop counts, while ``turn_elapsed_s`` spans
    the turn, because that is what the budget bounds. ``attempt`` is what lets a
    reader tell them apart -- one iteration beside twelve seconds otherwise reads as
    a single very slow call.
    """
    provider = _Scripted(_dead(), _answers("Alice Smith won it."))
    monkeypatch.setattr(turn_path, "monotonic", lambda: 3.0 * provider.calls)
    hook = _Watches(dead_end_retries=1, wall_clock_seconds=3600)
    agent = _loop(tmp_path, provider, [hook])

    await agent._process_message(_req(), session_key="s1")

    assert hook.turn_end["attempt"] == 2, "the record is the rerun's"
    assert hook.turn_end["iterations"] == 1, "which ran one iteration of its own"
    assert hook.turn_end["turn_elapsed_s"] == 12, "after twelve seconds of turn, dead attempt included"


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
# The terminal seam: what an attempt with a rerun coming does not pay for      #
# --------------------------------------------------------------------------- #


class _Salvages(_Budgeted):
    """A product's terminal gate, which manufactures an answer for a turn that ended
    with none. The research chain's does exactly this and may spend minutes on it, so
    ``costs`` is what one call takes off the turn's clock.
    """

    answer = "Here is what I managed to gather."

    def __init__(self, clock: dict | None = None, costs: float = 0.0, **budgets) -> None:
        super().__init__(**budgets)
        self._clock = clock
        self._costs = costs
        self.calls = 0

    async def terminal_answerless(self, ctx) -> HookDecision:
        self.calls += 1
        if self._clock is not None:
            self._clock["now"] += self._costs
        return HookDecision(short_circuit_result=self.answer)


class _Timed(_Scripted):
    """A scripted provider whose calls cost the turn's clock. It lets a test say what
    the research took and what the gate took as two separate numbers, which is the
    whole question when one of them is paid for work about to be discarded.
    """

    def __init__(self, clock: dict, costs: float, *attempts: list[LLMResponse]) -> None:
        super().__init__(*attempts)
        self._clock = clock
        self._costs = costs

    async def chat(self, *args, **kwargs):
        self._clock["now"] += self._costs
        return await super().chat(*args, **kwargs)


@pytest.mark.asyncio
async def test_the_attempt_with_a_rerun_coming_is_not_salvaged_first(tmp_path):
    """Two dead attempts and one gate. The gate is for the turn, not for an attempt
    the turn is about to abandon, so it is offered the second and not the first.
    """
    hook = _Salvages(dead_end_retries=1)
    provider = _Scripted(_dead(), _dead())
    agent = _loop(tmp_path, provider, [hook])

    out = await agent._process_message(_req(), session_key="s1")

    assert provider.attempts_used == 2, "the rerun ran, so there were two seams to offer"
    assert hook.calls == 1, f"the seam fired on both attempts, or on neither: {hook.calls}"
    assert out[0] == _Salvages.answer, "and the attempt with nothing after it kept its salvage"


@pytest.mark.asyncio
async def test_a_salvaged_answer_cannot_empty_the_rerun_s_own_trigger(tmp_path):
    """The stranded test asks whether the trajectory ended on the model's own prose,
    and a salvage ends it on exactly that. Run first, it answers the question the
    rerun is about to ask, and the turn ships the manufactured answer instead of the
    researched one -- salvage winning by suppressing the thing measured to beat it.
    """
    hook = _Salvages(dead_end_retries=1, dead_end_reasons=("stranded",))
    provider = _Scripted(_dead(), _answers("Alice Smith won it."))
    agent = _loop(tmp_path, provider, [hook])

    out = await agent._process_message(_req(), session_key="s1")

    assert provider.attempts_used == 2, "the salvage answered the rerun's question for it"
    assert out[0] == "Alice Smith won it."
    assert hook.calls == 0, "and no seam fired: the rerun's own answer is not answerless"


@pytest.mark.asyncio
async def test_the_salvage_a_rerun_would_discard_does_not_spend_the_turn_s_clock(tmp_path, monkeypatch):
    """A hundred-second turn, ten seconds a model call, and a gate that takes the two
    hundred and forty the shipped one is allowed. Paid before the rerun decision, that
    single call overruns the turn on its own, and the rerun it was going to be thrown
    away for never starts.
    """
    clock = {"now": 0.0}
    monkeypatch.setattr(turn_path, "monotonic", lambda: clock["now"])
    hook = _Salvages(clock=clock, costs=240.0, dead_end_retries=1, wall_clock_seconds=100.0)
    provider = _Timed(clock, 10.0, _dead(), _answers("Alice Smith won it."))
    agent = _loop(tmp_path, provider, [hook])

    out = await agent._process_message(_req(), session_key="s1")

    assert hook.calls == 0, "the turn bought an answer it was about to throw away"
    assert out[0] == "Alice Smith won it."
    assert clock["now"] < 100.0, f"the turn overran its budget on salvage: {clock['now']}s"


@pytest.mark.asyncio
async def test_the_last_attempt_keeps_the_salvage_the_clock_denies_it_a_rerun_for(tmp_path, monkeypatch):
    """The other side of that branch. A spent clock is a no to the rerun, so it has to
    be a yes to the seam: skipping both is the one outcome that leaves a dead turn
    with nothing at all.

    Ten seconds a call against a twenty-five second turn, and the third call -- the
    last one empty-response recovery has -- both ends the attempt with no answer and
    carries the clock past the budget, which is the only way the two conditions meet.
    """
    clock = {"now": 0.0}
    monkeypatch.setattr(turn_path, "monotonic", lambda: clock["now"])
    hook = _Salvages(dead_end_retries=1, wall_clock_seconds=25.0)
    provider = _Timed(clock, 10.0, _dead(), _answers("Alice Smith won it."))
    agent = _loop(tmp_path, provider, [hook])

    out = await agent._process_message(_req(), session_key="s1")

    assert provider.attempts_used == 1, "the first attempt was meant to spend the clock"
    assert hook.calls == 1, "the turn had no rerun coming and was not salvaged"
    assert out[0] == _Salvages.answer


@pytest.mark.asyncio
async def test_an_agent_that_budgets_no_rerun_is_salvaged_exactly_as_before(tmp_path):
    """Every other agent on this loop. Nothing decides against the seam, so it fires
    on the one dead attempt there is.
    """
    hook = _Salvages()
    provider = _Scripted(_dead())
    agent = _loop(tmp_path, provider, [hook])

    out = await agent._process_message(_req(), session_key="s1")

    assert provider.attempts_used == 1
    assert hook.calls == 1
    assert out[0] == _Salvages.answer


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
