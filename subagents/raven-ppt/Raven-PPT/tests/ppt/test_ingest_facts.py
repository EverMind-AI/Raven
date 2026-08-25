"""The index every claim is checked against: what it holds, and in what shape.

The gate and the index have to agree token for token, so these pin the
normalisation rules rather than the code that applies them.
"""

from __future__ import annotations

import json
from pathlib import Path

from raven.ppt.services.ingest.facts import (
    build_source_index,
    canonical_number,
    load_source_index,
    normalise,
    write_source_index,
)


def test_a_number_keeps_its_printed_identity_not_its_value() -> None:
    assert canonical_number("30,972", None) == "30972"
    assert canonical_number("31", "billion") == "31e9"
    assert canonical_number("31", "bn") == "31e9"
    assert canonical_number("14", "%") == "14%"
    assert canonical_number("14", "percent") == "14%"
    assert canonical_number("2.5", "x") == "2.5x"
    assert canonical_number("2.5", "×") == "2.5x"
    # A trailing zero of a decimal is not a different figure.
    assert canonical_number("31.0", None) == canonical_number("31", None) == "31"
    assert canonical_number("31.50", None) == "31.5"


def test_grouping_and_scale_both_reach_the_index() -> None:
    index = build_source_index("Revenue was $30,972 million, up 14%, from 4,230 units.")
    assert {"30972e6", "30972", "14%", "4230"} <= index.numbers
    # A percentage is not also a bare count: 14% and 14 are both printed here,
    # and the bare form is what anchors a slide that drops the sign.
    assert "14" in index.numbers


def test_a_scale_stated_once_covers_the_numbers_under_it() -> None:
    """A financial table says "in millions" in one line and prints bare digits."""
    index = build_source_index("Segment revenue in millions\nCloud 30,972\nDevices 4,230\n")
    assert "30972e6" in index.numbers
    assert "4230e6" in index.numbers


def test_names_are_indexed_upper_case_and_ordinary_words_are_not() -> None:
    index = build_source_index("The M4 chip and the gpt-4o model reach 38 TOPS on a laptop.")
    assert {"M4", "GPT-4O", "TOPS"} <= index.entities
    assert "LAPTOP" not in index.entities
    assert "CHIP" not in index.entities


def test_a_name_the_materials_never_state_is_simply_absent() -> None:
    """The whitelist is a set, so the rejection path is a missing member."""
    index = build_source_index("The M4 chip delivers 38 TOPS.")
    assert "M4" in index.entities
    assert "M5" not in index.entities
    assert "85" not in index.numbers


def test_caps_phrases_are_indexed_lower_cased_in_windows() -> None:
    index = build_source_index("state of the art results\n")
    assert "state of the art" in index.caps_phrases
    assert "state of" in index.caps_phrases
    # Six words is the longest window kept.
    assert "state of the art results" in index.caps_phrases


def test_a_page_anchor_can_be_cited_but_cannot_authorise_a_value() -> None:
    """The ingest writes "## page 17" itself; a chart bar of 17 is not sourced."""
    index = build_source_index("# Source scan\n\n## page 17\n\nNo machine-readable values.\n")
    assert "17" in index.numbers
    assert index.stated_numbers == frozenset()


def test_a_number_in_the_body_is_stated_even_next_to_headings() -> None:
    index = build_source_index("## [report.pdf] page 3\n\nRevenue was 42 million.\n")
    assert {"42e6", "3"} <= index.numbers
    assert "42e6" in index.stated_numbers
    assert "3" not in index.stated_numbers


def test_nfkc_is_the_whole_of_the_text_normalisation() -> None:
    """The predecessor also replaced NBSP after NFKC had already done it."""
    assert normalise("31 billion") == "31 billion"
    assert "31e9" in build_source_index("31 billion in revenue").numbers


def test_the_index_round_trips_through_its_file(tmp_path: Path) -> None:
    index = build_source_index("The M4 chip delivers 38 TOPS. Revenue was $30,972 million.\n## page 9\n")
    path = tmp_path / "fact_index.json"
    write_source_index(index, path, sources=["report.pdf"])

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema"] == "raven.ppt.source-index.v1"
    assert raw["sources"] == ["report.pdf"]
    assert load_source_index(path) == index


def test_an_index_file_missing_a_field_loads_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "fact_index.json"
    path.write_text(json.dumps({"numbers": ["38"]}), encoding="utf-8")
    loaded = load_source_index(path)
    assert loaded.numbers == frozenset({"38"})
    assert loaded.stated_numbers == frozenset()
    assert loaded.caps_phrases == frozenset()


def test_a_spec_is_not_a_grouped_number() -> None:
    """`44 FPS(单卡 A100,480p)` read as thousands becomes 100,480, which is in no source.

    Three pages of a live deck were refused for writing 480p after A100 with no space
    between them -- the comma is punctuation there, not a separator.
    """
    index = build_source_index("It runs at 44 FPS on a single A100 at 480p.")
    page = "推理速度:44 FPS(单卡 A100,480p)"
    assert {canonical_number(n, None) for n in ("44", "100", "480")} <= index.numbers
    assert "100480" not in index.numbers
    assert "100480" not in build_source_index(page).numbers


def test_grouped_thousands_still_group_when_nothing_alphanumeric_precedes() -> None:
    for text, want in (("1,000km 的距离", "1000"), ("$1,234,567 收入", "1234567"), ("营收 12,345.6 万元", "12345.6")):
        assert want in build_source_index(text).numbers, text
