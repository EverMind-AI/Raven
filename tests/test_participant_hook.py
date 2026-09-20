"""ParticipantHook: a participant seated in the hook chain, one instance per turn.

What is under test is the seam, not any participant: that each phase asks the
verbs that belong to it and renders the answer as the ``HookDecision`` the
composite already understands; that a turn gets one instance and the next
turn another; and that a phase-level caller can hand a bare namespace the
way the plugin tests do.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from raven.agent.hook.participant import ParticipantHook
from raven.contracts.loop_hooks import AgentHookContext
from raven.contracts.participant import Accept, AgentParticipant, End, Intake, Resample, StepView


class _Recording(AgentParticipant):
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
async def test_one_turn_gets_one_participant_and_the_next_turn_another():
    _Recording.made = 0
    hook = ParticipantHook("probe", lambda: _Recording(note="n"))
    turn_a, turn_b = _ctx(iteration=1), _ctx(iteration=1)
    await hook.before_iteration(turn_a)
    await hook.after_iteration(turn_a)
    assert _Recording.made == 1, "the same context is the same turn"
    await hook.before_iteration(turn_b)
    assert _Recording.made == 2, "a new context is a new turn"
    await hook.before_user_inbound(turn_b)
    assert _Recording.made == 3, "the first phase of a turn always starts fresh"


@pytest.mark.asyncio
async def test_two_turns_in_flight_at_once_keep_their_own_participants():
    """One hook instance serves every turn a process runs, and a system turn can
    overlap a user one, so the turn each phase belongs to is the turn its state
    comes from -- interleaved, not merely consecutive."""
    _Recording.made = 0
    hook = ParticipantHook("probe", lambda: _Recording(note="n"))
    user, system = _ctx(iteration=1), _ctx(iteration=1)
    await hook.before_user_inbound(user)
    await hook.before_user_inbound(system)
    await hook.before_iteration(user)
    await hook.before_iteration(system)
    assert _Recording.made == 2, "two turns, two participants, however their phases interleave"
    seen = [len(hook._seat(ctx).participant.steps) for ctx in (user, system)]
    assert seen == [2, 2], "each turn's participant saw only its own steps"


@pytest.mark.asyncio
async def test_a_participants_addendum_is_replaced_rather_than_stacked():
    """The system message carries one copy of what a participant adds: the next call
    takes the previous one back out before splicing this one in."""

    class Adding(AgentParticipant):
        def __init__(self):
            self.n = 0

        async def system_addendum(self, step):
            self.n += 1
            return Intake(text=f"repo note {self.n}")

    hook = ParticipantHook("probe", Adding)
    ctx = _ctx(iteration=1, messages=[{"role": "system", "content": "base"}])
    await hook.before_iteration(ctx)
    assert ctx.messages[0]["content"] == "base\n\nrepo note 1"
    await hook.before_iteration(ctx)
    assert ctx.messages[0]["content"] == "base\n\nrepo note 2", "one copy, not two"


@pytest.mark.asyncio
async def test_what_a_participant_archives_is_merged_into_the_turns_observers():
    """Mapping observers merge while scalar observer values remain intact."""

    class Filing(AgentParticipant):
        def __init__(self, name, counters):
            self._name, self._counters = name, counters

        async def archive(self, step, reply):
            return {self._name: self._counters}

    ctx = SimpleNamespace(outbound_content="done", metadata={"observers": {"flow": {"kept": 1}}})
    await ParticipantHook("a", lambda: Filing("flow", {"added": 2})).after_send(ctx)
    await ParticipantHook("b", lambda: Filing("other", {"own": 3})).after_send(ctx)
    await ParticipantHook("c", lambda: Filing("trail", "rendered trail")).after_send(ctx)
    assert ctx.metadata["observers"] == {
        "flow": {"kept": 1, "added": 2},
        "other": {"own": 3},
        "trail": "rendered trail",
    }


@pytest.mark.asyncio
async def test_intake_reshapes_or_ends_the_turn():
    hook = ParticipantHook("probe", lambda: _Recording(intake=Intake(text="hello\n\n---\ncard")))
    decision = await hook.before_user_inbound(SimpleNamespace(inbound_content="hello"))
    assert decision.modified_content == "hello\n\n---\ncard" and decision.short_circuit_result is None
    hook = ParticipantHook("probe", lambda: _Recording(intake=Intake(text="x", reply=("fix the config", []))))
    decision = await hook.before_user_inbound(SimpleNamespace(inbound_content="x"))
    assert decision.short_circuit_result == ("fix the config", [])
    hook = ParticipantHook("probe", lambda: _Recording(intake=Intake(text="same")))
    assert (await hook.before_user_inbound(SimpleNamespace(inbound_content="same"))).modified_content is None


@pytest.mark.asyncio
async def test_before_iteration_narrows_tools_and_carries_the_note():
    offered = [{"function": {"name": "a"}}, {"function": {"name": "b"}}]
    hook = ParticipantHook("probe", lambda: _Recording(tools=offered[:1], note="mind the budget"))
    decision = await hook.before_iteration(_ctx(iteration=2, tools=list(offered)))
    assert decision.modified_tools == offered[:1]
    assert decision.append_note == "mind the budget"
    hook = ParticipantHook("probe", lambda: _Recording(tools=None))
    assert (await hook.before_iteration(_ctx(iteration=2, tools=list(offered)))).modified_tools is None


@pytest.mark.asyncio
async def test_review_verdicts_become_the_decisions_the_loop_acts_on():
    resample = Resample(
        "too thin",
        inject=[{"role": "user", "content": "more"}],
        overrides={"reasoning_effort": "high"},
        note="gate: thin",
    )
    hook = ParticipantHook("probe", lambda: _Recording(verdict=resample, note="unused when rolled back"))
    decision = await hook.after_iteration(_ctx(iteration=3, response=SimpleNamespace(content="draft", tool_calls=None)))
    assert decision.rollback is True
    assert decision.rollback_inject == [{"role": "user", "content": "more"}]
    assert decision.rollback_overrides == {"reasoning_effort": "high"}
    assert decision.notes == ["too thin", "gate: thin"], "both the reason and the note reach the loop"
    assert decision.append_note is None
    hook = ParticipantHook("probe", lambda: _Recording(verdict=End("done here")))
    assert (await hook.before_execute_tools(_ctx(iteration=3))).short_circuit_result == "done here"
    hook = ParticipantHook("probe", lambda: _Recording(verdict=Accept(note="fine"), note="carry on"))
    decision = await hook.after_iteration(_ctx(iteration=3))
    assert decision.rollback is False and decision.append_note == "carry on" and decision.notes == ["fine"]


@pytest.mark.asyncio
async def test_the_step_is_read_off_the_context_and_off_a_bare_namespace():
    hook = ParticipantHook("probe", lambda: _Recording())
    ctx = _ctx(
        iteration=4,
        messages=[{"role": "user", "content": "q"}],
        turn_question="q",
        turn_base=1,
        metadata={"hook_rollbacks": 2, "mode": "max"},
    )
    await hook.after_iteration(ctx)
    step = hook.participant.steps[-1]
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
    step = hook.participant.steps[-1]
    assert (step.session_key, step.iteration, step.transcript, step.rollbacks, step.mode) == ("", 0, (), 0, None)


@pytest.mark.asyncio
async def test_salvage_and_the_outgoing_reply_land_where_the_loop_reads_them():
    hook = ParticipantHook("probe", lambda: _Recording(salvaged="rescued", outbound="\n\n--- 2 files changed"))
    assert (await hook.terminal_answerless(_ctx())).short_circuit_result == "rescued"
    decision = await hook.after_send(SimpleNamespace(outbound_content="done"))
    assert decision.modified_content == "done\n\n--- 2 files changed"
    assert hook.participant.archived == ["done"], "archive is asked after the reply, with the reply as sent"
    hook = ParticipantHook("probe", lambda: _Recording())
    assert (await hook.after_send(SimpleNamespace(outbound_content="done"))).modified_content is None


@pytest.mark.asyncio
async def test_a_bound_harness_decides_what_a_participants_verdict_does():
    """The seat asks the turn's modules, not the participant: with a harness bound
    whose Action lets every step stand, the participant's resample is not applied;
    with none bound, the default composition renders it as it always did."""
    from raven.agent.harness import bind_harness
    from raven.contracts.participant import Accept

    resample = Resample("thin", inject=[{"role": "user", "content": "more"}])
    hook = ParticipantHook("probe", lambda: _Recording(verdict=resample))
    ctx = _ctx(iteration=1, response=SimpleNamespace(content="draft", tool_calls=None))
    assert (await hook.after_iteration(ctx)).rollback is True, "unbound: the participant's own verdict"

    class Lenient:
        async def ask_review(self, step, participants):
            assert len(participants) == 1, "the seat hands the module this turn's participants"
            return Accept(note="action: overruled")

        async def ask_salvage(self, step, participants):
            return "the module's own salvage"

    class Louder:
        async def ask_advice(self, step, participants):
            assert len(participants) == 1
            return "planning: the module's own advice"

    class Rewriting:
        async def ask_intake(self, text, step, participants):
            assert len(participants) == 1
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
async def test_each_phase_names_itself_on_the_step():
    """``phase`` is the field a verb asked at two moments reads to tell them
    apart, and ``tools_ran`` is derived from it. Three of the five bundled
    plugins branch on ``tools_ran`` -- ppt on its close, oncall on its counter,
    research on which gate runs -- so a mistyped phase string at one of the six
    call sites would change what they do with nothing here to say so."""

    seen: list[tuple[str, bool]] = []

    class Watcher(AgentParticipant):
        async def intake(self, text, step):
            seen.append((step.phase, step.tools_ran))
            return None

        async def advise(self, step):
            seen.append((step.phase, step.tools_ran))
            return None

        async def review(self, step):
            seen.append((step.phase, step.tools_ran))
            return Accept()

        async def salvage(self, step):
            seen.append((step.phase, step.tools_ran))
            return None

        async def archive(self, step, reply):
            seen.append((step.phase, step.tools_ran))
            return None

    hook = ParticipantHook("probe", Watcher)
    ctx = _ctx(iteration=1, inbound_content="q")
    await hook.before_user_inbound(ctx)
    await hook.before_iteration(ctx)
    await hook.before_execute_tools(ctx)
    await hook.after_iteration(ctx)
    await hook.terminal_answerless(ctx)
    await hook.after_send(SimpleNamespace(outbound_content="done", metadata={}))

    assert seen == [
        ("user_inbound", False),
        ("iteration", False),
        ("execute_tools", False),
        ("after_iteration", True),
        ("after_iteration", True),
        ("answerless", False),
        ("sent", False),
    ], f"a phase is mislabelled: {seen}"


def test_the_registry_asks_judge_with_no_participants_at_all():
    """``judge`` has a role but no seat, and three docstrings now say so. What
    makes those sentences true is this: the one party that asks the verb is the
    tool registry, and it passes nothing, so the list the role composes holds
    only the dispatch's own rules.

    Asserted on the argument rather than on the refusal that comes back. A test
    that only checked "the Charter still refuses" would stay green after someone
    plumbed the seat's participants through -- which is the change these
    docstrings would silently outlive. Here that change makes ``handed``
    non-empty and turns this red, which is the point."""
    from raven.agent.harness import DefaultAction
    from raven.agent.subagent.charter import Charter, CheckRule, charter_scope
    from raven.agent.tools.registry import ToolRegistry

    handed: list[tuple] = []

    class Spy(DefaultAction):
        def ask_judge(self, name, params, prior, participants=()):
            handed.append(tuple(participants))
            return super().ask_judge(name, params, prior, participants)

    registry = ToolRegistry(verifier_provider=Spy)
    with charter_scope(Charter(checks=(CheckRule(tool="write_file", path_prefix="out/"),))):
        refusals = registry._verifier_refusals("write_file", {"path": "/etc/passwd"})

    assert handed == [()], f"the registry handed the role participants: {handed}"
    assert refusals, "the dispatch's own rules must still be asked"


@pytest.mark.asyncio
async def test_a_participant_cannot_write_through_the_step_it_is_shown():
    """ "Read-only" is the paper's word, so the rows are rows a write raises on.
    Without this the tuple froze the sequence and left every message inside it
    the loop's own object, open to edit from a seam that returns its answers."""

    wrote: list[str] = []

    class Mutator(AgentParticipant):
        async def advise(self, step):
            for field in (step.transcript, step.history, step.tools):
                if field:
                    try:
                        field[0]["role"] = "rewritten"
                        wrote.append("yes")
                    except TypeError:
                        pass
            # The one frozen field that is a mapping rather than a row of them.
            if step.mode_overlay is not None:
                try:
                    step.mode_overlay["depth"] = "rewritten"
                    wrote.append("yes")
                except TypeError:
                    pass
            return None

    ctx = _ctx(
        iteration=1,
        messages=[{"role": "user", "content": "q"}],
        session_history=[{"role": "assistant", "content": "a"}],
        tools=[{"name": "web_search"}],
        metadata={"mode_overlay": {"depth": "deep"}},
    )
    await ParticipantHook("probe", Mutator).before_iteration(ctx)

    assert wrote == [], "a participant wrote through the step"
    assert ctx.messages[0]["role"] == "user"
    assert ctx.session_history[0]["role"] == "assistant"


