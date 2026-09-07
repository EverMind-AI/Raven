"""The report audit finds the defects that decided the 2026-09-04 head-to-head.

Each test below is one of them, written from the report that carried it, so a regression
in the parser shows up as the head-to-head becoming unmeasurable rather than as a silent
zero. The three hard classes - an estimate inside a scored column, a ranking that
contradicts its totals, a citation nobody opened - are what ``--strict`` gates on.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from scripts.research_report_audit import (
    DERIVED_MARKERS,
    MARKER_GLOSSARY,
    _fenced,
    _order_tolerance,
    audit,
    check_estimates,
    check_ranking,
    check_table_width,
    check_unfetched_cells,
    link_columns,
    main,
    parse_tables,
    parse_trail,
    quantitative_columns,
    rank_column,
    rows_out_of_order,
    total_column,
)

RANKED_TABLE = """\
| # | Benchmark | Genre | Total | Size (found) |
|---|-----------|:--:|:--:|------|
| 1 | QASPER | 5 | 22 | 5,049 q / 1,585 papers |
| 2 | LV-Eval | 4 | 20 | 1,331 QA |
| 3 | CrossCodeEval | 5 | 16 | 4 langs, ~2k (est.) |
"""


def _tables(md: str):
    return parse_tables(md.split("\n"))


def test_a_table_is_split_into_its_header_and_body():
    (table,) = _tables(RANKED_TABLE)
    assert table.header == ["#", "Benchmark", "Genre", "Total", "Size (found)"]
    assert len(table.rows) == 3
    assert table.rows[0][1] == "QASPER"


def test_a_mostly_numeric_column_is_quantitative_and_a_prose_one_is_not():
    (table,) = _tables(RANKED_TABLE)
    quant = quantitative_columns(table)
    assert table.header.index("Total") in quant
    assert table.header.index("Genre") in quant
    assert table.header.index("Benchmark") not in quant


def test_an_estimate_in_a_scored_column_is_a_hard_finding():
    findings = check_estimates(_tables(RANKED_TABLE))
    assert [f.kind for f in findings] == ["estimate_in_quantitative_column"]
    assert findings[0].hard
    assert "CrossCodeEval" in findings[0].detail


def test_an_estimate_in_a_prose_column_is_not_reported():
    md = """\
| # | Benchmark | Note | Total |
|---|-----------|---|:--:|
| 1 | QASPER | size est. from the paper's abstract | 22 |
"""
    assert check_estimates(_tables(md)) == []


def test_a_derivation_is_not_an_estimate_but_a_declared_estimate_still_is():
    md = """\
| # | Benchmark | Queries per doc | Size |
|---|-----------|:--:|---|
| 1 | QASPER | ≈3.2 (derived: 5,049/1,585) | 5,049 q |
| 2 | LitQA2 | ≈1 (derived) | ~1k MC (est.) |
"""
    kinds = [(f.detail, f.hard) for f in check_estimates(_tables(md))]
    assert len(kinds) == 1, kinds
    detail, hard = kinds[0]
    assert hard and "LitQA2" in detail


RANKED_WITH_ONE_INVERSION = """\
| # | Benchmark | Genre | Depth | Total |
|---|-----------|:--:|:--:|:--:|
| 1 | QASPER | 5 | 5 | 10 |
| 2 | LV-Eval | 5 | 4 | 9 |
| 3 | MultiHop-RAG | 4 | 4 | 8 |
| 4 | CUAD | 4 | 3 | 7 |
| 5 | RepoQA | 4 | 4 | 8 |
| 6 | L-Eval | 3 | 3 | 6 |
"""


def test_a_rank_that_contradicts_its_total_is_a_hard_finding():
    (finding,) = check_ranking(_tables(RANKED_WITH_ONE_INVERSION))
    assert finding.kind == "rank_contradicts_total"
    assert finding.hard
    assert "RepoQA" in finding.detail and "CUAD" in finding.detail


def test_a_ranking_that_agrees_with_its_totals_is_clean():
    assert check_ranking(_tables(RANKED_TABLE)) == []


def test_equal_totals_are_not_an_inversion():
    md = """\
| # | Benchmark | Total |
|---|-----------|:--:|
| 1 | QASPER | 22 |
| 2 | LongCodeQA | 22 |
"""
    assert check_ranking(_tables(md)) == []


def test_a_row_the_report_marks_unfetched_is_reported_from_any_column():
    md = """\
| Method | Benchmarks used | Link |
|---|---|---|
| AutoCompressors | summarization/QA (paper; not fetched) | https://arxiv.org/abs/2306.10424 |
"""
    (finding,) = check_unfetched_cells(_tables(md))
    assert finding.kind == "unfetched_table_row"
    assert not finding.hard


TRAIL = """\
> ⚠️ **This answer shipped unreviewed** — the revision budget was spent before any review ran.

**Research trail** — 46 searches (46 unique query strings), 36 pages read (3 returned almost nothing), 19m of research, reviewer: budget_spent

> ⚠️ 5 of 39 link(s) cited above appear nowhere in this run - no search returned them and no page was opened at them: https://a.example/x
> ⚠️ 10 of 39 link(s) cited above were returned by a search but never opened, so nothing here rests on reading them: https://b.example/y

<details><summary>Pages read</summary>

