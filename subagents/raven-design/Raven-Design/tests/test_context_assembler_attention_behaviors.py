"""P6 — ContextAssembler injects attention.md (selected sections) + folded
behaviors.md tail into PlannerContext.

Covers:

- attention.md selection: only configured sections survive; ordering
  follows the config list, not on-disk order
- behaviors.md folding: time-window filter + max_events cap + folded
  single-line format
- empty paths: missing files / empty sections produce empty fields
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from raven.memory_engine.consolidate.behaviors import (
    BehaviorEvent,
    render_append_block,
)
from raven.memory_engine.consolidate.consolidator import MemoryStore
from raven.proactive_engine.sentinel.predictor.context_assembler import (
    ContextAssembler,
)


class Clock:
    def __init__(self, t0: datetime) -> None:
        self.t = t0

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 5, 29, 14, 0))


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> MemoryStore:
    return MemoryStore(tmp_path, now_fn=clock)


@pytest.fixture
def assembler(store, clock) -> ContextAssembler:
    return ContextAssembler(
        memory_store=store,
        now_fn=clock,
    )


def _seed_attention(store: MemoryStore, text: str) -> None:
    store.attention_file.parent.mkdir(parents=True, exist_ok=True)
    store.attention_file.write_text(text, encoding="utf-8")


def _seed_behaviors_events(
    store: MemoryStore,
    events: list[BehaviorEvent],
) -> None:
    store.behaviors_file.parent.mkdir(parents=True, exist_ok=True)
    store.behaviors_file.write_text(
        render_append_block(events),
        encoding="utf-8",
    )


def _event(**overrides) -> BehaviorEvent:
    defaults = dict(
        id="evt_a1b2c3d4",
        day="2026-05-29",
        start="14:00",
        end="14:30",
        session="cli:default",
        turns=8,
        intent="debug",
        outcome="resolved",
        topic="memory-engine",
        project="raven",
        source="user-asked",
        owner="user",
        tools=["Bash", "Edit"],
        summary="debugged memory_engine session split",
    )
    defaults.update(overrides)
    return BehaviorEvent(**defaults)


# ===========================================================================
# attention.md selection
# ===========================================================================


class TestAttentionForPlanner:
    def test_empty_when_no_attention_file(self, assembler) -> None:
        ctx = assembler.assemble()
        assert ctx.attention_md == ""

    def test_keeps_only_configured_sections(self, store, clock) -> None:
        _seed_attention(
            store,
            "## Pending proposals\n- prop_42\n\n"
            "## Active threads\n- routine_x\n\n"
            "## Currently focused on\n- session cli:default\n",
        )
        assembler = ContextAssembler(
            memory_store=store,
            now_fn=clock,
            attention_planner_sections=[
                "## Pending proposals",
                "## Currently focused on",
            ],
        )
        ctx = assembler.assemble()
        assert "## Pending proposals" in ctx.attention_md
        assert "prop_42" in ctx.attention_md
        assert "## Currently focused on" in ctx.attention_md
        # Active threads NOT in selection → dropped
        assert "## Active threads" not in ctx.attention_md
        assert "routine_x" not in ctx.attention_md

    def test_section_order_follows_config(self, store, clock) -> None:
        _seed_attention(
            store,
            "## Currently focused on\n- focus body\n\n## Pending proposals\n- prop body\n",
        )
        # Config order: Pending first, Currently second
        assembler = ContextAssembler(
            memory_store=store,
            now_fn=clock,
            attention_planner_sections=[
                "## Pending proposals",
                "## Currently focused on",
            ],
        )
        ctx = assembler.assemble()
        assert ctx.attention_md.index("Pending proposals") < (ctx.attention_md.index("Currently focused on"))

    def test_empty_section_bodies_dropped(self, store, clock) -> None:
        _seed_attention(
            store,
            "## Pending proposals\n\n## Currently focused on\n- focus body\n",
        )
        assembler = ContextAssembler(
            memory_store=store,
            now_fn=clock,
            attention_planner_sections=[
                "## Pending proposals",
                "## Currently focused on",
            ],
        )
        ctx = assembler.assemble()
        assert "## Pending proposals" not in ctx.attention_md
        assert "## Currently focused on" in ctx.attention_md


# ===========================================================================
# behaviors.md folding + window
# ===========================================================================


class TestBehaviorsForPlanner:
    def test_empty_when_no_behaviors_file(self, assembler) -> None:
        ctx = assembler.assemble()
        assert ctx.behaviors_recent == ""

    def test_folds_to_single_lines(self, store, clock) -> None:
        _seed_behaviors_events(
            store,
            [
                _event(
                    day="2026-05-28",
                    start="09:00",
                    end="09:30",
                    intent="design",
                    outcome="open",
                    topic="api",
                    project="raven",
                    turns=4,
                    summary="drafted API surface",
                ),
            ],
        )
        assembler = ContextAssembler(
            memory_store=store,
            now_fn=clock,
        )
        ctx = assembler.assemble()
        # Folded single-line format
        assert "[05-28 09:00-09:30 4t]" in ctx.behaviors_recent
        assert "design→open" in ctx.behaviors_recent
        assert "#raven" in ctx.behaviors_recent
        assert "drafted API surface" in ctx.behaviors_recent
        # H3 / sub-bullet noise NOT in folded output
        assert "### evt_" not in ctx.behaviors_recent
        assert "- session:" not in ctx.behaviors_recent

    def test_skips_events_outside_window(self, store, clock) -> None:
        now = clock()
        _seed_behaviors_events(
            store,
            [
                _event(id="evt_old", day=(now - timedelta(days=30)).date().isoformat(), summary="ancient"),
                _event(id="evt_fresh", day=now.date().isoformat(), summary="recent"),
            ],
        )
        assembler = ContextAssembler(
            memory_store=store,
            now_fn=clock,
            behaviors_planner_window_days=14,
        )
        ctx = assembler.assemble()
        assert "recent" in ctx.behaviors_recent
        assert "ancient" not in ctx.behaviors_recent

    def test_caps_at_max_events(self, store, clock) -> None:
        events = [
            _event(id=f"evt_{i:03d}", day="2026-05-29", start=f"{i:02d}:00", end=f"{i:02d}:10", summary=f"item {i}")
            for i in range(10)
        ]
        _seed_behaviors_events(store, events)
        assembler = ContextAssembler(
            memory_store=store,
            now_fn=clock,
            behaviors_planner_max_events=3,
        )
        ctx = assembler.assemble()
        lines = ctx.behaviors_recent.splitlines()
        assert len(lines) == 3
        # Most recent N kept: items 7, 8, 9
        assert "item 9" in ctx.behaviors_recent
        assert "item 0" not in ctx.behaviors_recent

    def test_order_is_oldest_first_within_window(self, store, clock) -> None:
        _seed_behaviors_events(
            store,
            [
                _event(id="evt_a", day="2026-05-28", start="14:00", end="14:30", summary="first"),
                _event(id="evt_b", day="2026-05-29", start="09:00", end="09:30", summary="second"),
            ],
        )
        assembler = ContextAssembler(memory_store=store, now_fn=clock)
        ctx = assembler.assemble()
        # Most recent at the bottom — matches "scroll-down to see latest"
        assert ctx.behaviors_recent.index("first") < (ctx.behaviors_recent.index("second"))


# ===========================================================================
# Defaults wire-up
# ===========================================================================


class TestDefaults:
    def test_default_attention_sections_include_fire_plan(
        self,
        store,
        clock,
    ) -> None:
        _seed_attention(
            store,
            "## Pending proposals\n- p1\n\n"
            "## Rejected proposals (cooldown)\n- r1\n\n"
            "## Recent stance log (30d)\n- s1\n\n"
            "## Predicted next 3 days\n- pred1\n\n"
            "## Currently focused on\n- focus\n\n"
            "## Recent proactive decisions (14d)\n- dec1\n\n"
            "## 今日 fire 计划\n- 09:00 deadline_report | msg=交报告\n\n"
            "## Active threads\n- t1\n\n"  # NOT in default selection
            "## Sentinel Observations (auto)\n- so\n",  # also excluded
        )
        # No explicit attention_planner_sections → uses the 7 defaults, which
        # now include the daily fire plan so a deferred deadline slot is in the
        # Planner's context.
        assembler = ContextAssembler(memory_store=store, now_fn=clock)
        ctx = assembler.assemble()
        for h2 in [
            "## Pending proposals",
            "## Rejected proposals (cooldown)",
            "## Recent stance log (30d)",
            "## Predicted next 3 days",
            "## Currently focused on",
            "## Recent proactive decisions (14d)",
            "## 今日 fire 计划",
        ]:
            assert h2 in ctx.attention_md, f"missing default section: {h2}"
        assert "deadline_report" in ctx.attention_md  # slot body reaches Planner
        assert "## Active threads" not in ctx.attention_md
        assert "## Sentinel Observations (auto)" not in ctx.attention_md
