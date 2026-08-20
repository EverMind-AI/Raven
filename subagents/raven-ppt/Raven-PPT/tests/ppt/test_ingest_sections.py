"""The index that lets the materials be read a part at a time.

Reading the whole file is what a live run did, and it was the single most expensive
thing in a $45 deck: 16k tokens entering the context at step 6 and re-sent on the 99
requests after it, for about $8. The index costs 533 tokens on the same file.
"""

from __future__ import annotations

from pathlib import Path

from raven.ppt.services.ingest import MAX_PARTS, index, sections

MATERIALS = """# Source: paper.pdf

## [paper.pdf] page 1

TarViS states every task as the same thing.
Given a set of target queries, segment those targets.

## [paper.pdf] page 2

Table 1 reports the per-task comparison.

# Source: notes.md

## Method

A single transformer decoder.
"""


def test_every_heading_carries_the_line_read_file_takes(tmp_path: Path) -> None:
    path = tmp_path / "materials.md"
    path.write_text(MATERIALS, encoding="utf-8")

    parts = sections(path)

    assert [part.heading for part in parts] == [
        "Source: paper.pdf",
        "[paper.pdf] page 1",
        "[paper.pdf] page 2",
        "Source: notes.md",
        "Method",
    ]
    # 1-based, because that is what read_file's offset is.
    assert [part.line for part in parts] == [1, 3, 8, 12, 14]
    lines = path.read_text(encoding="utf-8").splitlines()
    for part in parts:
        assert lines[part.line - 1].lstrip("# ").strip() == part.heading
    # The span reaches the next heading, so offset+limit reads the part whole.
    page_one = next(part for part in parts if part.heading == "[paper.pdf] page 1")
    body = "\n".join(lines[page_one.line - 1 : page_one.line - 1 + page_one.lines])
    assert "target queries" in body and "Table 1" not in body


def test_a_file_of_a_hundred_pages_lists_its_sources_instead(tmp_path: Path) -> None:
    """Past forty parts the index costs more than it saves, and nobody reads a
    hundred one-page entries."""
    path = tmp_path / "long.md"
    body = ["# Source: big.pdf", ""]
    for number in range(1, MAX_PARTS + 12):
        body += [f"## [big.pdf] page {number}", "", "text", ""]
    path.write_text("\n".join(body), encoding="utf-8")

    assert len(sections(path)) > MAX_PARTS
    assert [entry["heading"] for entry in index(path)] == ["Source: big.pdf"]


def test_a_file_with_no_headings_has_no_index(tmp_path: Path) -> None:
    path = tmp_path / "flat.md"
    path.write_text("just text, no headings at all\n", encoding="utf-8")

    assert sections(path) == () and index(path) == []


def test_a_missing_file_is_not_an_error(tmp_path: Path) -> None:
    assert sections(tmp_path / "gone.md") == ()


def test_the_tool_returns_the_index_and_asks_for_parts() -> None:
    """The ask matters as much as the index: the wording it replaces ("read
    materials.md before deciding what the deck says") is what sent a live author to
    read all 16k of it.
    """
    from raven.ppt.tools.ingest import PptIngestTool

    source = Path("raven/ppt/tools/ingest.py").read_text(encoding="utf-8")
    assert '"materials_index": index(' in source
    assert "read what you need of" in source
    assert "offset and limit" in source
    del PptIngestTool
