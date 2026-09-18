"""ConductHook: a conduct seated in the hook chain, one instance per turn.

What is under test is the seam, not any conduct: that each phase asks the
verbs that belong to it and renders the answer as the ``HookDecision`` the
composite already understands; that a turn gets one instance and the next
turn another; and that a phase-level caller can hand a bare namespace the
way the plugin tests do.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from raven.agent.hook.conduct import ConductHook
from raven.contracts.agent_conduct import Accept, AgentConduct, End, Intake, Resample, StepView
from raven.contracts.loop_hooks import AgentHookContext


class _Recording(AgentConduct):
    """Answers what it was told to and writes down every step it was shown."""

    made = 0

    def __init__(self, *, intake=None, tools=None, note=None, verdict=None, salvaged=None, outbound=None):
        type(self).made += 1
        self.steps: list[StepView] = []
        self.archived: list[str | None] = []
        self._intake, self._tools, self._note, self._verdict, self._salvaged, self._outbound = (
            intake,
            tools,
            note,
            verdict,
            salvaged,
            outbound,
        )

    async def intake(self, text, step):
        self.steps.append(step)
        return self._intake

    async def select_tools(self, offered, step):
        return self._tools

    async def advise(self, step):
        self.steps.append(step)
        return self._note

    async def review(self, step):
        self.steps.append(step)
        return self._verdict or Accept()

    async def salvage(self, step):
        return self._salvaged

    async def outbound(self, reply, step):
        return None if self._outbound is None else reply + self._outbound

    async def archive(self, step, reply):
        self.archived.append(reply)


def _ctx(**kw) -> AgentHookContext:
    return AgentHookContext(session_key="s1", **kw)


@pytest.mark.asyncio
async def test_one_turn_gets_one_conduct_and_the_next_turn_another():
    _Recording.made = 0
    hook = ConductHook("probe", lambda: _Recording(note="n"))
    turn_a, turn_b = _ctx(iteration=1), _ctx(iteration=1)
    await hook.before_iteration(turn_a)
    await hook.after_iteration(turn_a)
    assert _Recording.made == 1, "the same context is the same turn"
    await hook.before_iteration(turn_b)
    assert _Recording.made == 2, "a new context is a new turn"
    await hook.before_user_inbound(turn_b)
    assert _Recording.made == 3, "the first phase of a turn always starts fresh"


@pytest.mark.asyncio
async def test_two_turns_in_flight_at_once_keep_their_own_conducts():
    """One hook instance serves every turn a process runs, and a system turn can
    overlap a user one, so the turn each phase belongs to is the turn its state
    comes from -- interleaved, not merely consecutive."""
    _Recording.made = 0
    hook = ConductHook("probe", lambda: _Recording(note="n"))
    user, system = _ctx(iteration=1), _ctx(iteration=1)
    await hook.before_user_inbound(user)
    await hook.before_user_inbound(system)
    await hook.before_iteration(user)
    await hook.before_iteration(system)
    assert _Recording.made == 2, "two turns, two conducts, however their phases interleave"
    seen = [len(hook._seat(ctx).conduct.steps) for ctx in (user, system)]
    assert seen == [2, 2], "each turn's conduct saw only its own steps"


@pytest.mark.asyncio
async def test_a_conducts_addendum_is_replaced_rather_than_stacked():
    """The system message carries one copy of what a conduct adds: the next call
    takes the previous one back out before splicing this one in."""

    class Adding(AgentConduct):
        def __init__(self):
            self.n = 0

        async def system_addendum(self, step):
            self.n += 1
            return Intake(text=f"repo note {self.n}")

    hook = ConductHook("probe", Adding)
    ctx = _ctx(iteration=1, messages=[{"role": "system", "content": "base"}])
    await hook.before_iteration(ctx)
    assert ctx.messages[0]["content"] == "base\n\nrepo note 1"
    await hook.before_iteration(ctx)
    assert ctx.messages[0]["content"] == "base\n\nrepo note 2", "one copy, not two"


@pytest.mark.asyncio
async def test_what_a_conduct_archives_is_merged_into_the_turns_observers():
    """Two conducts stamping the same observer name keep both sets of counters."""

    class Filing(AgentConduct):
        def __init__(self, name, counters):
            self._name, self._counters = name, counters

        async def archive(self, step, reply):
            return {self._name: self._counters}

    ctx = SimpleNamespace(outbound_content="done", metadata={"observers": {"flow": {"kept": 1}}})
    await ConductHook("a", lambda: Filing("flow", {"added": 2})).after_send(ctx)
    await ConductHook("b", lambda: Filing("other", {"own": 3})).after_send(ctx)
    assert ctx.metadata["observers"] == {"flow": {"kept": 1, "added": 2}, "other": {"own": 3}}


@pytest.mark.asyncio
async def test_intake_reshapes_or_ends_the_turn():
    hook = ConductHook("probe", lambda: _Recording(intake=Intake(text="hello\n\n---\ncard")))
    decision = await hook.before_user_inbound(SimpleNamespace(inbound_content="hello"))
    assert decision.modified_content == "hello\n\n---\ncard" and decision.short_circuit_result is None
    hook = ConductHook("probe", lambda: _Recording(intake=Intake(text="x", reply=("fix the config", []))))
    decision = await hook.before_user_inbound(SimpleNamespace(inbound_content="x"))
    assert decision.short_circuit_result == ("fix the config", [])
    hook = ConductHook("probe", lambda: _Recording(intake=Intake(text="same")))
    assert (await hook.before_user_inbound(SimpleNamespace(inbound_content="same"))).modified_content is None


@pytest.mark.asyncio
async def test_before_iteration_narrows_tools_and_carries_the_note():
    offered = [{"function": {"name": "a"}}, {"function": {"name": "b"}}]
    hook = ConductHook("probe", lambda: _Recording(tools=offered[:1], note="mind the budget"))
    decision = await hook.before_iteration(_ctx(iteration=2, tools=list(offered)))
    assert decision.modified_tools == offered[:1]
    assert decision.append_note == "mind the budget"
    hook = ConductHook("probe", lambda: _Recording(tools=None))
    assert (await hook.before_iteration(_ctx(iteration=2, tools=list(offered)))).modified_tools is None


@pytest.mark.asyncio
async def test_review_verdicts_become_the_decisions_the_loop_acts_on():
    resample = Resample(
        "too thin",
        inject=[{"role": "user", "content": "more"}],
        overrides={"reasoning_effort": "high"},
        note="gate: thin",
    )
    hook = ConductHook("probe", lambda: _Recording(verdict=resample, note="unused when rolled back"))
    decision = await hook.after_iteration(_ctx(iteration=3, response=SimpleNamespace(content="draft", tool_calls=None)))
    assert decision.rollback is True
    assert decision.rollback_inject == [{"role": "user", "content": "more"}]
    assert decision.rollback_overrides == {"reasoning_effort": "high"}
    assert decision.notes == ["too thin", "gate: thin"], "both the reason and the note reach the loop"
    assert decision.append_note is None
    hook = ConductHook("probe", lambda: _Recording(verdict=End("done here")))
    assert (await hook.before_execute_tools(_ctx(iteration=3))).short_circuit_result == "done here"
    hook = ConductHook("probe", lambda: _Recording(verdict=Accept(note="fine"), note="carry on"))
    decision = await hook.after_iteration(_ctx(iteration=3))
    assert decision.rollback is False and decision.append_note == "carry on" and decision.notes == ["fine"]


@pytest.mark.asyncio
async def test_the_step_is_read_off_the_context_and_off_a_bare_namespace():
    hook = ConductHook("probe", lambda: _Recording())
    ctx = _ctx(
        iteration=4,
        messages=[{"role": "user", "content": "q"}],
        turn_question="q",
        turn_base=1,
        metadata={"hook_rollbacks": 2, "mode": "max"},
    )
    await hook.after_iteration(ctx)
    step = hook.conduct.steps[-1]
    assert (step.session_key, step.iteration, step.turn_base, step.question, step.rollbacks, step.mode) == (
        "s1",
        4,
        1,
        "q",
        2,
        "max",
    )
    assert isinstance(step.transcript, tuple) and step.transcript[0]["content"] == "q"
    await hook.after_iteration(SimpleNamespace(response=None))
    step = hook.conduct.steps[-1]
    assert (step.session_key, step.iteration, step.transcript, step.rollbacks, step.mode) == ("", 0, (), 0, None)


@pytest.mark.asyncio
async def test_salvage_and_the_outgoing_reply_land_where_the_loop_reads_them():
    hook = ConductHook("probe", lambda: _Recording(salvaged="rescued", outbound="\n\n--- 2 files changed"))
    assert (await hook.terminal_answerless(_ctx())).short_circuit_result == "rescued"
    decision = await hook.after_send(SimpleNamespace(outbound_content="done"))
    assert decision.modified_content == "done\n\n--- 2 files changed"
    assert hook.conduct.archived == ["done"], "archive is asked after the reply, with the reply as sent"
    hook = ConductHook("probe", lambda: _Recording())
    assert (await hook.after_send(SimpleNamespace(outbound_content="done"))).modified_content is None


@pytest.mark.asyncio
async def test_a_bound_harness_decides_what_a_conducts_verdict_does():
    """The seat asks the turn's modules, not the conduct: with a harness bound
    whose Action lets every step stand, the conduct's resample is not applied;
    with none bound, the default composition renders it as it always did."""
    from raven.agent.harness import bind_harness
    from raven.contracts.agent_conduct import Accept

    resample = Resample("thin", inject=[{"role": "user", "content": "more"}])
    hook = ConductHook("probe", lambda: _Recording(verdict=resample))
    ctx = _ctx(iteration=1, response=SimpleNamespace(content="draft", tool_calls=None))
    assert (await hook.after_iteration(ctx)).rollback is True, "unbound: the conduct's own verdict"

    class Lenient:
        async def judge_step(self, step, conducts):
            assert len(conducts) == 1, "the seat hands the module this turn's conducts"
            return Accept(note="action: overruled")

        async def rescue(self, step, conducts):
            return "the module's own salvage"

    class Louder:
        async def guide(self, step, conducts):
            assert len(conducts) == 1
            return "planning: the module's own advice"

    class Rewriting:
        async def read_inbound(self, text, step, conducts):
            assert len(conducts) == 1
            return Intake(text=f"{text} (as the module reads it)")

    harness = SimpleNamespace(action=Lenient(), planning=Louder(), memory=Rewriting(), capability=None)
    with bind_harness(harness):
        decision = await hook.after_iteration(
            _ctx(iteration=1, response=SimpleNamespace(content="draft", tool_calls=None))
        )
        assert decision.rollback is False and decision.notes == ["action: overruled"]
        assert decision.append_note == "planning: the module's own advice", "Planning answers for the advice"
        assert (await hook.terminal_answerless(_ctx())).short_circuit_result == "the module's own salvage"
        inbound = await hook.before_user_inbound(_ctx(inbound_content="q"))
        assert inbound.modified_content == "q (as the module reads it)", "Memory answers for the intake"


# --------------------------------------------------------------------------- #
# What the contract claims, asserted                                           #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_conduct_cannot_write_through_the_step_it_is_shown():
    """ "Read-only" is the paper's word, so the rows are rows a write raises on.
    Without this the tuple froze the sequence and left every message inside it
    the loop's own object, open to edit from a seam that returns its answers."""

    wrote: list[str] = []

    class Mutator(AgentConduct):
        async def advise(self, step):
            for field in (step.transcript, step.history, step.tools):
                if field:
                    try:
                        field[0]["role"] = "rewritten"
                        wrote.append("yes")
                    except TypeError:
                        pass
            return None

    ctx = _ctx(
        iteration=1,
        messages=[{"role": "user", "content": "q"}],
        session_history=[{"role": "assistant", "content": "a"}],
        tools=[{"name": "web_search"}],
    )
    await ConductHook("probe", Mutator).before_iteration(ctx)

    assert wrote == [], "a conduct wrote through the step"
    assert ctx.messages[0]["role"] == "user"
    assert ctx.session_history[0]["role"] == "assistant"


