"""The four harness strategy roles, and the identity the default set promises.

Every assertion here is about the default set changing nothing: the roles were
carved out of the loop, so the guard that matters is that a turn bound to them
sees what it saw when the same code sat inline. The behaviour-preserving claim
is what makes the seam safe to widen later.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from typing import Any

import pytest

from raven.agent.harness import default_harness_modules
from raven.agent.harness.action import DefaultAction
from raven.agent.harness.action import bind as bind_action
from raven.agent.harness.capability import DefaultCapability
from raven.agent.harness.memory import DefaultMemory
from raven.agent.harness.memory import bind as bind_memory
from raven.agent.harness.planning import DefaultPlanning
from raven.agent.window import shrink as shrink_module
from raven.config.schema import CompactionConfig
from raven.context_engine.runtime_context import (
    MCP_NOTICE_HEADER,
    RUNTIME_CONTEXT_KEY,
    mcp_notice,
    told_notices,
    with_runtime_context,
)
from raven.context_engine.segments.render import RUNTIME_CONTEXT_TAG
from raven.contracts.harness import (
    ActionModule,
    ActionRequest,
    CapabilityModule,
    CapabilityRequest,
    HarnessModules,
    MemoryModule,
    PlanningModule,
    PlanningRequest,
    ShrinkResult,
    WindowPressure,
    WindowState,
)


class _Engine:
    """The context engine's turn-facing surface, and nothing else."""

    def __init__(self, owns_compaction: bool = True) -> None:
        self.owns_compaction = owns_compaction
        self.assembled: list[str] = []
        self.after: list[str] = []

    async def assemble(self, session_key, session_messages, budget, *, turn):
        self.assembled.append(session_key)
        return {"messages": list(session_messages)}

    async def after_turn(self, session_key, outcome) -> None:
        self.after.append(session_key)


class _Registry:
    def __init__(self, defs) -> None:
        self._defs = defs
        self.calls = 0

    def get_definitions(self):
        self.calls += 1
        return self._defs


def _modules(engine=None, registry=None, **over):
    """The default set, wired the way the loop wires it."""
    engine = engine or _Engine()
    registry = registry or _Registry([])
    kwargs = dict(
        provider=lambda: SimpleNamespace(generation=None),
        model=lambda: "some/model",
        context_window_tokens=lambda: 200_000,
        system_prompt=lambda skills: "SYSTEM",
        compaction=lambda: SimpleNamespace(enabled=False),
        output_ceiling=lambda model=None: 4096,
    )
    kwargs.update(over)
    return default_harness_modules(engine, lambda: registry, **kwargs)


def test_the_defaults_satisfy_the_roles_they_are_bound_to():
    modules = _modules()
    assert isinstance(modules, HarnessModules)
    assert isinstance(modules.memory, MemoryModule)
    assert isinstance(modules.planning, PlanningModule)
    assert isinstance(modules.capability, CapabilityModule)
    assert isinstance(modules.action, ActionModule)


def test_memory_keeps_this_generations_engine_as_the_thing_that_assembles():
    """The role is a seat, not a replacement: the engine holds the Curator's
    state and each builder's provider binding, so it stays the assembler and
    the module forwards rather than reimplementing."""
    engine = _Engine()
    asyncio.run(_modules(engine=engine).memory.assemble("s1", [], None, turn=None))
    assert engine.assembled == ["s1"]


def test_binding_something_missing_a_member_fails_at_assembly():
    """Named at construction rather than as an AttributeError inside a turn."""

    class _Missing:
        owns_compaction = False

        async def assemble(self, *a, **k):  # no candidate_messages / token_budget
            return None

    with pytest.raises(TypeError, match="Memory role"):
        bind_memory(_Missing())


# --------------------------------------------------------------------------- #
# Memory: the two turn decisions that moved out of the shell                    #
# --------------------------------------------------------------------------- #


def test_an_archiving_engine_is_offered_the_whole_append_only_log():
    """``owns_compaction`` means the engine decides what to evict, so it has to
    see everything -- handing it the trimmed view would evict twice."""
    memory = _modules(engine=_Engine(owns_compaction=True)).memory
    session = SimpleNamespace(messages=[{"role": "user", "content": "a"}], get_history=lambda **k: [])
    assert memory.candidate_messages(session) == [{"role": "user", "content": "a"}]


