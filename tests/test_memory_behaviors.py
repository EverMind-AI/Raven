"""Round-trip tests for ``memory_engine.consolidate.behaviors``."""

from __future__ import annotations

from raven.memory_engine.consolidate.behaviors import (
    BehaviorEvent,
    parse_behaviors,
    render_append_block,
    render_event,
    slice_after_day,
)


def _event(**overrides) -> BehaviorEvent:
    defaults: dict[str, object] = dict(
        id="evt_a1b2c3",
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


def test_parse_empty_returns_empty_list() -> None:
    assert parse_behaviors("") == []


def test_render_event_emits_h3_and_five_field_bullets() -> None:
    text = render_event(_event())
    assert text.startswith("### evt_a1b2c3 — 14:00–14:30")
    assert "- session: `cli:default` · turns: 8" in text
    assert "- intent: debug · outcome: resolved" in text
    assert "- topic: memory-engine · project: raven" in text
    assert "- source: user-asked · owner: user · tools: Bash, Edit" in text
    assert "- summary: debugged memory_engine session split" in text


def test_render_event_omits_summary_when_empty() -> None:
    text = render_event(_event(summary=""))
    assert "- summary:" not in text


def test_append_block_groups_by_day_with_weekday_tag() -> None:
    e1 = _event(id="evt_001", day="2026-05-29", start="09:00", end="09:15")
    e2 = _event(id="evt_002", day="2026-05-29", start="14:00", end="14:30")
    e3 = _event(id="evt_003", day="2026-05-28", start="11:00", end="11:20")
    block = render_append_block([e1, e2, e3])
    # Sorted by day ascending, so 05-28 comes first.
    assert block.index("## 2026-05-28") < block.index("## 2026-05-29")
    assert "(Thu)" in block  # 2026-05-28 is Thursday
    assert "(Fri)" in block  # 2026-05-29 is Friday
    assert block.count("## 2026-05-29") == 1  # one extraction = one H2 per day


def test_round_trip_single_event() -> None:
    original = _event()
    text = render_append_block([original])
    parsed = parse_behaviors(text)
    assert len(parsed) == 1
    assert parsed[0] == original


def test_round_trip_multi_day_multi_event() -> None:
    events = [
        _event(id="evt_a", day="2026-05-28", start="09:00", end="09:10",
               topic="api", tools=["Read"]),
        _event(id="evt_b", day="2026-05-29", start="14:00", end="14:30",
               intent="design", outcome="open"),
        _event(id="evt_c", day="2026-05-29", start="16:00", end="16:05",
               session="telegram:user42", tools=[]),
    ]
    text = render_append_block(events)
    parsed = parse_behaviors(text)
    # Sorted ascending by day in render; ordering within day preserved.
    assert [e.id for e in parsed] == ["evt_a", "evt_b", "evt_c"]
    assert parsed[0].topic == "api"
    assert parsed[0].tools == ["Read"]
    assert parsed[1].intent == "design"
    assert parsed[2].session == "telegram:user42"
    assert parsed[2].tools == []


def test_parse_tolerates_duplicate_h2_for_same_day() -> None:
    """Append-only extractor may emit two H2 blocks for the same date when
    a second idle extraction runs later. Parser must concatenate."""
    text = (
        "## 2026-05-29 (Fri)\n\n"
        + render_event(_event(id="evt_001", start="09:00", end="09:15"))
        + "\n\n"
        + "## 2026-05-29 (Fri)\n\n"
        + render_event(_event(id="evt_002", start="14:00", end="14:30"))
        + "\n"
    )
    parsed = parse_behaviors(text)
    assert [e.id for e in parsed] == ["evt_001", "evt_002"]
    assert all(e.day == "2026-05-29" for e in parsed)


def test_parse_skips_malformed_events_without_crashing() -> None:
    text = (
        "## 2026-05-29 (Fri)\n\n"
        "### evt_ok — 09:00–09:15\n"
        "- session: `cli:x` · turns: 1\n"
        "- intent: foo · outcome: bar\n"
        "- topic: t · project: p\n"
        "- source: s · owner: o · tools: \n"
        "\n"
        "garbage line without ### prefix\n"
        "- this is a stray bullet\n"
        "\n"
        "### evt_ok2 — 10:00–10:30\n"
        "- session: `cli:y` · turns: 2\n"
    )
    parsed = parse_behaviors(text)
    assert [e.id for e in parsed] == ["evt_ok", "evt_ok2"]


def test_round_trip_preserves_tools_order() -> None:
    e = _event(tools=["Bash", "Edit", "Read"])
    parsed = parse_behaviors(render_append_block([e]))
    assert parsed[0].tools == ["Bash", "Edit", "Read"]


def test_slice_after_day_returns_suffix_from_first_matching_h2() -> None:
    text = render_append_block([
        _event(id="evt_old", day="2026-05-20", start="09:00", end="09:10"),
        _event(id="evt_mid", day="2026-05-25", start="09:00", end="09:10"),
        _event(id="evt_new", day="2026-05-30", start="09:00", end="09:10"),
    ])
    sliced = slice_after_day(text, "2026-05-25")
    parsed = parse_behaviors(sliced)
    assert [e.id for e in parsed] == ["evt_mid", "evt_new"]


def test_slice_after_day_empty_when_no_h2_reaches_window() -> None:
    text = render_append_block([
        _event(id="evt_old", day="2026-05-20", start="09:00", end="09:10"),
    ])
    assert slice_after_day(text, "2026-06-01") == ""


def test_slice_after_day_passthrough_on_empty_inputs() -> None:
    assert slice_after_day("", "2026-05-29") == ""
    assert slice_after_day("# nothing", "") == "# nothing"