- https://huggingface.co/datasets/allenai/qasper (1,870 chars)
- https://github.com/Future-House/litqa (6,047 chars)
- (3 fetch(es) returned almost nothing)
- (1 page(s) could not be retrieved)

</details>
"""


def test_the_trail_head_is_read_field_by_field_including_the_reviewer_verdict():
    trail, _citations, _pages = parse_trail(TRAIL.split("\n"))
    assert trail["searches"] == 46
    assert trail["unique_queries"] == 46
    assert trail["pages_read"] == 36
    assert trail["thin_pages"] == 3
    assert trail["research_minutes"] == 19
    assert trail["reviewer"] == "budget_spent"
    assert "revision budget" in trail["unreviewed"]


def test_the_two_citation_warnings_are_counted_apart():
    _trail, citations, _pages = parse_trail(TRAIL.split("\n"))
    assert citations == {"cited": 39, "never_surfaced": 5, "unopened": 10}


def test_a_clean_grounding_line_records_the_denominator_and_no_defect():
    _trail, citations, _pages = parse_trail(["> ✓ All 12 cited links were opened during this research."])
    assert citations == {"cited": 12, "all_opened": True}


def test_a_schemeless_citation_count_is_read_off_its_own_warning():
    line = (
        "> ⚠️ No links were cited in a checkable form, but the answer names 12 source(s) "
        "without a URL scheme (e.g. `github.com/THUDM/LongBench`), so the grounding check "
        "could not read them. Nothing above was verified."
    )
    _trail, citations, _pages = parse_trail([line])
    assert citations == {"schemeless": 12}


def test_fetch_depth_is_summarised_from_the_listed_pages():
    _trail, _citations, pages = parse_trail(TRAIL.split("\n"))
    assert pages["listed"] == 2
    assert pages["chars_min"] == 1870
    assert pages["chars_max"] == 6047
    assert pages["thin_unlisted"] == 3
    assert pages["failed"] == 1


def test_a_report_with_no_appendix_reads_as_unrecorded_rather_than_empty(tmp_path):
    report = tmp_path / "competitor.md"
    report.write_text("## Conclusion\n\n" + RANKED_TABLE, encoding="utf-8")
    result = audit(report)
    assert result.trail == {}
    assert "no_research_trail" in {f.kind for f in result.findings}
    # The body checks still run: this is what makes two vendors' reports comparable.
    assert "estimate_in_quantitative_column" in {f.kind for f in result.findings}


def test_prose_in_front_of_the_first_heading_is_reported(tmp_path):
    report = tmp_path / "leak.md"
    report.write_text(
        "Both flagged citations are now verified. Here is the corrected report.\n\n## Answer\n\nx\n", "utf-8"
    )
    result = audit(report)
    (finding,) = [f for f in result.findings if f.kind == "preamble_before_first_heading"]
    assert "corrected report" in finding.detail
    assert not finding.hard


def test_a_missing_template_section_is_named(tmp_path):
    report = tmp_path / "partial.md"
    report.write_text("## Answer\n\nx\n\n## Findings\n\ny\n", encoding="utf-8")
    result = audit(report)
    (finding,) = [f for f in result.findings if f.kind == "missing_template_section"]
    assert finding.detail == "Limitations"


def test_an_appendix_heading_is_not_counted_as_a_body_section(tmp_path):
    report = tmp_path / "full.md"
    report.write_text("## Answer\n\nx\n\n## Findings\n\ny\n\n## Limitations\n\nz\n\n" + TRAIL, encoding="utf-8")
    result = audit(report)
    assert result.sections == ["Answer", "Findings", "Limitations"]
    assert result.extra_sections == []


def test_strict_fails_on_a_hard_finding_and_passes_without_one(tmp_path, capsys):
    dirty = tmp_path / "dirty.md"
    dirty.write_text("## Answer\n\nx\n\n## Findings\n\n" + RANKED_TABLE + "\n## Limitations\n\nz\n", encoding="utf-8")
    clean = tmp_path / "clean.md"
    clean.write_text("## Answer\n\nx\n\n## Findings\n\ny\n\n## Limitations\n\nz\n", encoding="utf-8")

    assert main([str(dirty), "--strict"]) == 1
    assert main([str(clean), "--strict"]) == 0


def test_the_json_shape_carries_the_hard_count_for_a_batch(tmp_path, capsys):
    report = tmp_path / "r.md"
    report.write_text("## Answer\n\nx\n\n## Findings\n\n" + RANKED_TABLE, encoding="utf-8")
    assert main([str(report), "--json"]) == 0
    (payload,) = json.loads(capsys.readouterr().out)
    assert payload["hard_findings"] == 1
    assert payload["tables"][0]["width"] == 5


def test_a_missing_file_is_an_error_not_a_traceback(tmp_path, capsys):
    assert main([str(tmp_path / "nope.md")]) == 2
    assert "no such report" in capsys.readouterr().err


FULL_REPORT = (
    "## Answer\n\nQASPER and LongCodeQA.\n\n## Findings\n\n"
    + RANKED_TABLE
    + "\n| Method | Benchmarks used | Ratio |\n|---|---|---|\n"
    + "| AutoCompressors | summarization/QA (paper; not fetched) | ~4x |\n"
    + "\n## Limitations\n\nLongCodeQA size unconfirmed.\n\n"
    + TRAIL
)


def test_the_human_summary_names_the_trail_the_depth_and_both_finding_classes(tmp_path, capsys):
    report = tmp_path / "full.md"
    report.write_text(FULL_REPORT, encoding="utf-8")
    assert main([str(report)]) == 0
    out = capsys.readouterr().out

    assert "46 searches (46 unique), 36 pages read, 3 thin, 19m, reviewer: budget_spent" in out
    assert "median 3,958 chars" in out
    assert "cited=39" in out and "unopened=10" in out
    assert "sections:   Answer, Findings, Limitations" in out
    # Hard findings lead with '!', soft ones with '-', so a reader can stop after the first block.
    assert "     ! citation_never_opened" in out
    assert "     - unfetched_table_row" in out
    assert "     - shipped_unreviewed" in out


def test_a_report_without_an_appendix_says_the_trail_was_not_recorded(tmp_path, capsys):
    report = tmp_path / "bare.md"
    report.write_text("## Answer\n\nx\n", encoding="utf-8")
    main([str(report)])
    assert "trail:      not recorded (no process appendix)" in capsys.readouterr().out


def test_a_fence_tag_citation_and_a_schemeless_one_are_each_their_own_finding(tmp_path):
    report = tmp_path / "fenced.md"
    report.write_text(
        "## Answer\n\nx\n\n## Findings\n\ny\n\n## Limitations\n\nz\n\n"
        "**Research trail** - 3 searches (3 unique query strings), 2 pages read\n\n"
        "> ⚠️ No links were cited, but the answer references 75 tool-result fence tag(s) "
        "(e.g. `web_fetch #cd8187b0`). A fence tag is a data boundary, not a URL - the "
        "grounding check cannot resolve it to a page. Nothing above was verified.\n"
        "> ⚠️ No links were cited in a checkable form, but the answer names 8 source(s) "
        "without a URL scheme (e.g. `github.com/THUDM/LongBench`), so the grounding check "
        "could not read them. Nothing above was verified.\n",
        encoding="utf-8",
    )
    kinds = {f.kind for f in audit(report).findings}
    assert "citation_is_fence_tag" in kinds
    assert "citation_without_scheme" in kinds


def test_a_ragged_table_is_reported_with_the_row_count(tmp_path):
    report = tmp_path / "ragged.md"
    report.write_text(
        "## Answer\n\nx\n\n## Findings\n\n| A | B | C |\n|---|---|---|\n| 1 | 2 |\n\n## Limitations\n\nz\n",
        encoding="utf-8",
    )
    (finding,) = [f for f in audit(report).findings if f.kind == "ragged_table"]
    assert "1 row(s) off 3 columns" in finding.detail


# ---------------------------------------------------------------------------
# Table extraction over ordinary Markdown, not just the shape our own flow emits
# ---------------------------------------------------------------------------

NO_OUTER_PIPES = """\
# | Benchmark | Total | Size
--- | --- | :-: | ---
1 | QASPER | 22 | 5,049 q
2 | CrossCodeEval | 16 | 4 langs, ~2k (est.)
"""


def test_a_table_written_without_outer_pipes_is_still_a_table():
    (table,) = _tables(NO_OUTER_PIPES)
    assert table.header == ["#", "Benchmark", "Total", "Size"]
    assert len(table.rows) == 2
    assert table.rows[1][1] == "CrossCodeEval"


def test_an_estimate_hides_in_no_table_shape(tmp_path):
    report = tmp_path / "loose.md"
    report.write_text("## Answer\n\nx\n\n## Findings\n\n" + NO_OUTER_PIPES, encoding="utf-8")
    assert main([str(report), "--strict"]) == 1


def test_a_table_inside_a_fenced_block_is_an_example_not_data():
    md = "Here is the shape a convention table takes:\n\n```markdown\n" + RANKED_TABLE + "```\n"
    assert _tables(md) == []
    assert check_estimates(_tables(md)) == []


def test_a_tilde_fence_hides_its_table_too():
    md = "~~~\n" + RANKED_TABLE + "~~~\n"
    assert _tables(md) == []


def test_a_real_table_after_a_fenced_example_is_still_read():
    md = "```\n" + RANKED_TABLE + "```\n\n" + RANKED_TABLE
    (table,) = _tables(md)
    assert len(table.rows) == 3


def test_an_escaped_pipe_is_cell_content_and_does_not_ragged_the_row(tmp_path):
    md = """\