def test_a_non_archiving_engine_is_offered_the_post_consolidation_slice():
    memory = _modules(engine=_Engine(owns_compaction=False)).memory
    slice_ = [{"role": "user", "content": "kept"}]
    session = SimpleNamespace(messages=[{"role": "user", "content": "all"}], get_history=lambda **k: slice_)
    assert memory.candidate_messages(session) == slice_


def test_the_candidate_view_is_a_copy_the_engine_may_not_mutate_back():
    memory = _modules(engine=_Engine(owns_compaction=True)).memory
    messages = [{"role": "user", "content": "a"}]
    session = SimpleNamespace(messages=messages, get_history=lambda **k: [])
    assert memory.candidate_messages(session) is not messages


def test_the_budget_reserves_the_models_whole_output_ceiling():
    """Not a share of the window: a request no longer names a ceiling, so the
    one that applies is the model's own, and reserving less hands out a prompt
    the reply cannot coexist with."""
    memory = _modules(context_window_tokens=lambda: 200_000).memory
    budget = memory.token_budget()
    assert budget.context_length == 200_000
    assert budget.reserved_output > 0
    assert budget.available_history == (
        200_000 - budget.reserved_output - budget.reserved_tools - budget.reserved_system
    )


def test_the_budget_never_reserves_more_output_than_the_window_holds():
    memory = _modules(context_window_tokens=lambda: 8_000).memory
    assert memory.token_budget().reserved_output <= 8_000


def test_the_budget_follows_a_window_a_model_switch_moved():
    """Every input is read through a callable because a live switch rebuilds
    the provider and the window; a value captured at assembly would size the
    prompt against the retired model."""
    window = {"tokens": 200_000}
    memory = _modules(context_window_tokens=lambda: window["tokens"]).memory
    assert memory.token_budget().context_length == 200_000
    window["tokens"] = 32_000
    assert memory.token_budget().context_length == 32_000


def test_the_budget_charges_for_the_tools_this_turn_offers():
    lean = _modules(registry=_Registry([])).memory.token_budget()
    heavy = _modules(registry=_Registry([{"name": f"t{i}", "description": "x" * 200} for i in range(20)])).memory
    assert heavy.token_budget().reserved_tools > lean.reserved_tools


def test_the_budget_charges_for_the_skills_this_turn_injected():
    """``selected_skills`` crosses three layers to reach the identity builder,
    and a drop anywhere on the way is silent: the budget still returns, just
    sized against a system prompt the turn will not send."""
    seen: list = []

    def system_prompt(skills):
        seen.append(skills)
        return "SYSTEM" if not skills else "SYSTEM" + "x" * 400

    memory = _modules(system_prompt=system_prompt).memory
    bare = memory.token_budget()
    withskills = memory.token_budget(["skill-a"])
    assert seen == [None, ["skill-a"]]
    assert withskills.reserved_system > bare.reserved_system
    assert withskills.available_history < bare.available_history


def test_memory_after_turn_reaches_the_engine():
    engine = _Engine()
    asyncio.run(_modules(engine=engine).memory.after_turn("s1", {"final_content": "done"}))
    assert engine.after == ["s1"]


# --------------------------------------------------------------------------- #
# Planning / Capability                                                         #
# --------------------------------------------------------------------------- #


def test_planning_passes_the_turns_messages_through_untouched():
    """The identity that keeps a default turn byte-identical: same list object,
    so nothing downstream can tell the seam is there."""
    messages = [{"role": "user", "content": "hi"}]
    result = asyncio.run(DefaultPlanning().prepare(PlanningRequest(task="hi", session_key="s", messages=messages)))
    assert result.messages is messages


def test_capability_reports_exactly_what_the_registry_offers():
    defs = [{"name": "read_file"}, {"name": "exec"}]
    registry = _Registry(defs)
    selection = asyncio.run(DefaultCapability(lambda: registry).select(CapabilityRequest(messages=[], iteration=1)))
    assert selection.tools == defs


def test_capability_asks_the_registry_again_every_iteration():
    """Not captured once: turning a tool off mid-turn has to reach the next
    iteration's array, which a cached list would swallow."""
    registry = _Registry([])
    capability = DefaultCapability(lambda: registry)
    for iteration in (1, 2, 3):
        asyncio.run(capability.select(CapabilityRequest(messages=[], iteration=iteration)))
    assert registry.calls == 3