@pytest.mark.asyncio
async def test_two_seats_each_keep_one_addendum_in_the_system_message():
    """Two participants append to the same system message, and each takes its own
    text back out. Offsets could not do this: the first seat's strip shifts the
    second seat's text, so the second found nothing and spliced a second copy."""

    class Adds(AgentParticipant):
        def __init__(self, text):
            self.text = text

        async def system_addendum(self, step):
            return Intake(text=self.text)

    ctx = _ctx(iteration=1, messages=[{"role": "system", "content": "base"}])
    first, second = ParticipantHook("a", lambda: Adds("A")), ParticipantHook("b", lambda: Adds("B"))
    for _ in range(3):
        await first.before_iteration(ctx)
        await second.before_iteration(ctx)

    assert ctx.messages[0]["content"] == "base\n\nA\n\nB", "an addendum stacked or went missing"


@pytest.mark.asyncio
async def test_a_participant_may_contribute_a_tool_of_its_own_to_the_iteration():
    """The array is the iteration's, not a subset of what it was offered: the
    research flow contributes its escalation tool this way. Handing a name back
    is not granting it -- the registry still adjudicates every call."""

    class Contributes(AgentParticipant):
        async def select_tools(self, offered, step):
            return [*offered, {"name": "request_research"}]

    offered = [{"name": "web_search"}]
    decision = await ParticipantHook("probe", Contributes).before_iteration(_ctx(iteration=1, tools=list(offered)))
    assert [t["name"] for t in decision.modified_tools] == ["web_search", "request_research"]