| # | Benchmark | Metric | Total |
|---|-----------|---|:--:|
| 1 | QASPER | F1 \\| EM | 22 |
"""
    (table,) = _tables(md)
    assert table.rows[0] == ["1", "QASPER", "F1 | EM", "22"]
    report = tmp_path / "escaped.md"
    report.write_text("## Answer\n\nx\n\n## Findings\n\n" + md + "\n## Limitations\n\nz\n", encoding="utf-8")
    assert "ragged_table" not in {f.kind for f in audit(report).findings}


def test_prose_with_a_pipe_over_a_dashed_line_is_not_a_table():
    # The delimiter must have the header's width, which is what keeps a sentence
    # followed by a horizontal rule from being read as a one-column table.
    md = "The ratio is 4x | 8x depending on the arm.\n---\n"
    assert _tables(md) == []


# ---------------------------------------------------------------------------
# The producer's other grounding outcomes
# ---------------------------------------------------------------------------


def _with_appendix(tmp_path, name, warning, head="46 searches (46 unique query strings), 12 pages read"):
    report = tmp_path / name
    report.write_text(
        f"## Answer\n\nx\n\n## Findings\n\ny\n\n## Limitations\n\nz\n\n**Research trail** - {head}\n\n{warning}\n",
        encoding="utf-8",
    )
    return report


def test_pages_read_and_nothing_cited_is_a_hard_finding(tmp_path):
    report = _with_appendix(
        tmp_path,
        "untraceable.md",
        "> ⚠️ 12 page(s) were read but the answer cites no link, so nothing above can be traced to a source.",
    )
    result = audit(report)
    assert result.citations["cited"] == 0
    assert result.citations["read_but_cited_nothing"] == 12
    (finding,) = [f for f in result.findings if f.kind == "answer_cites_nothing"]
    assert finding.hard
    assert "12 page(s) were read" in finding.detail


def test_citing_nothing_with_nothing_opened_is_also_hard(tmp_path):
    report = _with_appendix(
        tmp_path,
        "empty.md",
        "> No links were cited above, so there was nothing to check.",
        head="3 searches (3 unique query strings), 0 pages read",
    )
    (finding,) = [f for f in audit(report).findings if f.kind == "answer_cites_nothing"]
    assert finding.hard
    assert "no page was opened" in finding.detail


def test_an_answer_that_cites_its_sources_is_not_accused_of_citing_nothing(tmp_path):
    report = _with_appendix(
        tmp_path,
        "clean.md",
        "> ✓ All 12 cited links were opened during this research.",
    )
    kinds = {f.kind for f in audit(report).findings}
    assert "answer_cites_nothing" not in kinds


# ---------------------------------------------------------------------------
# The page listing is capped, so its median is a prefix
# ---------------------------------------------------------------------------

TRUNCATED_PAGES = """\
**Research trail** - 60 searches (58 unique query strings), 45 pages read (2 returned almost nothing), 40m of research, reviewer: pass