def test_capability_follows_a_registry_the_loop_rebuilt():
    """A hot config apply builds a new registry; a captured instance would keep
    answering for the retired one, which is why the provider is a callable."""
    current = _Registry([{"name": "old"}])
    capability = DefaultCapability(lambda: current)
    first = asyncio.run(capability.select(CapabilityRequest(messages=[], iteration=1)))
    current = _Registry([{"name": "new"}])
    second = asyncio.run(capability.select(CapabilityRequest(messages=[], iteration=2)))
    assert [t["name"] for t in first.tools] == ["old"]
    assert [t["name"] for t in second.tools] == ["new"]


# --------------------------------------------------------------------------- #
# Action                                                                        #
# --------------------------------------------------------------------------- #


def _action_request(**over):
    seen: dict = {}

    class _Provider:
        async def chat_with_retry(self, **kwargs):
            seen["retry"] = kwargs
            return "retried"

    async def _stream(**kwargs):
        seen["stream"] = kwargs
        return "streamed"

    base = dict(
        provider=_Provider(),
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "exec"}],
        model="some/model",
        fallback_models=["other/model"],
        stream_call=_stream,
        generation_overrides={"temperature": 0.2},
    )
    base.update(over)
    return ActionRequest(**base), seen


def test_action_takes_the_retry_ladder_when_no_sink_is_attached():
    request, seen = _action_request()
    assert asyncio.run(DefaultAction().decide(request)) == "retried"
    assert seen["retry"]["fallback_models"] == ["other/model"]
    assert seen["retry"]["temperature"] == 0.2
    assert "stream" not in seen


def test_action_streams_when_a_token_sink_is_attached():
    request, seen = _action_request(on_token_delta=lambda _t: None)
    assert asyncio.run(DefaultAction().decide(request)) == "streamed"
    assert seen["stream"]["temperature"] == 0.2
    assert "retry" not in seen


def test_action_streams_for_a_reasoning_sink_alone():
    """The loop's own condition was an ``or``: a turn that renders reasoning and
    no tokens still streams. Pinned because reading it as ``and`` sends that
    turn down the retry path and its reasoning never reaches the reader."""
    request, seen = _action_request(on_reasoning_delta=lambda _t: None)
    assert asyncio.run(DefaultAction().decide(request)) == "streamed"
    assert "retry" not in seen


def test_action_hands_the_stream_the_sink_it_was_given():
    """The shell splices its continuation gate into ``on_token_delta`` before
    building the request, so the module must forward the field rather than
    reach for a sink of its own."""

    def gate(_t):
        return None

    request, seen = _action_request(on_token_delta=gate)
    asyncio.run(DefaultAction().decide(request))
    assert seen["stream"]["on_token_delta"] is gate


def test_action_does_not_reach_the_provider_on_the_streaming_path():
    """The stream call is the loop's own: it fans deltas to the turn's sinks.
    A module that called the provider directly would drop them."""
    request, seen = _action_request(on_token_delta=lambda _t: None)
    asyncio.run(DefaultAction().decide(request))
    assert list(seen) == ["stream"]


def test_the_module_set_is_frozen_once_assembled():
    """A generation's strategy set cannot move mid-turn: the tool array is the
    prompt-cache prefix, so two model calls of one turn must not run on two
    different sets."""
    modules = _modules()
    with pytest.raises(FrozenInstanceError):
        modules.action = DefaultAction()


def test_a_memory_built_by_hand_is_the_same_role_the_loop_binds():
    """The constructor is the seam a replacement uses, so it has to stand on
    its own rather than only through the default assembler."""
    engine = _Engine()
    memory = DefaultMemory(
        engine,
        provider=lambda: SimpleNamespace(generation=None),
        model=lambda: "m",
        context_window_tokens=lambda: 1_000,
        tool_definitions=lambda: [],
        system_prompt=lambda skills: "S",
        compaction=lambda: SimpleNamespace(enabled=False),
        output_ceiling=lambda model=None: 4096,
    )
    assert isinstance(bind_memory(memory), MemoryModule)


def _window_memory(*, compaction=None):
    return DefaultMemory(
        _Engine(),
        provider=lambda: SimpleNamespace(generation=None),
        model=lambda: "m",
        context_window_tokens=lambda: 1_000,
        tool_definitions=lambda: [],
        system_prompt=lambda skills: "S",
        compaction=lambda: compaction or SimpleNamespace(enabled=False),
        output_ceiling=lambda model=None: 256,
    )


