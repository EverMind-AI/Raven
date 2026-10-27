"""Two invariants of the context-builder design.

Invariant 1 — Curator output boundary: the Curator contributes only
``*history`` and segment 6 (``# Curator Working State``). It never
touches system segments 1–5. Concretely, injecting a working state is
purely additive at the tail; the rest of the prompt is byte-identical.

Invariant 2 — one owner per segment: each memory / skill block has
exactly one home. The transitional ``# Recalled memory`` and
``# Retrieved skills`` blocks are gone; recall merges into ``# Memory``
and router hits render into ``# Skills`` — one of each, no duplicates.

Invariant 3 — the cacheable prefix: under turn scratch nothing
query-conditioned sits in the system slot, and nothing from the turn
scratch reaches the session log.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.agent.context import ContextBuilder
from raven.config.raven import ContextConfig
from raven.context_engine import ContextAssembler, TurnContext
from raven.context_engine.segments import (
    ActiveSkillsSegmentBuilder,
    BootstrapSegmentBuilder,
    IdentitySegmentBuilder,
    MemorySegmentBuilder,
    SkillsSegmentBuilder,
)
from raven.context_engine.segments.curator import CuratorSegmentBuilder
from raven.contracts.assembled import TokenBudget
from raven.contracts.context import AssemblyContext, Segment
from raven.contracts.memory import Memory
from raven.memory_engine.skill_forge import RouterHit, SkillForgeRouter


@pytest.fixture
def builder(tmp_path: Path) -> ContextBuilder:
    return ContextBuilder(workspace=tmp_path)


class _FakeCurator:
    """Stand-in phase-B builder: emits a fixed working-state seg + history."""

    name = "curator"
    order = 6
    needs_prefix = True

    def __init__(self, working_state: str, history: list[dict] | None = None) -> None:
        self._ws = working_state
        self._history = history or []

    async def build(self, ctx: AssemblyContext) -> Segment:
        assert ctx.prefix is not None  # phase B contract
        text = f"# Curator Working State\n\n{self._ws}" if self._ws else ""
        return Segment(text=text, history=self._history)


def _budget() -> TokenBudget:
    return TokenBudget(100_000, 4_000, 2_000, 1_000, 93_000)


async def _assemble(builder: ContextBuilder, curator, **kw):
    eng = ContextAssembler(
        [IdentitySegmentBuilder(builder.workspace), curator],
        lambda: [],
    )
    return await eng.assemble("s", [], _budget(), turn=TurnContext(current_message="hi"))


def _full_prompt(ac) -> str:
    """System slot plus the turn's user content.

    ``# Memory`` and ``# Skills`` ride in the user message's turn-scratch tail
    rather than the system prompt, so an assertion about "the prompt" has to
    read both slots to keep meaning what it used to.
    """
    parts = [ac.messages[0]["content"]]
    content = ac.messages[-1]["content"]
    if isinstance(content, str):
        parts.append(content)
    else:
        parts.extend(str(b.get("text", "")) for b in content if isinstance(b, dict))
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Invariant 1 — Curator only writes *history + segment 6
# ---------------------------------------------------------------------------


class TestCuratorBoundary:
    async def test_working_state_is_purely_additive_tail(
        self,
        builder: ContextBuilder,
    ) -> None:
        """The Curator's seg6 appends at the tail; the prefix (seg1–5) is
        byte-identical with or without it."""
        base = await _assemble(builder, _FakeCurator(""))
        with_ws = await _assemble(builder, _FakeCurator("goals: ship it"))
        base_sys = base.messages[0]["content"]
        ws_sys = with_ws.messages[0]["content"]
        assert ws_sys.startswith(base_sys)
        assert ws_sys[len(base_sys) :] == "\n\n---\n\n# Curator Working State\n\ngoals: ship it"

    async def test_segments_1_to_5_independent_of_working_state(
        self,
        builder: ContextBuilder,
    ) -> None:
        a = (await _assemble(builder, _FakeCurator("state A"))).messages[0]["content"]
        b = (await _assemble(builder, _FakeCurator("state B-different"))).messages[0]["content"]
        assert a.split("# Curator Working State")[0] == b.split("# Curator Working State")[0]

    async def test_history_slot_is_curator_owned(
        self,
        builder: ContextBuilder,
    ) -> None:
        hist = [{"role": "user", "content": "earlier"}]
        ac = await _assemble(builder, _FakeCurator("", history=hist))
        # messages = [system, *history, user] → history sits in the middle.
        assert ac.messages[1] == {"role": "user", "content": "earlier"}


# ---------------------------------------------------------------------------
# Invariant 2 — one owner per segment, no transitional blocks
# ---------------------------------------------------------------------------


class TestOneOwnerPerSegment:
    async def test_no_transitional_blocks_recall_and_skills_co_located(
        self,
        builder: ContextBuilder,
    ) -> None:
        backend = _Backend([Memory(text="user fact")])
        router = SkillForgeRouter(
            [
                _Source(
                    [
                        RouterHit(qualified_id="local/s", name="s", content="skill body", score=0.9),
                    ]
                )
            ]
        )
        eng = ContextAssembler(
            [
                IdentitySegmentBuilder(builder.workspace),
                MemorySegmentBuilder(builder.memory, backend),
                SkillsSegmentBuilder(router),
                _FakeCurator("ws"),
            ],
            lambda: [],
        )
        ac = await eng.assemble("s", [], _budget(), turn=TurnContext(current_message="hi"))
        prompt = _full_prompt(ac)
        # Transitional split blocks are gone for good.
        assert "# Recalled memory" not in prompt
        assert "# Retrieved skills" not in prompt
        # Recall owned by # Memory; router hits owned by # Skills.
        assert "# Memory" in prompt and prompt.index("# Memory") < prompt.index("user fact")
        assert "### Skill: s  [local/s]" in prompt
        assert "skill body" in prompt
        # Working state is the single, final segment of the system slot.
        assert prompt.count("# Curator Working State") == 1
        assert ac.messages[0]["content"].rstrip().endswith("# Curator Working State\n\nws")


# ---------------------------------------------------------------------------
# Engine-level: the assembled prompt honors both invariants
# ---------------------------------------------------------------------------


class _Backend:
    def __init__(self, mems):
        self._mems = mems

    async def start(self):
        pass

    async def stop(self):
        pass

    async def feedback(self, signals):
        pass

    async def store(self, session_id, messages, *, metadata=None):
        pass

    async def recall(self, query, *, user_id=None, agent_id=None, top_k):
        return list(self._mems)


class _Source:
    name = "local"
    weight = 1.0

    def __init__(self, hits):
        self._hits = hits

    async def search(self, query, history, k):
        return list(self._hits)


class _Provider:
    def get_default_model(self):
        return "stub"

    async def chat_with_retry(self, *a, **k):
        raise NotImplementedError


def _budget():
    return TokenBudget(
        context_length=100_000,
        reserved_output=4_000,
        reserved_tools=2_000,
        reserved_system=1_000,
        available_history=93_000,
    )


class TestEngineAssembledInvariants:
    async def test_fast_path_prompt_has_single_owned_segments(
        self,
        builder: ContextBuilder,
    ) -> None:
        backend = _Backend([Memory(text="user prefers dark mode")])
        router = SkillForgeRouter(
            [
                _Source(
                    [
                        RouterHit(qualified_id="local/g", name="g", content="how to git", score=0.9),
                    ]
                )
            ]
        )
        eng = ContextAssembler(
            [
                IdentitySegmentBuilder(builder.workspace),
                BootstrapSegmentBuilder(builder.workspace),
                MemorySegmentBuilder(builder.memory, backend),
                ActiveSkillsSegmentBuilder(builder.skills),
                SkillsSegmentBuilder(router),
                CuratorSegmentBuilder(
                    workspace=builder.workspace,
                    config=ContextConfig(),
                    provider=_Provider(),
                    model="stub",
                    context_window_tokens=100_000,
                    get_tool_definitions=lambda: [],
                ),
            ],
            lambda: [],
        )
        ac = await eng.assemble(
            "s",
            [],
            _budget(),
            turn=TurnContext(current_message="hi"),
        )
        prompt = _full_prompt(ac)
        assert "# Recalled memory" not in prompt
        assert "# Retrieved skills" not in prompt
        # Recall merged under # Memory; router hit rendered under # Skills.
        assert "# Memory" in prompt
        assert prompt.index("# Memory") < prompt.index("user prefers dark mode")
        assert "### Skill: g  [local/g]" in prompt
        assert "how to git" in prompt


# ---------------------------------------------------------------------------
# Invariant 3 — the cacheable prefix: nothing query-conditioned in the system
# slot, and nothing from the turn scratch in the session log
# ---------------------------------------------------------------------------


class _EchoBackend:
    """Recall that moves with the query -- the whole point of the invariant."""

    async def start(self):
        pass

    async def stop(self):
        pass

    async def recall(self, query, *, user_id=None, agent_id=None, top_k):
        return [Memory(text=f"recalled for {query}")]


class _EchoSource:
    name = "local"
    weight = 1.0

    async def search(self, query, history, k):
        return [RouterHit(qualified_id="local/e", name="e", content=f"skill for {query}", score=0.9)]


class _HistoryCurator(_FakeCurator):
    """A phase-B builder that hands back the session it was given."""

    async def build(self, ctx: AssemblyContext) -> Segment:
        assert ctx.prefix is not None
        return Segment(text="", history=list(ctx.session_messages))


def _prefix_engine(builder: ContextBuilder, **kw) -> ContextAssembler:
    return ContextAssembler(
        [
            IdentitySegmentBuilder(builder.workspace),
            BootstrapSegmentBuilder(builder.workspace),
            MemorySegmentBuilder(builder.memory, _EchoBackend()),
            ActiveSkillsSegmentBuilder(builder.skills),
            SkillsSegmentBuilder(SkillForgeRouter([_EchoSource()])),
            _HistoryCurator(""),
        ],
        lambda: [],
        **kw,
    )


class TestCacheablePrefix:
    async def test_system_slot_holds_nothing_query_conditioned(self, builder: ContextBuilder) -> None:
        eng = _prefix_engine(builder)
        ac = await eng.assemble("s", [], _budget(), turn=TurnContext(current_message="espresso"))

        system = ac.messages[0]["content"]
        assert "# Memory" not in system
        assert "# Skills" not in system
        assert "espresso" not in system
        assert "recalled for espresso" in _full_prompt(ac)
        assert "skill for espresso" in _full_prompt(ac)
        assert ac.metadata["turn_scratch"] is True

    async def test_prefix_is_byte_identical_across_two_queries(self, builder: ContextBuilder) -> None:
        history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "sure"}]
        eng = _prefix_engine(builder)

        first = await eng.assemble("s", history, _budget(), turn=TurnContext(current_message="one"))
        second = await eng.assemble("s", history, _budget(), turn=TurnContext(current_message="two"))

        # Everything up to the turn's own user message is what providers cache.
        assert first.messages[:-1] == second.messages[:-1]
        assert first.messages[-1] != second.messages[-1]

    async def test_turn_scratch_off_puts_the_segments_back_in_system(self, builder: ContextBuilder) -> None:
        eng = _prefix_engine(builder, turn_scratch=False)
        ac = await eng.assemble("s", [], _budget(), turn=TurnContext(current_message="espresso"))

        system = ac.messages[0]["content"]
        assert "# Memory" in system
        assert "# Skills" in system
        assert "recalled for espresso" in system
        assert ac.metadata["turn_scratch"] is False