> ✓ All 30 cited links were opened during this research.

<details><summary>Queries run</summary>

- `qasper dataset size`
- … and 18 more

</details>

<details><summary>Pages read</summary>

- https://a.example/one (12,000 chars)
- https://b.example/two (9,000 chars)
- … and 41 more
- (2 fetch(es) returned almost nothing)

</details>
"""


def test_a_capped_page_listing_is_reported_as_a_prefix_not_as_the_runs_depth(tmp_path):
    report = tmp_path / "deep.md"
    report.write_text("## Answer\n\nx\n\n## Findings\n\ny\n\n## Limitations\n\nz\n\n" + TRUNCATED_PAGES, "utf-8")
    result = audit(report)
    assert result.pages["listed"] == 2
    assert result.pages["unlisted"] == 41
    assert result.pages["truncated"] is True
    assert result.pages["substantive_total"] == 43
    kinds = {f.kind for f in result.findings}
    assert "fetch_depth_from_a_truncated_list" in kinds
    # The prefix happened to be the two deepest reads; calling that the run's depth
    # would be an invented all-clear, so the shallow verdict is withheld either way.
    assert "shallow_fetch_depth" not in kinds


def test_the_queries_cap_is_not_read_as_the_pages_cap(tmp_path):
    report = tmp_path / "deep.md"
    report.write_text("## Answer\n\nx\n\n## Findings\n\ny\n\n## Limitations\n\nz\n\n" + TRUNCATED_PAGES, "utf-8")
    result = audit(report)
    assert result.trail["queries_unlisted"] == 18
    assert result.pages["unlisted"] == 41


def test_a_listing_short_of_the_head_count_is_truncated_even_with_no_marker(tmp_path):
    # The marker is the producer's; the head's own count is what survives a producer
    # that stops writing one. "pages read" counts the thin stubs, so they come off.
    report = tmp_path / "nomarker.md"
    report.write_text(
        "## Answer\n\nx\n\n## Findings\n\ny\n\n## Limitations\n\nz\n\n"
        "**Research trail** - 20 searches (20 unique query strings), 36 pages read "
        "(3 returned almost nothing), 19m of research, reviewer: pass\n\n"
        "<details><summary>Pages read</summary>\n\n"
        "- https://a.example/one (900 chars)\n- https://b.example/two (800 chars)\n\n</details>\n",
        encoding="utf-8",
    )
    result = audit(report)
    assert result.pages["truncated"] is True
    assert result.pages["substantive_total"] == 33
    assert "shallow_fetch_depth" not in {f.kind for f in result.findings}


def test_a_complete_listing_still_gets_its_depth_verdict(tmp_path):
    report = tmp_path / "complete.md"
    report.write_text(
        "## Answer\n\nx\n\n## Findings\n\ny\n\n## Limitations\n\nz\n\n"
        "**Research trail** - 4 searches (4 unique query strings), 2 pages read\n\n"
        "<details><summary>Pages read</summary>\n\n"
        "- https://a.example/one (900 chars)\n- https://b.example/two (800 chars)\n\n</details>\n",
        encoding="utf-8",
    )
    result = audit(report)
    assert not result.pages.get("truncated")
    (finding,) = [f for f in result.findings if f.kind == "shallow_fetch_depth"]
    assert "median 850 chars over 2 listed pages" in finding.detail


# ---------------------------------------------------------------------------
# Producer round trip: the parser reads what the flow actually writes
# ---------------------------------------------------------------------------

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.support.process_appendix import ResearchTrail  # noqa: E402


def _rendered(trail: ResearchTrail, tmp_path: Path, name: str = "run.md") -> Path:
    """A report whose appendix is the producer's own render, not a copied string."""
    report = tmp_path / name
    body = "## Answer\n\nx\n\n## Findings\n\ny\n\n## Limitations\n\nz\n"
    report.write_text(body + trail.render(), encoding="utf-8")
    return report