def test_the_window_role_answers_every_pressure_the_loop_asks_under():
    """The five recoveries the loop used to run inline are one method now, so
    each one is asked for by name and answers with the list to go on with."""
    memory = _window_memory()
    asked = []

    for pressure in WindowPressure:
        messages = [{"role": "user", "content": "q"}]
        state = WindowState(image_window=2)
        result = asyncio.run(memory.shrink(list(messages), pressure=pressure, state=state, model="m"))
        assert isinstance(result, ShrinkResult), pressure
        assert result.messages == messages, f"{pressure} left a transcript with nothing to give up alone"
        asked.append(pressure)

    assert asked == list(WindowPressure), "every pressure the paper names is answered"
    with pytest.raises(ValueError):
        asyncio.run(memory.shrink([], pressure="sideways", state=WindowState(image_window=1), model=None))


def test_the_standing_pass_withdraws_the_pictures_outside_the_window():
    """The standing pass is the one that touches no retry budget: it runs every
    iteration and only decides which pictures the window still holds."""
    memory = _window_memory()
    picture = {
        "role": "tool",
        "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 4000}},
        ],
    }
    state = WindowState(image_window=1, image_budget=10)
    shown = [dict(picture, content=list(picture["content"])) for _ in range(3)]
    result = asyncio.run(memory.shrink(shown, pressure=WindowPressure.STANDING, state=state, model="m"))
    assert result.changed, "the pictures outside the window are withdrawn"
    assert "image_url" not in str(result.messages[0]), "the withdrawal leaves a note, not the bytes"
    assert result.messages is shown, "the standing pass hands back the list it was given"


def test_an_action_role_missing_judge_is_refused_at_assembly():
    """The registry answers with no opinion when a role raises, so a role that
    cannot judge would let every call a playbook refuses through. Naming it
    here is the difference between a loud assembly and a silent turn."""

    class DecideOnly:
        async def decide(self, request): ...

    with pytest.raises(TypeError, match="decide, ask_judge"):
        bind_action(DecideOnly())
    assert bind_action(DefaultAction()) is not None


def test_a_refusal_closes_the_window_a_notch_and_spends_the_turns_budget():
    """The pressures that carry a retry budget advance the state the shell holds,
    which is the only thing that stops a refusal retrying forever. Asserted on
    the counters, not on the transcript: delete the increments and the moves
    still shrink, but the loop never stops paying for them."""
    memory = _window_memory()
    picture = {
        "role": "tool",
        "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 4000}},
        ],
    }

    state = WindowState(image_window=2, image_budget=10)
    shown = [dict(picture, content=list(picture["content"])) for _ in range(3)]
    first = asyncio.run(memory.shrink(shown, pressure=WindowPressure.IMAGES_TOO_LARGE, state=state, model="m"))
    assert first.changed and state.image_strip_retries == 1, "the first refusal is paid for"
    assert state.image_budget is None and state.image_window == 1, "the window closed a notch"

    state.image_strip_retries = shrink_module.MAX_IMAGE_STRIP_RETRIES
    spent = asyncio.run(
        memory.shrink(
            [dict(picture, content=list(picture["content"]))],
            pressure=WindowPressure.IMAGES_TOO_LARGE,
            state=state,
            model="m",
        )
    )
    assert not spent.changed, "a turn out of budget stops retrying"

    demote_state = WindowState(image_window=2)
    demoted = asyncio.run(
        memory.shrink(
            [dict(picture, content=list(picture["content"]))],
            pressure=WindowPressure.TOOL_IMAGES_REFUSED,
            state=demote_state,
            model="m",
        )
    )
    assert demoted.changed and demote_state.image_demote_retries == 1
    again = asyncio.run(
        memory.shrink(
            [dict(picture, content=list(picture["content"]))],
            pressure=WindowPressure.TOOL_IMAGES_REFUSED,
            state=demote_state,
            model="m",
        )
    )
    assert not again.changed, "demotion is once per turn, and the counter is what says so"


