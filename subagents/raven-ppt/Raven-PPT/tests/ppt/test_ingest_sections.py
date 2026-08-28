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
    """The ask matters as much as the index, and "read what you need of" was not
    enough.

    Measured A/B on one paper, same model both sides: the author holding the index
    read 65% of the file, the one without it 37%. A list of page numbers is a
    checklist, not a filter, and "what you need" is a judgement it cannot make from
    page numbers. So the wording now states the granularity up front and gives the
    opening words one job -- ruling parts out -- with reading as the fallback.
    """
    from raven.ppt.tools.ingest import PptIngestTool

    source = Path("raven/ppt/tools/ingest.py").read_text(encoding="utf-8")
    assert '"materials_index": index(' in source
    assert "a part at a time" in source
    assert "rule parts out" in source, "the opening words are for exclusion"
    assert "unsure" in source, "and reading is what it falls back to"
    assert "offset and limit" in source
    del PptIngestTool


def test_a_part_carries_enough_to_be_ruled_out(tmp_path: Path) -> None:
    """A bibliography and a page of class names are what the gist has to catch: they
    are the parts a deck can never quote, and together they were a fifth of the file
    on the run that prompted this.
    """
    materials = tmp_path / "materials.md"
    materials.write_text(
        "# Source: paper.pdf\n\n"
        "## [paper.pdf] page 8\n\n"
        "Table 4. Ablation experiment results with ResNet-50 backbone.\n\n"
        "## [paper.pdf] page 9\n\n"
        "References\n\n[1] A. Author, A paper, CVPR 2023.\n\n"
        "## [paper.pdf] page 14\n\n"
        "airplane\nbicycle\nbird\n",
        encoding="utf-8",
    )
    by_heading = {part.heading: part.gist for part in sections(materials)}
    assert by_heading["[paper.pdf] page 8"].startswith("Table 4.")
    assert by_heading["[paper.pdf] page 9"] == "References"
    assert by_heading["[paper.pdf] page 14"] == "airplane"


def test_a_part_of_stray_labels_says_so(tmp_path: Path) -> None:
    """A PDF page that is only a diagram comes out as loose labels. The gist reports
    the first of them rather than inventing a description -- which is the honest
    answer: that part is fragments, and an author can see that.
    """
    materials = tmp_path / "materials.md"
    materials.write_text(
        "## [paper.pdf] page 3\n\n---\n\nQsem\nQinst\nBackbone\n",
        encoding="utf-8",
    )
    assert sections(materials)[0].gist == "Qsem"