@pytest.mark.asyncio
async def test_a_participant_that_only_implements_the_verbs_is_still_heard():
    """The verbs are the contract; inheriting the base class is a convenience.
    An object that implements them without it used to have its answer dropped
    by the composite, because the seat asked it for the trail unconditionally."""

    class Standalone:
        async def review(self, step):
            return End("closed by a participant that inherits nothing")

    decision = await ParticipantHook("probe", Standalone).before_execute_tools(_ctx(iteration=1))
    assert decision.short_circuit_result == "closed by a participant that inherits nothing"


@pytest.mark.asyncio
async def test_the_reason_a_resample_was_written_with_reaches_the_loops_notes():
    """``Resample`` takes its reason positionally, so an author writes it there
    first. Reading only the note dropped it."""

    class Sends(AgentParticipant):
        async def review(self, step):
            return Resample("the draft is thin")

    decision = await ParticipantHook("probe", Sends).after_iteration(
        _ctx(iteration=1, response=SimpleNamespace(content="draft", tool_calls=None))
    )
    assert decision.rollback is True and decision.notes == ["the draft is thin"]


@pytest.mark.asyncio
async def test_every_verb_the_roles_seat_goes_through_them():
    """Seven verbs have a module seat and the seat asks it. Without this the
    three that were applied straight off the participant had nowhere to compose two
    participants, nowhere to vet an answer, and nothing a replacement could
    decide -- and no test would have noticed."""
    from raven.agent.harness import bind_harness

    asked: list[str] = []

    class Memory:
        async def ask_intake(self, text, step, participants):
            asked.append("read_inbound")
            return None

        async def ask_system_addendum(self, step, participants):
            asked.append("compose_addendum")
            return None

        async def ask_archive(self, step, reply, participants):
            asked.append("file_record")
            return None

    class Capability:
        async def ask_select_tools(self, offered, step, participants):
            asked.append("offer")
            return None

    class Planning:
        async def ask_advice(self, step, participants):
            asked.append("guide")
            return None

    class Action:
        async def ask_review(self, step, participants):
            asked.append("judge_step")
            return Accept()

        async def ask_salvage(self, step, participants):
            asked.append("rescue")
            return None

    hook = ParticipantHook("probe", lambda: _Recording())
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