def test_the_engine_is_handed_a_turn_memory_briefed_from_the_charter():
    """The join, not the two halves. A charter is bound, a real assemble runs,
    and what the engine receives carries the brief -- which it can only do if
    Memory filled the turn before handing it over. Without this, deleting the
    ``_briefed`` call leaves every other test in this file green."""
    from raven.agent.subagent.charter import Charter, charter_scope
    from raven.contracts.assembled import TokenBudget
    from raven.contracts.context import TurnContext

    seen: dict[str, object] = {}

    class _Recording:
        owns_compaction = False

        async def assemble(self, session_key, session_messages, budget, *, turn):
            seen["brief"] = turn.task_brief
            seen["done_when"] = turn.task_done_when
            return object()

        async def after_turn(self, session_key, outcome): ...

    memory = DefaultMemory(
        _Recording(),
        provider=lambda: SimpleNamespace(generation=None),
        model=lambda: "m",
        context_window_tokens=lambda: 1_000,
        tool_definitions=lambda: [],
        system_prompt=lambda skills: "S",
        compaction=lambda: SimpleNamespace(enabled=False),
        output_ceiling=lambda model=None: 256,
    )
    budget = TokenBudget(
        context_length=8000, reserved_output=1000, reserved_tools=0, reserved_system=0, available_history=7000
    )

    with charter_scope(Charter(prompt="Only look at A.", stop_when="both tables land")):
        asyncio.run(memory.assemble("s1", [], budget, turn=TurnContext(current_message="hi")))

    assert seen == {"brief": "Only look at A.", "done_when": "both tables land"}, (
        "the engine was handed a turn Memory never briefed"
    )


class _BriefProvider:
    """A provider whose every chat call answers one fixed handoff brief, the way
    a paid-for head summary comes back."""

    def __init__(self, brief: str = "the head, as a brief.") -> None:
        self.brief = brief
        self.calls = 0

    async def chat(self, *, messages, tools, model, max_tokens, reasoning_effort):
        self.calls += 1
        return SimpleNamespace(content=self.brief, finish_reason="stop")


def _compacting_memory(provider: Any) -> DefaultMemory:
    """A Memory over the real shrink moves, sized so a stub summary is worth
    paying for: the window sits well above the head, and the tail budget holds
    the question alone, so the split lands inside the older exchange."""
    return DefaultMemory(
        _Engine(),
        provider=lambda: provider,
        model=lambda: "m",
        context_window_tokens=lambda: 20_000,
        tool_definitions=lambda: [],
        system_prompt=lambda skills: "S",
        compaction=lambda: CompactionConfig(enabled=True, prune=False, preserve_recent_tokens=2_500),
        output_ceiling=lambda model=None: 4_000,
    )


def _question(lines: str | None = None, words: Any = "and now?") -> dict[str, Any]:
    """The turn's own question envelope: a runtime-context block that left the
    plugin notes out because an older block in the history had told them."""
    lines = lines or f"{RUNTIME_CONTEXT_TAG}\nChat ID: c1"
    return {"role": "user", "content": with_runtime_context(lines, words), RUNTIME_CONTEXT_KEY: lines}


def _pressured_turn(question: dict[str, Any], *older_blocks: str) -> list[dict[str, Any]]:
    """The turn's list as the model sees it: the task statement (protected from
    summaries), one older user exchange per given block, bulk that makes the
    head worth a summary call, and the question."""
    messages: list[dict[str, Any]] = [{"role": "user", "content": "list the repos"}]
    for index, block in enumerate(older_blocks):
        messages.append({"role": "user", "content": with_runtime_context(block, f"older work {index}")})
        messages.append({"role": "assistant", "content": f"filler {index} " * 2_200})
    messages.append(question)
    return messages


_AUTH = mcp_notice("github", "installed, awaiting authorization.")
_SLIDES_CONNECTED = mcp_notice("slides", "connected, offering 2 tool(s).")
_QUOTED_NAME = "back\\slash'"
_QUOTED = mcp_notice(_QUOTED_NAME, "connected, offering 1 tool(s).")