def test_the_parser_reads_the_producers_own_clean_trail(tmp_path):
    trail = ResearchTrail(
        searches=12,
        distinct_queries=[f"q{i}" for i in range(12)],
        pages=[(f"https://a.example/{i}", 5_000, True) for i in range(6)],
        cited=[f"https://a.example/{i}" for i in range(6)],
        span_seconds=900,
        verify_outcome="pass",
    )
    result = audit(_rendered(trail, tmp_path))
    assert result.trail["searches"] == 12
    assert result.trail["pages_read"] == 6
    assert result.trail["reviewer"] == "pass"
    assert result.citations == {"cited": 6, "all_opened": True}
    assert result.pages["chars_median"] == 5_000
    assert not result.pages.get("truncated")
    assert [f.kind for f in result.findings if f.hard] == []


def test_the_parser_reads_the_producers_own_untraceable_trail(tmp_path):
    """The outcome with no count in its sentence: pages read, nothing cited."""
    trail = ResearchTrail(
        searches=8,
        distinct_queries=["q"],
        pages=[(f"https://a.example/{i}", 4_000, True) for i in range(9)],
        span_seconds=600,
        verify_outcome="pass",
    )
    result = audit(_rendered(trail, tmp_path, "untraceable.md"))
    assert result.citations["cited"] == 0
    assert result.citations["read_but_cited_nothing"] == 9
    (finding,) = [f for f in result.findings if f.kind == "answer_cites_nothing"]
    assert finding.hard


def test_the_parser_sees_the_producers_own_page_cap(tmp_path):
    """41 substantive pages, so render() lists 40 and counts the rest."""
    trail = ResearchTrail(
        searches=50,
        distinct_queries=[f"q{i}" for i in range(50)],
        pages=[(f"https://a.example/{i}", 12_000, True) for i in range(41)],
        cited=["https://a.example/0"],
        span_seconds=3_000,
        verify_outcome="pass",
    )
    result = audit(_rendered(trail, tmp_path, "capped.md"))
    assert result.pages["listed"] == 40
    assert result.pages["unlisted"] == 1
    assert result.pages["substantive_total"] == 41
    kinds = {f.kind for f in result.findings}
    assert "fetch_depth_from_a_truncated_list" in kinds
    assert "shallow_fetch_depth" not in kinds


def test_the_parser_reads_the_producers_own_unreviewed_banner(tmp_path):
    trail = ResearchTrail(
        searches=5,
        distinct_queries=["q"],
        pages=[("https://a.example/1", 2_000, True)],
        cited=["https://a.example/1"],
        span_seconds=120,
        verify_outcome="budget_spent",
    )
    result = audit(_rendered(trail, tmp_path, "unreviewed.md"))
    assert result.trail["reviewer"] == "budget_spent"
    assert "revision budget" in result.trail["unreviewed"]
    assert "shipped_unreviewed" in {f.kind for f in result.findings}


# ---------------------------------------------------------------------------
# Fence tracking, GFM's own rules: same marker, no shorter, nothing trailing
# ---------------------------------------------------------------------------


def test_a_four_backtick_fence_survives_a_triple_backtick_example_inside_it():
    """The shape that made an example table into report data.

    A convention table is often shown inside a four-backtick block precisely so the
    example can contain its own triple-backtick fence. Toggling on any fence-shaped line
    re-enters the document at that inner fence, and everything after it is scanned.
    """
    md = "````markdown\n```\n" + RANKED_TABLE + "```\n````\n"
    assert _tables(md) == []
    assert check_estimates(_tables(md)) == []


def test_a_tilde_line_does_not_close_a_backtick_fence():
    md = "```\n~~~\n" + RANKED_TABLE + "~~~\n```\n"
    assert _tables(md) == []


def test_a_backtick_line_does_not_close_a_tilde_fence():
    md = "~~~\n```\n" + RANKED_TABLE + "```\n~~~\n"
    assert _tables(md) == []


def test_a_shorter_run_does_not_close_a_longer_fence():
    md = "`````\n```\n" + RANKED_TABLE + "```\n`````\n"
    assert _tables(md) == []


def test_a_longer_run_does_close_a_shorter_fence():
    # GFM: the closer must be at least the opener's length, not exactly it.
    md = "```\nx\n`````\n\n" + RANKED_TABLE
    (table,) = _tables(md)
    assert len(table.rows) == 3


def test_a_closing_fence_may_not_carry_an_info_string():
    md = "```\n``` json\n" + RANKED_TABLE + "```\n"
    assert _tables(md) == []


def test_an_info_string_still_opens_a_fence_and_the_close_ends_it():
    md = "```markdown\n" + RANKED_TABLE + "```\n\n" + NO_OUTER_PIPES
    (table,) = _tables(md)
    assert table.header == ["#", "Benchmark", "Total", "Size"]