@pytest.mark.asyncio
async def test_an_unreadable_answer_is_silence_rather_than_a_refusal():
    """The third invariant: a participant that answers badly has said nothing.
    It matters most for the verbs that can halt a turn -- a generated function
    returning a stray string must not be able to stop the loop with it."""

    class Nonsense:
        async def review(self, step):
            return "not a verdict at all"

        async def intake(self, text, step):
            return ["neither", "is", "this"]

        async def salvage(self, step):
            return {"not": "a reply"}

    hook = ParticipantHook("probe", Nonsense)
    stands = await hook.before_execute_tools(_ctx(iteration=1))
    assert stands.rollback is False and stands.short_circuit_result is None

    inbound = await hook.before_user_inbound(_ctx(inbound_content="q"))
    assert inbound.modified_content is None and inbound.short_circuit_result is None

    answerless = await hook.terminal_answerless(_ctx())
    assert answerless.short_circuit_result is None, "a salvage that is not text is not a reply"


@pytest.mark.asyncio
async def test_a_participant_answers_with_a_mapping_it_could_have_built_itself():
    """The builders are a convenience; the contract is the mapping. A
    participant that writes the keys by hand is answering the same thing, which
    is what lets a generated function answer at all."""

    class ByHand:
        async def review(self, step):
            return {
                "verdict": "resample",
                "reason": "written by hand",
                "inject": [{"role": "user", "content": "again"}],
            }

    decision = await ParticipantHook("probe", ByHand).before_execute_tools(_ctx(iteration=1))
    assert decision.rollback is True
    assert decision.rollback_inject == [{"role": "user", "content": "again"}]
    assert decision.notes == ["written by hand"]