def test_a_proactive_summary_tells_again_the_note_it_took_away():
    """The head summary can replace the only block that still carried a plugin
    note. The question left the note out because that block had told it, so the
    summarized turn has to say it again or the model spends the rest of the
    turn without it. Driven through the public shrink dispatch, with the real
    split and a paid-for stub brief."""
    provider = _BriefProvider()
    memory = _compacting_memory(provider)
    block = f"{RUNTIME_CONTEXT_TAG}\n{MCP_NOTICE_HEADER}\n{_AUTH}"
    messages = _pressured_turn(_question(), block)
    before = copy.deepcopy(messages)
    state = WindowState(image_window=1, session_key="s1", last_context_used=17_000)

    result = asyncio.run(memory.shrink(messages, pressure=WindowPressure.PROACTIVE, state=state, model="m"))

    assert provider.calls == 1, "the fixture owes exactly one summary call"
    assert result.changed and state.compress_retries == 1 and state.last_context_used == 0
    assert not any("older work 0" in str(m.get("content")) for m in result.messages), (
        "fixture must have the summary replace the note's carrier, not tail or protect it"
    )
    assert messages == before, "the list handed in is the turn's own record, not the summary's to edit"
    assert told_notices(result.messages) == {"github": _AUTH}
    [repaired] = [m for m in result.messages if m.get(RUNTIME_CONTEXT_KEY)]
    assert repaired["content"].split("\n\n", 1)[0] == repaired[RUNTIME_CONTEXT_KEY]
    assert repaired[RUNTIME_CONTEXT_KEY].splitlines().count(MCP_NOTICE_HEADER) == 1
    assert repaired[RUNTIME_CONTEXT_KEY].endswith(_AUTH)


def test_an_overflow_head_summary_tells_again_the_note_it_took_away():
    """The overflow path's last resort is the same head summary, reached once
    nothing is left to elide, so it passes the same common wrap and loses no
    notes either -- and its own budget bookkeeping moves exactly one step."""
    provider = _BriefProvider()
    memory = _compacting_memory(provider)
    block = f"{RUNTIME_CONTEXT_TAG}\n{MCP_NOTICE_HEADER}\n{_AUTH}"
    messages = _pressured_turn(_question(), block)
    state = WindowState(image_window=1, session_key="s1")

    result = asyncio.run(memory.shrink(messages, pressure=WindowPressure.OVERFLOW, state=state, model="m"))

    assert provider.calls == 1, "nothing is elidable in this fixture, so the summary is the answer"
    assert result.changed and state.compress_retries == 1 and state.reactive_summary_tried
    assert told_notices(result.messages) == {"github": _AUTH}


_CLEARED = "the earlier note about it no longer applies; this turn's tool definitions show what it offers."


def test_the_newest_note_state_is_what_comes_back():
    """What the repair puts back is the state the last block left standing, not
    every line the head ever carried: a note a later block cleared stays gone,
    a note a later block replaced comes back replaced, and a name with quotes
    and a backslash keeps its own line."""
    provider = _BriefProvider()
    memory = _compacting_memory(provider)
    slides_connecting = mcp_notice("slides", "still connecting.")
    older = f"{RUNTIME_CONTEXT_TAG}\n{MCP_NOTICE_HEADER}\n{_AUTH}\n{slides_connecting}"
    newer = (
        f"{RUNTIME_CONTEXT_TAG}\n{MCP_NOTICE_HEADER}\n{mcp_notice('github', _CLEARED)}\n{_SLIDES_CONNECTED}\n{_QUOTED}"
    )
    messages = _pressured_turn(_question(), older, newer)
    state = WindowState(image_window=1, session_key="s1", last_context_used=17_000)

    result = asyncio.run(memory.shrink(messages, pressure=WindowPressure.PROACTIVE, state=state, model="m"))

    assert provider.calls == 1 and result.changed
    assert told_notices(result.messages) == {_QUOTED_NAME: _QUOTED, "slides": _SLIDES_CONNECTED}
    assert not any(_AUTH in str(m.get("content")) for m in result.messages), "a cleared note must not come back"


def test_a_note_the_question_already_carries_is_not_told_twice():
    """When the newest state rides in the question's own block -- the shape a
    turn whose plugins just changed sends -- the summary takes only stale
    copies, and the question comes back exactly as it went in."""
    provider = _BriefProvider()
    memory = _compacting_memory(provider)
    block = f"{RUNTIME_CONTEXT_TAG}\n{MCP_NOTICE_HEADER}\n{_AUTH}"
    question = _question(block)
    messages = _pressured_turn(question, block)
    state = WindowState(image_window=1, session_key="s1", last_context_used=17_000)

    result = asyncio.run(memory.shrink(messages, pressure=WindowPressure.PROACTIVE, state=state, model="m"))

    assert provider.calls == 1 and result.changed
    assert result.messages[-1] is question, "nothing was lost, so nothing was rewritten"
    assert told_notices(result.messages) == {"github": _AUTH}
    assert question["content"].split("\n\n", 1)[0].splitlines().count(MCP_NOTICE_HEADER) == 1