def test_a_backtick_in_an_info_string_is_inline_code_not_a_fence():
    # A line that opens with backticks and closes them again on the same line is inline
    # code in a paragraph, not a code block - GFM forbids a backtick in the info string.
    # Treating it as an opener would mask the rest of the report and silence every
    # finding after it, which is the failure direction that matters here.
    md = "```est.``` is how that cell reads.\n\n" + RANKED_TABLE
    assert _fenced(md.split("\n"))[0] is False
    (table,) = _tables(md)
    assert len(table.rows) == 3


def test_an_unclosed_fence_masks_the_rest_of_the_report():
    # GFM ends an unclosed fence at the end of the document, so a table after it is
    # still inside the block; anything else would read code as data.
    md = "```\n" + RANKED_TABLE
    assert _tables(md) == []


def test_an_indented_fence_up_to_three_spaces_is_still_a_fence():
    md = "   ```\n" + RANKED_TABLE + "   ```\n"
    assert _tables(md) == []


# ---------------------------------------------------------------------------
# Rank and total are found by shape, so a report in any language keeps the check
# ---------------------------------------------------------------------------

UNNAMED_TOTAL = """\
| # | Benchmark | Genre | Depth | Reuse | Zzz | Notes |
|---|-----------|:--:|:--:|:--:|:--:|---|
| 1 | QASPER | 5 | 4 | 4 | 13 | in genre |
| 2 | LV-Eval | 4 | 4 | 4 | 12 | denoiser |
| 3 | MuSiQue | 3 | 4 | 4 | 11 | RAG lane |
| 4 | HotpotQA | 3 | 3 | 4 | 10 | short hops |
| 5 | Loong | 4 | 4 | 4 | 12 | multi-doc |
"""


def test_a_total_column_is_found_by_adding_up_not_by_its_header():
    (table,) = _tables(UNNAMED_TOTAL)
    quant = sorted(set(quantitative_columns(table)))
    rank = rank_column(table)
    assert rank == 0
    # 'Zzz' names nothing, and is still the total: it is the sum of the run beside it.
    assert total_column(table, quant, rank) == table.header.index("Zzz")


def test_the_shape_check_catches_an_inversion_under_an_unnamed_total():
    (finding,) = check_ranking(_tables(UNNAMED_TOTAL))
    assert finding.kind == "rank_contradicts_total"
    assert "Loong" in finding.detail and "HotpotQA" in finding.detail


def test_a_column_that_does_not_add_up_is_not_taken_for_a_total():
    md = """\
| # | Benchmark | Genre | Depth | Pages |
|---|-----------|:--:|:--:|:--:|
| 1 | QASPER | 5 | 4 | 1585 |
| 2 | LV-Eval | 4 | 5 | 1331 |
| 3 | MuSiQue | 3 | 4 | 25000 |
"""
    (table,) = _tables(md)
    quant = sorted(set(quantitative_columns(table)))
    assert total_column(table, quant, rank_column(table)) is None
    assert check_ranking([table]) == []


def test_a_column_of_years_is_not_a_rank():
    """The false positive that made a yearly breakdown into two hard findings.

    `Year | North | South | Total` is an unbroken ascending run in the first column beside
    a column that really is the sum of the two before it, so everything except the
    starting value looks like a scored ranking - and its totals climb, which then read as
    contradictions. Ordinary research-report content, and the reason a rank has to begin
    where a rank begins.
    """
    md = """\
| Year | North | South | Total |
|------|:--:|:--:|:--:|
| 2024 | 10 | 12 | 22 |
| 2025 | 14 | 15 | 29 |
| 2026 | 18 | 19 | 37 |
| 2027 | 21 | 24 | 45 |
"""
    (table,) = _tables(md)
    assert rank_column(table) is None
    assert check_ranking([table]) == []
    assert total_column(table, sorted(set(quantitative_columns(table))), None) == 3


def test_an_index_column_beside_totals_that_merely_vary_is_not_a_ranking():
    """A numbered list is not a ranking, and its rises are not contradictions.

    The rank shape alone cannot tell the two apart; what can is whether the table is
    already ordered by the total. Here it is not, so nothing is reported - reporting the
    rises would put a hard finding on every indexed breakdown that happens to be numbered.
    """
    md = """\
| # | Region | North | South | Total |
|---|--------|:--:|:--:|:--:|
| 1 | East | 4 | 5 | 9 |
| 2 | West | 9 | 8 | 17 |
| 3 | North | 2 | 3 | 5 |
| 4 | South | 7 | 6 | 13 |
"""
    (table,) = _tables(md)
    assert rank_column(table) == 0
    assert check_ranking([table]) == []


def test_a_table_too_short_to_show_an_order_is_left_alone():
    md = """\
| # | Benchmark | Genre | Depth | Total |
|---|-----------|:--:|:--:|:--:|
| 1 | QASPER | 5 | 4 | 9 |
| 2 | LV-Eval | 5 | 5 | 10 |
"""
    assert check_ranking(_tables(md)) == []


def test_a_criterion_column_that_climbs_is_not_a_rank():
    md = """\
| Benchmark | Genre | Depth |
|-----------|:--:|:--:|
| QASPER | 3 | 5 |
| LV-Eval | 4 | 4 |
| MuSiQue | 5 | 3 |
"""
    (table,) = _tables(md)
    # 3, 4, 5 is consecutive and ascending, but it is not the first column, so it is a
    # criterion; reading it as a rank would invent an inversion on every scored table.
    assert rank_column(table) is None