@pytest.mark.asyncio
async def test_composite_asks_each_role_once_with_the_turns_complete_roster():
    """Two seats are one role call, in registration order, for every delegated verb."""
    from raven.agent.harness import bind_harness
    from raven.agent.hook.composite import CompositeHook

    class Named(_Recording):
        def __init__(self, name):
            super().__init__()
            self.name = name

    calls: list[tuple[str, tuple[str, ...]]] = []

    def seen(verb, participants):
        calls.append((verb, tuple(p.name for p in participants)))

    class Memory:
        async def ask_intake(self, text, step, participants):
            seen("intake", participants)
            return None

        async def ask_system_addendum(self, step, participants):
            seen("system_addendum", participants)
            return None

        async def ask_archive(self, step, reply, participants):
            seen("archive", participants)
            return None

    class Planning:
        async def ask_advice(self, step, participants):
            seen("advise", participants)
            return None

    class Capability:
        async def ask_select_tools(self, offered, step, participants):
            seen("select_tools", participants)
            return None

    class Action:
        async def ask_review(self, step, participants):
            seen("review", participants)
            return Accept()

        async def ask_salvage(self, step, participants):
            seen("salvage", participants)
            return None

    chain = CompositeHook([ParticipantHook("a", lambda: Named("a")), ParticipantHook("b", lambda: Named("b"))])
    harness = SimpleNamespace(memory=Memory(), planning=Planning(), capability=Capability(), action=Action())
    ctx = _ctx(
        iteration=1,
        inbound_content="q",
        outbound_content="done",
        tools=[{"name": "read_file"}],
        metadata={},
    )
    with bind_harness(harness):
        await chain.before_user_inbound(ctx)
        await chain.before_iteration(ctx)
        await chain.before_execute_tools(ctx)
        await chain.after_iteration(ctx)
        await chain.terminal_answerless(ctx)
        await chain.after_send(ctx)

    assert calls == [
        ("intake", ("a", "b")),
        ("select_tools", ("a", "b")),
        ("advise", ("a", "b")),
        ("system_addendum", ("a", "b")),
        ("review", ("a", "b")),
        ("advise", ("a", "b")),
        ("review", ("a", "b")),
        ("salvage", ("a", "b")),
        ("archive", ("a", "b")),
    ]