def test_consecutive_summaries_do_not_stack_the_notes():
    """A turn can cross the trigger twice: the second summary runs over a list
    whose question already carries the retold note, and must not add it (or the
    header) a second time."""
    provider = _BriefProvider()
    memory = _compacting_memory(provider)
    block = f"{RUNTIME_CONTEXT_TAG}\n{MCP_NOTICE_HEADER}\n{_AUTH}"
    state = WindowState(image_window=1, session_key="s1", last_context_used=17_000)

    first = asyncio.run(
        memory.shrink(_pressured_turn(_question(), block), pressure=WindowPressure.PROACTIVE, state=state, model="m")
    )
    assert first.changed and told_notices(first.messages) == {"github": _AUTH}

    call = {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function"}]}
    result_msg = {"role": "tool", "tool_call_id": "c1", "content": "done"}
    question = first.messages.pop()
    state.last_context_used = 17_000
    second = asyncio.run(
        memory.shrink(
            [*first.messages, {"role": "assistant", "content": "filler x " * 2_200}, question, call, result_msg],
            pressure=WindowPressure.PROACTIVE,
            state=state,
            model="m",
        )
    )

    assert provider.calls == 2 and second.changed and state.compress_retries == 2
    [repaired] = [m for m in second.messages if m.get(RUNTIME_CONTEXT_KEY)]
    block_lines = repaired["content"].split("\n\n", 1)[0].splitlines()
    assert block_lines.count(MCP_NOTICE_HEADER) == 1 and block_lines.count(_AUTH) == 1
    assert told_notices(second.messages) == {"github": _AUTH}


def test_a_multimodal_question_gets_the_note_in_its_first_block():
    """The question may carry its words as a block list; the note goes into the
    block the runtime lines already live in, and the words, the calls and the
    results behind it keep their shape and order."""
    provider = _BriefProvider()
    memory = _compacting_memory(provider)
    block = f"{RUNTIME_CONTEXT_TAG}\n{MCP_NOTICE_HEADER}\n{_AUTH}"
    lines = f"{RUNTIME_CONTEXT_TAG}\nChat ID: c1"
    question = {
        "role": "user",
        "content": [{"type": "text", "text": lines}, {"type": "text", "text": "look at this"}],
        RUNTIME_CONTEXT_KEY: lines,
    }
    call = {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function"}]}
    result_msg = {"role": "tool", "tool_call_id": "c1", "content": "done"}
    messages = [
        {"role": "user", "content": "list the repos"},
        {"role": "user", "content": with_runtime_context(block, "older work")},
        {"role": "assistant", "content": "filler x " * 2_200},
        question,
        call,
        result_msg,
    ]
    state = WindowState(image_window=1, session_key="s1", last_context_used=17_000)

    result = asyncio.run(memory.shrink(messages, pressure=WindowPressure.PROACTIVE, state=state, model="m"))

    assert provider.calls == 1 and result.changed
    [repaired] = [m for m in result.messages if m.get(RUNTIME_CONTEXT_KEY)]
    assert repaired["content"][0]["text"] == repaired[RUNTIME_CONTEXT_KEY]
    assert repaired["content"][0]["text"].endswith(_AUTH)
    assert repaired["content"][1:] == [{"type": "text", "text": "look at this"}]
    assert result.messages[-2:] == [call, result_msg]
    assert told_notices(result.messages) == {"github": _AUTH}


async def _raising_chat(**_kwargs):
    raise RuntimeError("the endpoint is down")


def test_a_failed_summary_leaves_the_transcript_alone():
    """A summary that freed nothing hands back the list it was given, and the
    notes in it keep reading exactly as they did: the repair belongs to the
    head a summary took away, not to the attempt at one."""
    provider = _BriefProvider()
    provider.chat = _raising_chat
    memory = _compacting_memory(provider)
    block = f"{RUNTIME_CONTEXT_TAG}\n{MCP_NOTICE_HEADER}\n{_AUTH}"
    messages = _pressured_turn(_question(), block)
    state = WindowState(image_window=1, session_key="s1", last_context_used=17_000)

    result = asyncio.run(memory.shrink(messages, pressure=WindowPressure.PROACTIVE, state=state, model="m"))

    assert result.messages is messages and not result.changed
    assert state.compress_retries == 1 and state.head_summary_failures == 1
    assert told_notices(messages) == {"github": _AUTH}, "nothing was taken away, so nothing needed retelling"


def test_a_skipped_summary_leaves_the_transcript_alone():
    """A head too small to be worth a call is skipped without payment and
    without touching the notes: no summary, no repair, no retry spent."""
    provider = _BriefProvider()
    memory = _compacting_memory(provider)
    block = f"{RUNTIME_CONTEXT_TAG}\n{MCP_NOTICE_HEADER}\n{_AUTH}"
    messages = [
        {"role": "user", "content": "list the repos"},
        {"role": "user", "content": with_runtime_context(block, "older work")},
        _question(),
    ]
    state = WindowState(image_window=1, session_key="s1", last_context_used=17_000)

    result = asyncio.run(memory.shrink(messages, pressure=WindowPressure.PROACTIVE, state=state, model="m"))

    assert result.messages is messages and not result.changed and provider.calls == 0
    assert state.compress_retries == 0
    assert told_notices(messages) == {"github": _AUTH}


def test_compaction_disabled_leaves_the_transcript_alone():
    """With compaction off, no pressure the trigger could fire on reaches a
    summary at all, and the notes ride untouched."""
    provider = _BriefProvider()
    memory = DefaultMemory(
        _Engine(),
        provider=lambda: provider,
        model=lambda: "m",
        context_window_tokens=lambda: 20_000,
        tool_definitions=lambda: [],
        system_prompt=lambda skills: "S",
        compaction=lambda: CompactionConfig(enabled=False),
        output_ceiling=lambda model=None: 4_000,
    )
    block = f"{RUNTIME_CONTEXT_TAG}\n{MCP_NOTICE_HEADER}\n{_AUTH}"
    messages = _pressured_turn(_question(), block)
    state = WindowState(image_window=1, session_key="s1", last_context_used=17_000)

    result = asyncio.run(memory.shrink(messages, pressure=WindowPressure.PROACTIVE, state=state, model="m"))

    assert result.messages is messages and not result.changed and provider.calls == 0
    assert told_notices(messages) == {"github": _AUTH}


def test_pruning_tool_results_alone_touches_no_notice_blocks():
    """The deterministic tier works on tool bodies, not on user messages, so a
    turn it settles never reaches a summary and its note carriers come back
    byte for byte."""
    provider = _BriefProvider()
    memory = DefaultMemory(
        _Engine(),
        provider=lambda: provider,
        model=lambda: "m",
        context_window_tokens=lambda: 20_000,
        tool_definitions=lambda: [],
        system_prompt=lambda skills: "S",
        compaction=lambda: CompactionConfig(enabled=True, prune=True, preserve_recent_tokens=2_500),
        output_ceiling=lambda model=None: 4_000,
    )
    block = f"{RUNTIME_CONTEXT_TAG}\n{MCP_NOTICE_HEADER}\n{_AUTH}"
    question = _question()
    payload = "payload " * 900
    messages = [
        {"role": "user", "content": "list the repos"},
        {"role": "user", "content": with_runtime_context(block, "older work")},
        *[{"role": "tool", "tool_call_id": f"c{i}", "content": payload} for i in range(5)],
        question,
    ]
    state = WindowState(image_window=1, session_key="s1", last_context_used=17_000)

    result = asyncio.run(memory.shrink(messages, pressure=WindowPressure.PROACTIVE, state=state, model="m"))

    assert result.changed and provider.calls == 0, "pruning settled the turn; no summary was owed"
    elided = [m for m in result.messages if m.get("role") == "tool" and m.get("content") != payload]
    assert len(elided) == 2, "fixture must have pruned the two oldest tool bodies"
    carrier = next(m for m in result.messages if "older work" in str(m.get("content")))
    assert carrier is messages[1], "a user message is never the pruner's to edit"
    assert result.messages[-1] is question and MCP_NOTICE_HEADER not in str(question["content"])
    assert told_notices(result.messages) == {"github": _AUTH}
