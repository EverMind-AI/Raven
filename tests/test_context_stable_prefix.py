"""Where the system message stops being the same on the next turn.

A prompt cache keys on every block up to its breakpoint, and the system message
is rebuilt each turn from segments -- the first of which (identity, the bootstrap
files) read the same every time, while the rest (memory recall, the skill
router's hits, the Curator's working state) are derived from what the user just
said. One breakpoint at the end of that message therefore changes key on every
new turn and re-bills the stable head in front of it.

These pin the boundary the assembler measures for a second breakpoint: that it
falls between segments, that one volatile segment ends the run for every stable
segment behind it, and that declaring it changes no text the model reads.
"""

from __future__ import annotations

import pytest

from raven.context_engine.assembler import _SEG_SEP, _stable_prefix_chars
from raven.context_engine.segments import (
    ActiveSkillsSegmentBuilder,
    BootstrapSegmentBuilder,
    IdentitySegmentBuilder,
    MemorySegmentBuilder,
    SkillsSegmentBuilder,
)
from raven.context_engine.segments.curator import CuratorSegmentBuilder
from raven.providers.prompt_cache import STABLE_PREFIX_KEY


def _parts(*spec: tuple[str, bool]) -> list[tuple[int, str, bool]]:
    return [(i, text, stable) for i, (text, stable) in enumerate(spec, start=1)]


class TestTheRunOfStableSegments:
    def test_the_boundary_falls_between_the_run_and_what_follows(self):
        parts = _parts(("IDENT", True), ("BOOT", True), ("RECALL", False))

        chars = _stable_prefix_chars(parts)

        whole = _SEG_SEP.join(text for _, text, _ in parts)
        assert whole[:chars] == "IDENT" + _SEG_SEP + "BOOT" + _SEG_SEP
        assert whole[chars:] == "RECALL"

    def test_one_volatile_segment_ends_the_run_for_everything_behind_it(self):
        """The reason `active_skills` gains nothing where it sits today.

        It does not depend on the message, but `memory` is in front of it, so a
        breakpoint any later than `memory` keys on text that changes anyway.
        """
        parts = _parts(("IDENT", True), ("RECALL", False), ("ALWAYS-ON SKILLS", True))

        chars = _stable_prefix_chars(parts)

        whole = _SEG_SEP.join(text for _, text, _ in parts)
        assert whole[:chars] == "IDENT" + _SEG_SEP
        assert "ALWAYS-ON SKILLS" in whole[chars:]

    @pytest.mark.parametrize(
        ("parts", "why"),
        [
            (_parts(("RECALL", False), ("MORE", False)), "no run at all"),
            (_parts(("IDENT", True), ("BOOT", True)), "the run is the whole message and nothing follows"),
            ([], "nothing was contributed"),
        ],
    )
    def test_a_boundary_at_either_end_is_not_declared(self, parts, why):
        """Zero, because such a breakpoint would sit where the end-of-message
        one already is and cache nothing it does not."""
        assert _stable_prefix_chars(parts) == 0, why