def test_every_marker_the_glossary_names_is_still_recognised():
    """The non-ASCII markers are codepoint escapes, so nothing in the file reads them.

    The glossary is what a reader gets instead, and this asserts the two stay in step: a
    marker that stops firing, or one added without an entry, fails here. "derived" is the
    one entry that is not an estimate - it is the exemption - so it is checked from the
    other side: it must suppress the soft finding an approximation would otherwise raise.
    """
    for literal, meaning in MARKER_GLOSSARY.items():
        if meaning == "derived":
            assert DERIVED_MARKERS.search(f"~5 ({literal})")
            md = "| # | Benchmark | Queries |\n|---|---|---|\n" + f"| 1 | X | ~5 ({literal}) |" + "\n"
            assert check_estimates(_tables(md)) == []
            continue
        cell = f"| 1 | X | 12 {literal}5 |" if meaning == "approximately" else f"| 1 | X | 12 ({literal}) |"
        md = "| # | Benchmark | Size |\n|---|---|---|\n" + cell + "\n"
        findings = check_estimates(_tables(md))
        assert findings, f"{meaning} ({literal!r}) is in the glossary but matches nothing"
        assert findings[0].hard, f"{meaning} is a self-declared estimate and must be hard"


def test_a_four_row_ranking_with_one_exception_is_still_caught():
    """The share-based threshold could not see this, and a four-candidate table is
    ordinary report content.

    Three adjacent pairs mean one exception is a third of them, so no share threshold can
    both catch this and keep out an indexed table whose totals merely vary. Over all six
    pairs the order is plain: five fall or tie, one rises.
    """
    md = """\
| Rank | Candidate | Accuracy | Depth | Total |
|------|-----------|:--:|:--:|:--:|
| 1 | QASPER | 5 | 5 | 10 |
| 2 | LV-Eval | 5 | 4 | 9 |
| 3 | RepoQA | 5 | 5 | 10 |
| 4 | L-Eval | 4 | 3 | 7 |
"""
    (finding,) = check_ranking(_tables(md))
    assert finding.kind == "rank_contradicts_total"
    assert finding.hard
    assert "RepoQA" in finding.detail and "LV-Eval" in finding.detail


def test_the_ordering_measure_counts_rows_that_would_have_to_move():
    assert rows_out_of_order([5, 4, 3, 2]) == 0
    assert rows_out_of_order([3, 3, 3]) == 0
    assert rows_out_of_order([2, 3, 4, 5]) == 3
    # One misplaced candidate, with and without tied leaders. A rank correlation read the
    # second of these as disorder, because the tie sits in its denominator and the one
    # displaced row costs it two pairs.
    assert rows_out_of_order([10, 9, 10, 7]) == 1
    assert rows_out_of_order([10, 10, 11, 9]) == 1
    # An indexed breakdown whose totals merely vary is three moves from sorted.
    assert rows_out_of_order([9, 17, 5, 13]) == 2


def test_a_single_misplaced_candidate_is_caught_even_with_tied_totals():
    """The second four-row reproduction: `10, 10, 11, 9`, one row out of place.

    Its two leaders tie, which is ordinary for a rubric total, and row three belongs at
    the top. One move sorts the table, so the exception is reported.
    """
    md = """\
| Rank | Candidate | Accuracy | Depth | Total |
|------|-----------|:--:|:--:|:--:|
| 1 | QASPER | 5 | 5 | 10 |
| 2 | LV-Eval | 6 | 4 | 10 |
| 3 | RepoQA | 6 | 5 | 11 |
| 4 | L-Eval | 5 | 4 | 9 |
"""
    (finding,) = check_ranking(_tables(md))
    assert finding.hard
    assert "RepoQA" in finding.detail and "LV-Eval" in finding.detail


def test_a_long_shortlist_tolerates_a_second_exception_a_short_one_does_not():
    """One row out of place, or a tenth of a long table.

    A twenty-row shortlist with two rows misplaced is still a shortlist somebody sorted;
    a four-row table with two is not a ranking at all.
    """
    assert _order_tolerance(4) == 1
    assert _order_tolerance(20) == 2
    assert _order_tolerance(100) == 10


def test_three_rows_with_one_exception_are_too_few_to_show_an_order():
    md = """\
| # | Benchmark | Genre | Depth | Total |
|---|-----------|:--:|:--:|:--:|
| 1 | QASPER | 5 | 5 | 10 |
| 2 | LV-Eval | 4 | 4 | 8 |
| 3 | RepoQA | 5 | 4 | 9 |
"""
    # Two of three pairs fall, one rises: tau is 0.33, below the bar. A single exception
    # in three rows is as consistent with an unsorted list as with a ranking.
    assert check_ranking(_tables(md)) == []


# ── the readability dimension, in the one part a parser can settle ────


def _row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _grid(headers: list[str], rows: list[list[str]]) -> str:
    lines = [_row(headers), _row(["---"] * len(headers))] + [_row(r) for r in rows]
    return "\n".join(lines) + "\n"


MATRIX = _grid(
    ["#", "Benchmark", "Genre", "Headroom", "Dispersion", "Multi-q", "Comp.", "Total", "Source/venue", "Size"],
    [
        ["1", "QASPER", "5", "4", "5", "4", "4", "22", "ACL 2021", "5,049 q"],
        ["2", "LV-Eval", "4", "4", "4", "4", "4", "20", "arXiv 2024", "1,331 QA"],
    ],
)