@pytest.mark.asyncio
async def test_two_seats_each_keep_one_addendum_in_the_system_message():
    """Two conducts append to the same system message, and each takes its own
    text back out. Offsets could not do this: the first seat's strip shifts the
    second seat's text, so the second found nothing and spliced a second copy."""

    class Adds(AgentConduct):
        def __init__(self, text):
            self.text = text

        async def system_addendum(self, step):
            return Intake(text=self.text)

    ctx = _ctx(iteration=1, messages=[{"role": "system", "content": "base"}])
    first, second = ConductHook("a", lambda: Adds("A")), ConductHook("b", lambda: Adds("B"))
    for _ in range(3):
        await first.before_iteration(ctx)
        await second.before_iteration(ctx)

    assert ctx.messages[0]["content"] == "base\n\nA\n\nB", "an addendum stacked or went missing"


@pytest.mark.asyncio
async def test_a_conduct_may_contribute_a_tool_of_its_own_to_the_iteration():
    """The array is the iteration's, not a subset of what it was offered: the
    research flow contributes its escalation tool this way. Handing a name back
    is not granting it -- the registry still adjudicates every call."""

    class Contributes(AgentConduct):
        async def select_tools(self, offered, step):
            return [*offered, {"name": "request_research"}]

    offered = [{"name": "web_search"}]
    decision = await ConductHook("probe", Contributes).before_iteration(_ctx(iteration=1, tools=list(offered)))
    assert [t["name"] for t in decision.modified_tools] == ["web_search", "request_research"]