class TestWhenPhaseBIsTheOnlyThingThatFollows:
    """An all-stable phase A is not the end of the message if phase B appends.

    The Curator runs in phase B on every turn it has anything to say, and the
    segments in front of it can all be stable at once: identity and bootstrap
    present, memory and both skill segments contributing nothing. Read over
    phase A alone that run looks terminal, and a boundary at the end of the
    message is worth nothing -- so it used to be dropped, and the assembled
    message went out as `IDENTITY<sep>CURATOR` carrying no key. That is the
    exact miss this measurement exists to remove.
    """

    def test_the_whole_stable_prefix_becomes_the_head_when_a_tail_follows(self):
        parts = _parts(("IDENT", True), ("BOOT", True))

        chars = _stable_prefix_chars(parts, tail_follows=True)

        prefix = _SEG_SEP.join(text for _, text, _ in parts)
        assert chars == len(prefix) + len(_SEG_SEP)
        finished = prefix + _SEG_SEP + "CURATOR"
        assert finished[:chars] == prefix + _SEG_SEP
        assert finished[chars:] == "CURATOR"

    def test_nothing_following_still_declares_nothing(self):
        parts = _parts(("IDENT", True), ("BOOT", True))

        assert _stable_prefix_chars(parts, tail_follows=False) == 0

    def test_a_run_that_already_ends_inside_phase_a_is_unaffected(self):
        """`tail_follows` decides only the terminal case."""
        parts = _parts(("IDENT", True), ("RECALL", False))

        assert _stable_prefix_chars(parts, tail_follows=True) == _stable_prefix_chars(parts, tail_follows=False)

    async def test_the_assembler_declares_it_for_an_all_stable_phase_a(self):
        from raven.context_engine import ContextAssembler, TurnContext
        from raven.context_engine.base import AssemblyContext, Segment
        from raven.memory_engine.base import TokenBudget

        class _PhaseA:
            def __init__(self, name, order, text):
                self.name, self.order, self.text = name, order, text
                self.needs_prefix, self.stable = False, True

            async def build(self, ctx: AssemblyContext) -> Segment:
                return Segment(text=self.text)

        class _Curator:
            name, order, needs_prefix, stable = "curator", 6, True, False

            async def build(self, ctx: AssemblyContext) -> Segment:
                return Segment(text="CURATOR")

        eng = ContextAssembler(
            [_PhaseA("identity", 1, "IDENT"), _PhaseA("boot", 2, "BOOT"), _Curator()],
            lambda: [],
        )

        ac = await eng.assemble(
            "s", [], TokenBudget(100_000, 4_000, 2_000, 1_000, 93_000), turn=TurnContext(current_message="hi")
        )

        system = ac.messages[0]
        assert STABLE_PREFIX_KEY in system, "an all-stable phase A with a Curator tail declares a head"
        chars = system[STABLE_PREFIX_KEY]
        assert system["content"][:chars] == "IDENT" + _SEG_SEP + "BOOT" + _SEG_SEP
        assert system["content"][chars:] == "CURATOR"

    async def test_an_all_stable_message_with_no_tail_declares_nothing(self):
        from raven.context_engine import ContextAssembler, TurnContext
        from raven.context_engine.base import AssemblyContext, Segment
        from raven.memory_engine.base import TokenBudget

        class _PhaseA:
            def __init__(self, name, order, text):
                self.name, self.order, self.text = name, order, text
                self.needs_prefix, self.stable = False, True

            async def build(self, ctx: AssemblyContext) -> Segment:
                return Segment(text=self.text)

        eng = ContextAssembler([_PhaseA("identity", 1, "IDENT"), _PhaseA("boot", 2, "BOOT")], lambda: [])

        ac = await eng.assemble(
            "s", [], TokenBudget(100_000, 4_000, 2_000, 1_000, 93_000), turn=TurnContext(current_message="hi")
        )

        assert STABLE_PREFIX_KEY not in ac.messages[0]


class TestTheShippedSegmentsDeclareThemselves:
    """Whether each builder's `stable` flag matches what it reads.

    Asserted here rather than left to the assembler's arithmetic, because the
    flag is a claim about a segment's inputs and the cost of getting one wrong
    is silent: a segment wrongly called stable pays for a write whose key never
    matches again.
    """

    @pytest.mark.parametrize(
        ("builder", "stable", "why"),
        [
            (IdentitySegmentBuilder, True, "SOUL.md and the identity header"),
            (BootstrapSegmentBuilder, True, "the bootstrap files as they are on disk"),
            (MemorySegmentBuilder, False, "host memory is picked per message; recall queries it"),
            (ActiveSkillsSegmentBuilder, True, "the always-on set, chosen without the message"),
            (SkillsSegmentBuilder, False, "the router's hits for this message"),
            (CuratorSegmentBuilder, False, "working state recomputed for this turn"),
        ],
    )
    def test_each_builder_says_whether_it_reads_the_message(self, builder, stable, why):
        assert builder.stable is stable, why


class TestTheAssemblerDeclaresIt:
    async def test_the_offset_points_at_the_message_it_was_measured_over(self):
        from raven.context_engine import ContextAssembler, TurnContext
        from raven.context_engine.base import AssemblyContext, Segment
        from raven.memory_engine.base import TokenBudget

        class _Seg:
            def __init__(self, name, order, text, stable):
                self.name, self.order, self.text, self.stable = name, order, text, stable
                self.needs_prefix = False

            async def build(self, ctx: AssemblyContext) -> Segment:
                return Segment(text=self.text)

        eng = ContextAssembler(
            [_Seg("identity", 1, "IDENT", True), _Seg("boot", 2, "BOOT", True), _Seg("recall", 3, "RECALL", False)],
            lambda: [],
        )

        ac = await eng.assemble(
            "s", [], TokenBudget(100_000, 4_000, 2_000, 1_000, 93_000), turn=TurnContext(current_message="hi")
        )

        system = ac.messages[0]
        chars = system[STABLE_PREFIX_KEY]
        assert system["content"][:chars] == "IDENT" + _SEG_SEP + "BOOT" + _SEG_SEP
        assert system["content"][chars:] == "RECALL"

    async def test_an_all_volatile_prefix_declares_nothing(self):
        from raven.context_engine import ContextAssembler, TurnContext
        from raven.context_engine.base import AssemblyContext, Segment
        from raven.memory_engine.base import TokenBudget

        class _Seg:
            name, order, needs_prefix, stable = "recall", 1, False, False

            async def build(self, ctx: AssemblyContext) -> Segment:
                return Segment(text="RECALL")

        eng = ContextAssembler([_Seg()], lambda: [])

        ac = await eng.assemble(
            "s", [], TokenBudget(100_000, 4_000, 2_000, 1_000, 93_000), turn=TurnContext(current_message="hi")
        )

        assert STABLE_PREFIX_KEY not in ac.messages[0]