def test_a_matrix_that_carries_every_criterion_and_the_sources_is_named():
    """The shape our own 2026-09-04 main table had: rank, six criteria, the total, the
    venue and the size in one grid. Ten columns at 134 characters, so it is the count
    that makes it unreadable and not the width."""
    (finding,) = check_table_width(_tables(MATRIX))

    assert finding.kind == "too_many_compared_columns"
    assert finding.hard is False
    assert "10 compared column(s)" in finding.detail
    # The two to move, which is the whole point of naming them.
    assert "last two: Source/venue, Size" in finding.detail


def test_eight_columns_is_the_most_a_row_carries():
    """The cap is a boundary, so both sides of it are pinned. Every ordinary comparison
    table in the 2026-09-04 corpus sits at eight or below."""
    eight = _grid([f"c{i}" for i in range(8)], [[str(i) for i in range(8)]])
    nine = _grid([f"c{i}" for i in range(9)], [[str(i) for i in range(9)]])

    assert check_table_width(_tables(eight)) == []
    assert len(check_table_width(_tables(nine))) == 1


def test_a_source_column_of_urls_does_not_count_against_the_cap():
    """The template asks for full URLs, and the eye goes down a source column rather than
    along it. Charging a table for being properly sourced would put the audit at odds with
    the contract the report is written to."""
    headers = [f"c{i}" for i in range(8)] + ["Source"]
    rows = [
        [str(i) for i in range(8)] + ["https://arxiv.org/abs/2401.12345"],
        [str(i) for i in range(8)] + ["https://aclanthology.org/2021.acl-long.1/"],
    ]

    findings = check_table_width(_tables(_grid(headers, rows)))

    assert findings == [], "a ninth column of bare URLs is not a ninth compared column"


def test_the_link_columns_are_still_counted_in_the_finding():
    headers = [f"c{i}" for i in range(9)] + ["Source"]
    rows = [[str(i) for i in range(9)] + ["https://arxiv.org/abs/2401.12345"]] * 2

    (finding,) = check_table_width(_tables(_grid(headers, rows)))

    assert "9 compared column(s), 1 link column(s)" in finding.detail


def test_a_column_of_inline_links_is_compared_at_the_width_of_its_text():
    """An inline link renders as its own text, so it is a column the reader does compare -
    and its width is the text's, not the address's."""
    headers = [f"c{i}" for i in range(9)] + ["Venue"]
    rows = [[str(i) for i in range(9)] + ["[ACL 2021](https://aclanthology.org/2021.acl-long.1/)"]] * 2

    (finding,) = check_table_width(_tables(_grid(headers, rows)))

    assert "10 compared column(s)" in finding.detail
    assert "link column" not in finding.detail
    # The link cell counts as its eight characters of text, not its fifty-three of address.
    assert ", 57 chars wide;" in finding.detail


def test_a_ragged_row_does_not_break_the_width():
    """``parse_tables`` keeps a short row as it is so ``ragged_table`` can report it, so
    every column reader has to index defensively - including this one."""
    md = _grid([f"c{i}" for i in range(10)], [[str(i) for i in range(10)]])
    md += _row(["1", "2"]) + "\n"

    (finding,) = check_table_width(_tables(md))

    assert "10 compared column(s)" in finding.detail


def test_the_width_finding_reaches_the_report(tmp_path):
    report = tmp_path / "r.md"
    report.write_text(f"## Answer\n\nyes\n\n## Findings\n\n{MATRIX}\n## Limitations\n\nnone\n", encoding="utf-8")

    kinds = [f.kind for f in audit(report).findings]

    assert "too_many_compared_columns" in kinds


def test_a_sourced_criterion_column_is_compared_not_exempt():
    """The exemption is whole-cell, and this is why.

    The report template requires every specific number to carry its full source, so a
    criterion cell reads `5 https://example.com/source`. Testing for a URL anywhere in
    the cell excused eight columns of a ten-column matrix, and the widest tables in the
    corpus escaped this check by being properly sourced. A cell carrying a value AND its
    source is a compared column: the value is what the reader reads across.
    """
    headers = ["#", "Benchmark"] + [f"c{i}" for i in range(7)] + ["Total"]
    rows = [
        ["1", "QASPER"] + ["5 https://example.com/source"] * 7 + ["35"],
        ["2", "LV-Eval"] + ["4 https://example.com/other"] * 7 + ["28"],
    ]
    (table,) = _tables(_grid(headers, rows))

    assert link_columns(table) == []
    (finding,) = check_table_width([table])
    assert "10 compared column(s)" in finding.detail
    assert "link column" not in finding.detail


def test_a_cell_of_nothing_but_urls_is_still_exempt():
    """Narrowing the test must not lose the case it was written for: a dedicated source
    column, including one carrying two addresses for the same row."""
    headers = [f"c{i}" for i in range(8)] + ["Source"]
    one = [[str(i) for i in range(8)] + ["https://arxiv.org/abs/2401.12345"]] * 2
    two = [[str(i) for i in range(8)] + ["https://a.example/x https://b.example/y"]] * 2

    assert link_columns(_tables(_grid(headers, one))[0]) == [8]
    assert link_columns(_tables(_grid(headers, two))[0]) == [8]
    assert check_table_width(_tables(_grid(headers, one))) == []