@pytest.mark.asyncio
async def test_roster_waits_for_a_nested_products_setup_axes():
    """A product may hide its seat behind setup axes; the roster runs after them."""
    from raven.agent.harness import bind_harness
    from raven.agent.hook.composite import CompositeHook
    from raven.contracts.loop_hooks import AgentHook, HookDecision

    events: list[str] = []

    class Setup(AgentHook):
        @property
        def name(self):
            return "setup"

        async def before_iteration(self, ctx):
            events.append("setup")
            return HookDecision()

    class Named(_Recording):
        def __init__(self, name):
            super().__init__()
            self.name = name

    class Planning:
        async def ask_advice(self, step, participants):
            events.append("roster:" + ",".join(p.name for p in participants))
            return None

    class Memory:
        async def ask_system_addendum(self, step, participants):
            return None

    class Capability:
        async def ask_select_tools(self, offered, step, participants):
            return None

    nested = CompositeHook([Setup(), ParticipantHook("b", lambda: Named("b"))])
    chain = CompositeHook([ParticipantHook("a", lambda: Named("a")), nested])
    harness = SimpleNamespace(memory=Memory(), planning=Planning(), capability=Capability())
    ctx = _ctx(iteration=1)
    with bind_harness(harness):
        await chain.before_iteration(ctx)
        await chain.before_iteration(ctx)

    assert events == ["setup", "roster:a,b", "setup", "roster:a,b"]


@pytest.mark.asyncio
async def test_unbound_composition_asks_the_complete_roster_and_drains_every_trail():
    from raven.agent.hook.composite import CompositeHook

    events: list[str] = []

    class Named(AgentParticipant):
        def __init__(self, name):
            self.name = name

        async def intake(self, text, step):
            events.append(f"intake:{self.name}:{text}")
            self.note(f"trail:{self.name}")
            return Intake(text + self.name)

        async def review(self, step):
            events.append(f"review:{self.name}")
            return End("closed") if self.name == "b" else Accept()

    chain = CompositeHook(
        [
            ParticipantHook("a", lambda: Named("a")),
            ParticipantHook("b", lambda: Named("b")),
        ]
    )
    ctx = _ctx(inbound_content="q")

    inbound = await chain.before_user_inbound(ctx)
    review = await chain.before_execute_tools(ctx)

    assert inbound.modified_content == "qab"
    assert inbound.notes == ["trail:a", "trail:b"]
    assert review.short_circuit_result == "closed"
    assert events == ["intake:a:q", "intake:b:qa", "review:a", "review:b"]


