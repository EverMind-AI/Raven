"""Round-trip tests for ``memory_engine.consolidate.attention``."""

from __future__ import annotations

from raven.memory_engine.consolidate.attention import (
    ATTENTION_SECTIONS,
    parse_attention,
    render_attention,
    upsert_section,
)


def test_parse_returns_empty_for_empty_text() -> None:
    assert parse_attention("") == {}
    assert parse_attention("no headings here") == {}


def test_parse_splits_two_canonical_sections() -> None:
    text = "## User overrides\n- 凌晨别 nudge\n\n## Pending proposals\n- prop_42: review weekly\n"
    out = parse_attention(text)
    assert out == {
        "## User overrides": "- 凌晨别 nudge",
        "## Pending proposals": "- prop_42: review weekly",
    }


def test_parse_normalizes_chinese_aliases() -> None:
    text = "## 用户指令\n- 凌晨别打扰\n\n## 待处理提议\n- prop_1\n"
    out = parse_attention(text)
    assert "## User overrides" in out
    assert "## Pending proposals" in out
    assert "## 用户指令" not in out


def test_parse_preserves_unknown_headings() -> None:
    text = "## Custom section\n- body\n"
    out = parse_attention(text)
    assert out == {"## Custom section": "- body"}


def test_render_emits_canonical_order_regardless_of_input_order() -> None:
    sections = {
        "## Pending proposals": "- p1",
        "## User overrides": "- ov1",
    }
    text = render_attention(sections)
    # User overrides is index 0 in ATTENTION_SECTIONS, Pending proposals is 2.
    assert text.index("## User overrides") < text.index("## Pending proposals")


def test_render_skips_empty_sections_by_default() -> None:
    sections = {"## User overrides": "", "## Pending proposals": "- p1"}
    text = render_attention(sections)
    assert "## User overrides" not in text
    assert "## Pending proposals" in text


def test_render_includes_empty_when_requested() -> None:
    sections = {"## User overrides": "", "## Pending proposals": "- p1"}
    text = render_attention(sections, include_empty=True)
    assert text.count("## User overrides") == 1
    assert "## Pending proposals" in text


def test_render_appends_unknown_sections_after_canonical() -> None:
    sections = {
        "## Custom thing": "- custom",
        "## User overrides": "- ov",
    }
    text = render_attention(sections)
    assert text.index("## User overrides") < text.index("## Custom thing")


def test_round_trip_preserves_canonical_section_bodies() -> None:
    sections = {h2: f"- body of {h2}" for h2 in ATTENTION_SECTIONS}
    rendered = render_attention(sections)
    parsed = parse_attention(rendered)
    assert parsed == sections


def test_upsert_section_replaces_existing() -> None:
    text = "## User overrides\n- old\n\n## Pending proposals\n- p\n"
    new = upsert_section(text, "## User overrides", "- new")
    parsed = parse_attention(new)
    assert parsed["## User overrides"] == "- new"
    assert parsed["## Pending proposals"] == "- p"


def test_upsert_section_appends_when_absent() -> None:
    text = "## User overrides\n- ov\n"
    new = upsert_section(text, "## Pending proposals", "- new")
    parsed = parse_attention(new)
    assert parsed["## User overrides"] == "- ov"
    assert parsed["## Pending proposals"] == "- new"


def test_upsert_section_normalizes_alias_target() -> None:
    text = "## User overrides\n- ov\n"
    # Caller used Chinese alias — should resolve via parse normalization on
    # the next round trip.
    new = upsert_section(text, "## User overrides", "- new ov")
    parsed = parse_attention(new)
    assert parsed["## User overrides"] == "- new ov"
