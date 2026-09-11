"""The four harness strategy roles, and the identity the default set promises.

Every assertion here is about the default set changing nothing: the roles were
carved out of the loop, so the guard that matters is that a turn bound to them
sees what it saw when the same code sat inline. The behaviour-preserving claim
is what makes the seam safe to widen later.
"""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from raven.agent.harness import default_harness_modules
from raven.agent.harness.action import DefaultAction
from raven.agent.harness.capability import DefaultCapability
from raven.agent.harness.memory import DefaultMemory
from raven.agent.harness.memory import bind as bind_memory
from raven.agent.harness.planning import DefaultPlanning
from raven.contracts.harness import (
    ActionModule,
    ActionRequest,
    CapabilityModule,
    CapabilityRequest,
    HarnessModules,
    MemoryModule,
    PlanningModule,
    PlanningRequest,
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
    )
    assert isinstance(bind_memory(memory), MemoryModule)