@pytest.mark.asyncio
async def test_a_factory_failure_omits_only_that_seat_from_the_roster():
    from raven.agent.harness import bind_harness
    from raven.agent.hook.composite import CompositeHook

    class Named(_Recording):
        def __init__(self, name):
            super().__init__()
            self.name = name

    def broken():
        raise RuntimeError("factory failed")

    seen: list[tuple[str, ...]] = []

    class Action:
        async def ask_review(self, step, participants):
            seen.append(tuple(participant.name for participant in participants))
            return Accept()

    chain = CompositeHook(
        [
            ParticipantHook("broken", broken),
            ParticipantHook("a", lambda: Named("a")),
            ParticipantHook("b", lambda: Named("b")),
        ]
    )
    ctx = _ctx(iteration=1)
    with bind_harness(SimpleNamespace(action=Action())):
        await chain.before_execute_tools(ctx)

    assert seen == [("a", "b")]
    assert ParticipantHook._ROSTER_KEY not in ctx.metadata


@pytest.mark.asyncio
async def test_roster_discovers_a_participant_behind_product_axes():
    from raven.agent.harness import bind_harness
    from raven.agent.hook.composite import CompositeHook
    from raven.contracts.loop_hooks import AgentHook, HookDecision

    events: list[str] = []

    class Setup(AgentHook):
        async def before_iteration(self, ctx):
            events.append("setup")
            return HookDecision()

    class Named(_Recording):
        def __init__(self, name):
            super().__init__()
            self.name = name

    class Product(AgentHook):
        def __init__(self):
            self.axes = (Setup(), ParticipantHook("b", lambda: Named("b")))

        async def before_iteration(self, ctx):
            for axis in self.axes:
                decision = await axis.before_iteration(ctx)
                if decision.short_circuit_result is not None or decision.rollback:
                    return decision
            return HookDecision()

    class Planning:
        async def ask_advice(self, step, participants):
            events.append("roster:" + ",".join(participant.name for participant in participants))
            return None

    class Memory:
        async def ask_system_addendum(self, step, participants):
            return None

    class Capability:
        async def ask_select_tools(self, offered, step, participants):
            return None

    chain = CompositeHook([ParticipantHook("a", lambda: Named("a")), Product()])
    harness = SimpleNamespace(memory=Memory(), planning=Planning(), capability=Capability())
    with bind_harness(harness):
        await chain.before_iteration(_ctx(iteration=1))

    assert events == ["setup", "roster:a,b"]


@pytest.mark.asyncio
async def test_roster_discovery_and_cleanup_survive_bad_neighbor_hooks():
    from raven.agent.harness import bind_harness
    from raven.agent.hook.composite import CompositeHook
    from raven.contracts.loop_hooks import AgentHook

    class Named(_Recording):
        name = "a"

    class BadAxes(AgentHook):
        @property
        def axes(self):
            raise RuntimeError("axes failed")

    class BadDecision(AgentHook):
        async def before_execute_tools(self, ctx):
            return object()

    class Action:
        async def ask_review(self, step, participants):
            return Accept()

    ctx = _ctx(iteration=1)
    harness = SimpleNamespace(action=Action())
    with bind_harness(harness):
        await CompositeHook([BadAxes(), ParticipantHook("a", Named)]).before_execute_tools(ctx)
        with pytest.raises(AttributeError):
            await CompositeHook([ParticipantHook("a", Named), BadDecision()]).before_execute_tools(ctx)

    assert ParticipantHook._ROSTER_KEY not in ctx.metadata


@pytest.mark.asyncio
async def test_an_interleaved_hook_runs_before_the_roster_short_circuits():
    from raven.agent.hook.composite import CompositeHook
    from raven.contracts.loop_hooks import AgentHook, HookDecision

    events: list[str] = []

    class Named(AgentParticipant):
        def __init__(self, name):
            self.name = name

        async def review(self, step):
            events.append("review:" + self.name)
            return End("closed") if self.name == "a" else Accept()

    class Middle(AgentHook):
        async def before_execute_tools(self, ctx):
            events.append("middle")
            return HookDecision()

    chain = CompositeHook(
        [
            ParticipantHook("a", lambda: Named("a")),
            Middle(),
            ParticipantHook("b", lambda: Named("b")),
        ]
    )
    decision = await chain.before_execute_tools(_ctx(iteration=1))

    assert decision.short_circuit_result == "closed"
    assert events == ["middle", "review:a"]