@pytest.mark.asyncio
async def test_a_conduct_that_only_implements_the_verbs_is_still_heard():
    """The verbs are the contract; inheriting the base class is a convenience.
    An object that implements them without it used to have its answer dropped
    by the composite, because the seat asked it for the trail unconditionally."""

    class Standalone:
        async def review(self, step):
            return End("closed by a conduct that inherits nothing")

    decision = await ConductHook("probe", Standalone).before_execute_tools(_ctx(iteration=1))
    assert decision.short_circuit_result == "closed by a conduct that inherits nothing"


@pytest.mark.asyncio
async def test_the_reason_a_resample_was_written_with_reaches_the_loops_notes():
    """``Resample`` takes its reason positionally, so an author writes it there
    first. Reading only the note dropped it."""

    class Sends(AgentConduct):
        async def review(self, step):
            return Resample("the draft is thin")

    decision = await ConductHook("probe", Sends).after_iteration(
        _ctx(iteration=1, response=SimpleNamespace(content="draft", tool_calls=None))
    )
    assert decision.rollback is True and decision.notes == ["the draft is thin"]


@pytest.mark.asyncio
async def test_every_verb_the_roles_seat_goes_through_them():
    """Seven verbs have a module seat and the seat asks it. Without this the
    three that were applied straight off the conduct had nowhere to compose two
    participants, nowhere to vet an answer, and nothing a replacement could
    decide -- and no test would have noticed."""
    from raven.agent.harness import bind_harness

    asked: list[str] = []

    class Memory:
        async def read_inbound(self, text, step, conducts):
            asked.append("read_inbound")
            return None

        async def compose_addendum(self, step, conducts):
            asked.append("compose_addendum")
            return None

        async def file_record(self, step, reply, conducts):
            asked.append("file_record")
            return None

    class Capability:
        async def offer(self, offered, step, conducts):
            asked.append("offer")
            return None

    class Planning:
        async def guide(self, step, conducts):
            asked.append("guide")
            return None

    class Action:
        async def judge_step(self, step, conducts):
            asked.append("judge_step")
            return Accept()

        async def rescue(self, step, conducts):
            asked.append("rescue")
            return None

    hook = ConductHook("probe", lambda: _Recording())
    harness = SimpleNamespace(memory=Memory(), planning=Planning(), capability=Capability(), action=Action())
    with bind_harness(harness):
        ctx = _ctx(iteration=1, inbound_content="q", tools=[{"name": "web_search"}])
        await hook.before_user_inbound(ctx)
        await hook.before_iteration(ctx)
        await hook.before_execute_tools(ctx)
        await hook.after_iteration(ctx)
        await hook.terminal_answerless(ctx)
        await hook.after_send(SimpleNamespace(outbound_content="done", metadata={}))

    assert set(asked) == {
        "read_inbound",
        "compose_addendum",
        "file_record",
        "offer",
        "guide",
        "judge_step",
        "rescue",
    }, f"a verb bypassed its role: {sorted(set(asked))}"
