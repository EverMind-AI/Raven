"""The report's reading page: the delivered report as one HTML file beside the reply.

The reply stays markdown and the conversation shows it as markdown. The page is a second
form of the same text, written at turn end. These tests hold its three promises: it shows
nothing the reply does not hold, it cannot run or load anything, and a turn that cannot
write it still ends as it would have.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

import research_flow.flow as flow_module  # noqa: E402
import research_flow.support.report_page as page_module  # noqa: E402
from research_flow.config import FlowConfig  # noqa: E402
from research_flow.flow import ToolHandles, TurnFrame, build_chain  # noqa: E402
from research_flow.gates.conversation import _RESEARCH_TURN  # noqa: E402
from research_flow.gates.report_shape import LIMITS_HEADINGS  # noqa: E402
from research_flow.prompts import render_identity_and_contract, render_parts  # noqa: E402
from research_flow.state import SessionStore  # noqa: E402
from research_flow.support.report_highlights import _SYSTEM, check_highlights, select_highlights  # noqa: E402
from research_flow.support.report_html import (  # noqa: E402
    GENERATOR,
    STYLE,
    Figure,
    Highlights,
    Moment,
    close_with_sources,
    render_page,
)
from research_flow.support.report_page import ReportPage, page_line, written_pages  # noqa: E402

from raven.agent import workdir  # noqa: E402
from raven.agent.loop._shared import _appended_by_hook  # noqa: E402
from raven.contracts.loop_hooks import AgentHookContext  # noqa: E402
from raven.i18n import t_in  # noqa: E402

_REPORT = (
    "> Use Vite: it starts in 1.2 s where webpack takes 8.4 s.\n\n"
    "## What the sources show\n"
    "Vite starts in 1.2 s ([Vite docs](https://vite.dev/guide/why.html)) and webpack in 8.4 s "
    "([webpack docs](https://webpack.js.org/concepts/)). The Vite number is repeated in "
    "[its guide](https://vite.dev/guide/why.html).\n\n"
    "| Tool | Cold start |\n|:--|--:|\n| Vite | 1.2 s |\n| webpack | 8.4 s |\n\n"
    "## Timeline\n"
    "- Vite 5.2 shipped in March 2024\n- Vite 6 shipped in November 2024\n- Vite 7 shipped in June 2025\n\n"
    "## Limitations\nNo shared benchmark on one machine was found.\n"
)
# Chinese for "the cold start takes 1.2 seconds", spelled as escapes.
_ZH_SENTENCE = "\u51b7\u542f\u52a8\u8017\u65f6 1.2 \u79d2"
_ZH_REPORT = f"> {_ZH_SENTENCE}\n\n## \u4e00\u3001\u901f\u5ea6\n{_ZH_SENTENCE}\n\n## \u672a\u80fd\u6838\u5b9e\n\u65e0\n"
_TRAIL = "\n---\n\n**Research trail** — 3 searches (3 unique query strings), 5 pages read\n"
# A trail that warns, as ``ResearchTrail.render`` writes it.
_WARNED_TRAIL = _TRAIL + "\n> \u26a0\ufe0f 1 of 2 link(s) cited above were never opened: https://listed.example/b\n"


# --------------------------------------------------------------------------- #
# The page                                                                     #
# --------------------------------------------------------------------------- #


def test_the_page_lays_out_the_reader_layouts_three_parts():
    page = render_page("# Vite or webpack\n\n" + _REPORT, day="2026-11-19")
    assert "<h1>Vite or webpack</h1>" in page
    assert '<div class="answer"><p>Use Vite: it starts in 1.2 s where webpack takes 8.4 s.</p></div>' in page
    assert '<section class="section" id="section-1"><h2>What the sources show</h2><div class="panel">' in page
    assert '<section class="section" id="section-3"><h2>Limitations</h2><div class="panel is-limits">' in page
    assert "Research report · 2026-11-19" in page
    assert "<title>Vite or webpack</title>" in page


def test_a_report_without_a_title_leads_with_its_answer():
    page = render_page(_REPORT, title="")
    assert "<h1>" not in page
    assert '<div class="answer">' in page
    # The answer reads in the body's size with or without a title above it.
    inner = re.findall(r"\.answer[^{,]+\{([^}]*)\}", STYLE)
    assert inner and not any("font-size" in rule for rule in inner)
    assert "<title>Use Vite: it starts in 1.2 s where webpack takes 8.4 s.</title>" in page


def test_the_title_of_the_report_file_is_used_when_the_reply_has_none():
    assert "<h1>Kept from the file</h1>" in render_page(_REPORT, title="Kept from the file")


@pytest.mark.asyncio
async def test_a_report_with_no_title_is_titled_by_the_task_its_dispatcher_named(tmp_path, _no_trail):
    """A real page went out untitled, and the agent that delivered it rewrote its ``<title>`` by script."""
    request = SimpleNamespace(task_summary="Vite or\n webpack")
    frame, facts = _frame(tmp_path), {}
    with workdir.bind(tmp_path):
        await frame.before_user_inbound(
            AgentHookContext(session_key="cli:t", turn_request=request, inbound_content="compare them", metadata=facts)
        )
        await _send(frame, _REPORT, facts=facts)
    (page,) = tmp_path.glob("*.html")
    html = page.read_text(encoding="utf-8")
    assert "<h1>Vite or webpack</h1>" in html and "<title>Vite or webpack</title>" in html
    assert "dr_task_name" not in facts


@pytest.mark.asyncio
async def test_a_title_of_the_reports_own_stands_over_the_tasks_name(tmp_path):
    writer = ReportPage(_cfg().report_page)
    with workdir.bind(tmp_path):
        own, _ = await writer.write("# Its own title\n\n" + _REPORT, task="The task", day="2026-11-19")
        report = tmp_path / "vite.md"
        report.write_text("# Kept in the file\n\n" + _REPORT, encoding="utf-8")
        beside, _ = await writer.write(_REPORT, beside=str(report), task="The task", day="2026-11-19")
        untitled, _ = await writer.write(_REPORT + "\nMore.\n", day="2026-11-19")
    assert "<h1>Its own title</h1>" in pathlib.Path(own).read_text(encoding="utf-8")
    assert "<h1>Kept in the file</h1>" in pathlib.Path(beside).read_text(encoding="utf-8")
    # A turn nobody dispatched has no task name: the page leads with the answer, as before.
    assert "<h1>" not in pathlib.Path(untitled).read_text(encoding="utf-8")


def test_each_address_is_numbered_once_in_the_order_the_report_cites_it():
    page = render_page(_REPORT)
    assert page.count('<sup class="cite"><a href="#source-1">1</a></sup>') == 2
    assert page.count('<sup class="cite"><a href="#source-2">2</a></sup>') == 1
    assert (
        '<li id="source-1"><span class="source-number">1</span><span><span class="source-host">vite.dev</span>' in page
    )
    assert '<li id="source-2"><span class="source-number">2</span>' in page
    assert "source-3" not in page
    assert "Sources: 2" in page


_NUMBERED = (
    "> Use Vite.\n> It starts faster [1].\n\n"
    "## What the sources show\nVite starts in 1.2 s [1][2], see also [the guide](https://vite.dev/guide/why) "
    "and [9] and [another page](https://other.example/x).\n\n"
    "## References\n{references}\n\n"
    "## Limitations\nNo shared benchmark.\n"
)
_AS_LIST = (
    "1. Why Vite, vite.dev, https://vite.dev/guide/why\n2. [Concepts](https://webpack.js.org/concepts/), webpack.js.org"
)
_AS_LINES = "[1] Why Vite, https://vite.dev/guide/why\n[2] Concepts, https://webpack.js.org/concepts/"
_AS_BULLETS = "- [1] Why Vite, https://vite.dev/guide/why\n- [2] Concepts, https://webpack.js.org/concepts/"


@pytest.mark.parametrize("references", [_AS_LIST, _AS_LINES, _AS_BULLETS], ids=["ordered_list", "lines", "bullets"])
def test_a_report_with_its_own_reference_list_keeps_its_numbers(references):
    """The report numbered its sources itself: its marks link into its list, and the page adds no second one."""
    page = render_page(_NUMBERED.format(references=references))
    cite = '<sup class="cite"><a href="#source-{0}">{0}</a></sup>'
    assert f"1.2 s {cite.format(1)}{cite.format(2)}, see also" in page
    assert f"faster {cite.format(1)}." in page
    # A link to a listed address takes the list's number; one outside the list takes none.
    assert (
        f'href="https://vite.dev/guide/why" target="_blank" rel="noopener noreferrer">the guide</a>{cite.format(1)}'
        in page
    )
    assert 'rel="noopener noreferrer">another page</a>.' in page
    # A mark the list does not hold stays the text it was.
    assert "and [9] and" in page
    assert page.count('<li id="source-1">') == 1 and page.count('<li id="source-2">') == 1
    assert 'id="sources"' not in page and "Sources: 2" in page
    assert '<section class="section" id="section-3"><h2>Limitations</h2><div class="panel is-limits">' in page


def test_a_reference_list_after_the_limits_does_not_take_their_place():
    report = "> a\n\n## One\nb [1]\n\n## Limitations\nc\n\n## References\n" + _AS_LIST + "\n"
    page = render_page(report)
    assert '<section class="section" id="section-2"><h2>Limitations</h2><div class="panel is-limits">' in page
    assert '<section class="section" id="section-3"><h2>References</h2><div class="panel"><ol class="sources">' in page


def test_without_the_limits_the_page_leaves_that_section_out():
    page = render_page(_REPORT, show_limits=False)
    assert "<h2>Limitations</h2>" not in page and "No shared benchmark" not in page
    assert "<h2>What the sources show</h2>" in page and "<h2>Timeline</h2>" in page
    assert 'href="#section-3"' not in page
    # The Chinese fixed heading goes too, before a list of sources.
    zh_limits = "\u672a\u6838\u9a8c\u9879"
    report = f"> a\n\n## One\nb https://a.example/x\n\n## {zh_limits}\nc\n\n## Sources\n- https://a.example/x\n"
    page = render_page(report, language="zh", show_limits=False)
    assert zh_limits not in page and "<h2>One</h2>" in page and 'class="sources"' in page


def test_sources_written_one_per_line_are_the_pages_list_and_the_limits_still_go():
    """A sources section of bare lines, no list marker: one list on the page, and the limits before it go."""
    zh_limits = "\u672a\u6838\u9a8c\u9879"
    report = (
        "> a\n\n## One\nb\n\n"
        f"## {zh_limits}\nc\n\n"
        "## Sources\n"
        "Example, Title A, https://a.example/x\n"
        "Example, Title B, https://b.example/y\n"
    )
    page = render_page(report, language="zh", show_limits=False)
    # The report's own list is the page's: no second list under the page's own heading.
    assert 'id="sources"' not in page and page.count('class="sources"') == 1 and page.count('id="source-') == 2
    assert zh_limits not in page and "<h2>One</h2>" in page
    assert close_with_sources(report, "zh") == (report, {})


@pytest.mark.parametrize("path", ["/data/notes.md", "/notes.md", "C:\\notes\\a.md", "[notes](/data/notes.md)"])
def test_sources_one_per_line_may_name_a_local_file_beside_the_addresses(path):
    """A line that cites the user's file, not a page, is still a line of the list: one sources
    heading on the page, and the limits before it go."""
    report = (
        "> a\n\n## One\nb\n\n## Limitations\nc\n\n## Sources\n"
        f"Example, Title A, https://a.example/x\nLocal notes, {path}\n"
    )
    page = render_page(report, show_limits=False)
    assert page.count("<h2>Sources</h2>") <= 1 and page.count('class="sources"') == 1, page
    assert "<h2>Limitations</h2>" not in page and "<h2>One</h2>" in page


def test_body_lines_that_each_end_in_a_link_are_not_a_source_list():
    report = (
        "> a\n\n## Findings\nRevenue rose, https://a.example/x\nMargins fell, https://b.example/y\n\n"
        "## Limitations\nc\n"
    )
    page = render_page(report)
    assert "<h2>Findings</h2>" in page and 'id="sources"' in page and "Sources: 2" in page


def test_a_last_section_under_another_heading_stays_on_the_page():
    report = "> a\n\n## One\nb\n\n## Outlook\nc\n"
    assert "<h2>Outlook</h2>" in render_page(report, show_limits=False)


def test_the_page_shows_the_limits_unless_the_config_turns_them_off():
    from research_flow.config import ReportPageConfig

    assert ReportPageConfig().show_limits is True
    assert ReportPageConfig.model_validate({"showLimits": False}).show_limits is False


def test_a_numbered_list_of_steps_is_not_a_reference_list():
    report = "> a\n\n## Steps\n1. install [1]\n2. build\n3. see https://a.example/x\n\n## Limitations\nc\n"
    page = render_page(report)
    assert "install [1]" in page and "<ol><li>install" in page
    assert 'id="sources"' in page and "Sources: 1" in page


def test_steps_that_each_carry_a_link_are_not_a_reference_list():
    """Numbered entries with addresses read like references; only a mark that cites them makes them so."""
    report = (
        "> Use Vite.\n\n"
        "## Why\nIt starts faster ([Vite guide](https://vite.dev/guide/)).\n\n"
        "## How to move\n1. Install it ([install](https://vite.dev/guide/#scaffolding)).\n"
        "2. Move the config ([config](https://vite.dev/config/)).\n\n"
        "## Limitations\nNone.\n"
    )
    page = render_page(report)
    assert (
        "<ol><li>Install it" in page
        and '<ol class="sources"><li id="source-1"><span class="source-number">1</span><span>Install' not in page
    )
    # The page numbers the report's links itself, the steps' among them.
    assert 'id="sources"' in page and "Sources: 3" in page and page.count('<sup class="cite">') == 3


def test_a_closing_source_list_the_report_cites_by_link_is_the_pages_list():
    """The report lists its sources and cites them by link: the page numbers the links into that list."""
    report = (
        "> a\n\n## One\nb ([guide](https://vite.dev/guide/why/)) and [3] and ([other](https://other.example/x))\n\n"
        "## References\n" + _AS_LIST + "\n\n## Limitations\nc\n"
    )
    page = render_page(report)
    assert '<ol class="sources"><li id="source-1"><span class="source-number">1</span><span>Why Vite' in page
    # The link takes its entry's number, a closing slash apart; one the list does not hold takes none.
    assert 'rel="noopener noreferrer">guide</a><sup class="cite"><a href="#source-1">1</a></sup>' in page
    assert 'rel="noopener noreferrer">other</a>)' in page and "and [3] and" in page
    assert 'id="sources"' not in page and "Sources: 2" in page and page.count('<sup class="cite">') == 1
    assert '<section class="section" id="section-3"><h2>Limitations</h2><div class="panel is-limits">' in page


_SOURCE_TABLE = (
    "| Title | URL | Read |\n| --- | --- | --- |\n"
    "| Why Vite | https://vite.dev/guide/why | 2026-11-19 |\n"
    "| Concepts | https://webpack.js.org/concepts/ | 2026-11-19 |\n"
)
_LINK_CHECK = "| URL | Status |\n| --- | --- |\n| https://vite.dev/guide/why | 200 |\n| https://webpack.js.org/concepts/ | 200 |\n"
_TABLED = (
    "> a\n\n## One\nVite ([guide](https://vite.dev/guide/why)) and webpack "
    "([concepts](https://webpack.js.org/concepts/)) and [2].\n\n"
    "## Sources\n{sources}\n## Link check\n" + _LINK_CHECK + "\n## Limitations\nc\n"
)


def test_a_closing_source_table_is_the_pages_list_and_a_link_check_stays_as_written():
    """A real report closed with a table of sources and a table of link checks: the page listed them a third time."""
    page = render_page(_TABLED.format(sources=_SOURCE_TABLE))
    # The table stays a table; the page numbers its rows and the citations link to them.
    assert "<thead><tr><th></th><th>Title</th><th>URL</th><th>Read</th></tr></thead>" in page
    assert '<tr id="source-1"><td class="source-number">1</td><td>Why Vite</td>' in page
    assert '<tr id="source-2"><td class="source-number">2</td><td>Concepts</td>' in page
    assert 'rel="noopener noreferrer">concepts</a><sup class="cite"><a href="#source-2">2</a></sup>' in page
    # Only the links of the body are cited: an address that is a row of its own needs no mark beside it.
    assert page.count('<sup class="cite">') == 2 and 'id="sources"' not in page and "Sources: 2" in page
    # The page numbered the rows, not the report: a ``[2]`` in the text names nothing.
    assert "and [2]." in page
    check = page[page.index('id="section-3"') : page.index('id="section-4"')]
    assert "<h2>Link check</h2>" in check and "<td>200</td>" in check and "source-" not in check
    assert '<section class="section" id="section-4"><h2>Limitations</h2><div class="panel is-limits">' in page


def test_the_source_list_is_the_one_that_names_its_sources_wherever_the_link_check_stands():
    swapped = _TABLED.replace(
        "## Sources\n{sources}\n## Link check\n" + _LINK_CHECK,
        "## Link check\n" + _LINK_CHECK + "\n## Sources\n{sources}",
    )
    page = render_page(swapped.format(sources=_SOURCE_TABLE))
    assert '<tr id="source-1"><td class="source-number">1</td><td>Why Vite</td>' in page
    check = page[page.index('id="section-2"') : page.index('id="section-3"')]
    assert "<h2>Link check</h2>" in check and "source-" not in check and "cite" not in check


def test_a_link_check_beside_a_numbered_reference_list_carries_no_citations():
    report = _NUMBERED.format(references=_AS_LIST + "\n\n## Link check\n" + _LINK_CHECK)
    page = render_page(report)
    check = page[page.index("<h2>Link check</h2>") : page.index("<h2>Limitations</h2>")]
    assert "<td>200</td>" in check and "cite" not in check
    assert '<section class="section" id="section-4"><h2>Limitations</h2><div class="panel is-limits">' in page


def test_a_numbered_step_list_is_not_the_source_list_beside_a_real_one():
    """A list of steps that each open with a link reads like references; the heading says it is not."""
    report = (
        "> Use Vite.\n\n"
        "## Why\nIt starts faster [1].\n\n"
        "## How to move\n"
        "1. [Install](https://vite.dev/guide/) it first.\n"
        "2. [Configure](https://vite.dev/config/) the build.\n\n"
        "## Limitations\nNone.\n\n"
        "## Sources\n"
        "- [Vite guide](https://vite.dev/guide/)\n"
        "- [Vite config](https://vite.dev/config/)\n"
    )
    page = render_page(report)
    steps = page[page.index('id="section-2"') : page.index('id="section-3"')]
    assert "<h2>How to move</h2>" in steps and "<ol>" in steps and 'class="sources"' not in steps
    sources = page[page.index('id="section-4"') :]
    assert '<li id="source-1">' in sources and '<li id="source-2">' in sources
    # The report's list is not numbered, so a bare mark names no entry: it stays text.
    assert "faster [1]." in page


def test_a_link_check_before_the_limits_carries_no_citation_marks():
    """The contract puts the link check before the limits; it is not the page's source list."""
    report = (
        "> a\n\n## One\nVite ([guide](https://vite.dev/guide/why)) and webpack "
        "([concepts](https://webpack.js.org/concepts/)).\n\n"
        "## Link check\n" + _LINK_CHECK + "\n## Limitations\nc\n\n## Sources\n"
        "- [Vite guide](https://vite.dev/guide/why)\n"
        "- [Concepts](https://webpack.js.org/concepts/)\n"
    )
    page = render_page(report)
    check = page[page.index('id="section-2"') : page.index('id="section-3"')]
    assert "<td>200</td>" in check and "cite" not in check
    sources = page[page.index('id="section-4"') :]
    assert '<li id="source-1">' in sources and '<li id="source-2">' in sources


def test_a_source_entry_with_a_note_under_it_is_still_one_list():
    """An entry that carries a note below it is still an entry: one sources list, and the note kept."""
    report = (
        "> a\n\n## One\nVite ([guide](https://vite.dev/guide/why)) and webpack "
        "([concepts](https://webpack.js.org/concepts/)).\n\n"
        "## Limitations\nc\n\n## Sources\n"
        "1. [Why Vite](https://vite.dev/guide/why) - the guide\n"
        "   - a note on this entry\n"
        "2. [Concepts](https://webpack.js.org/concepts/)\n"
    )
    page = render_page(report)
    assert page.count("<h2>Sources</h2>") == 1 and 'id="sources"' not in page
    sources = page[page.index('id="section-3"') :]
    assert '<section class="section" id="section-3"><h2>Sources</h2><div class="panel"><ol class="sources">' in page
    assert '<li id="source-1">' in sources and '<li id="source-2">' in sources
    assert "a note on this entry" in sources


def test_a_link_whose_label_is_its_address_is_one_anchor():
    """A model cites ``[https://x](https://x)``; the page must not put an anchor inside an anchor."""
    report = (
        "> a\n\n## One\nSee [https://vite.dev/guide/why](https://vite.dev/guide/why) for details.\n\n"
        "## Limitations\nc\n"
    )
    page = render_page(report)
    body = page[page.index("<h2>One</h2>") : page.index('id="section-2"')]
    assert not re.search(r"<a [^>]*><a ", body)
    assert (
        '<a href="https://vite.dev/guide/why" target="_blank" rel="noopener noreferrer">'
        "https://vite.dev/guide/why</a>" in body
    )


def test_a_source_table_that_numbers_its_rows_keeps_its_numbers():
    table = (
        "| # | Title |\n| --- | --- |\n| [7] | [Why Vite](https://vite.dev/guide/why) |\n"
        "| [8] | [Concepts](https://webpack.js.org/concepts/) |\n"
    )
    page = render_page("> a\n\n## One\nVite starts faster [7].\n\n## Sources\n" + table + "\n## Limitations\nc\n")
    assert 'faster <sup class="cite"><a href="#source-7">7</a></sup>.' in page
    assert (
        '<tr id="source-7"><td>[7]</td>' in page and 'class="source-number"' not in page and 'id="sources"' not in page
    )


_CITED = (
    "> a\n\n## One\nVite ([guide](https://vite.dev/guide/why)) and webpack "
    "([concepts](https://webpack.js.org/concepts/)) and [2].\n\n## Sources\n{sources}\n\n## Limitations\nc\n"
)
_AS_RUNS = (
    "**Primary**: [Why Vite](https://vite.dev/guide/why) (v8, 2026-09)\uff5c[Vite releases](https://vite.dev/releases)\n\n"
    "**Secondary**\uff1a[Concepts](https://webpack.js.org/concepts/) (a guide, no data)."
)
_AS_GROUPS = (
    "### Primary\n- [Why Vite](https://vite.dev/guide/why) (v8, 2026-09)\n- [Vite releases](https://vite.dev/releases)\n\n"
    "### Secondary\n- [Concepts](https://webpack.js.org/concepts/) (a guide, no data)."
)


@pytest.mark.parametrize("sources", [_AS_RUNS, _AS_GROUPS], ids=["runs_in_paragraphs", "a_list_per_group"])
def test_sources_listed_in_several_blocks_are_one_list_numbered_through(sources):
    """A real reply listed its sources as two paragraphs of links, one per kind: the page listed them again."""
    page = render_page(_CITED.format(sources=sources))
    assert 'id="sources"' not in page and "Sources: 3" in page and page.count('<ol class="sources">') == 2
    assert '<li id="source-2"><span class="source-number">2</span><span><a href="https://vite.dev/releases"' in page
    assert (
        '<ol class="sources"><li id="source-3"><span class="source-number">3</span><span><a href="https://webpack'
        in page
    )
    # Each entry keeps what the report says of it, each group its label, and the body cites into the list.
    assert "(v8, 2026-09)</span></li>" in page and "(a guide, no data).</span>" in page and "\uff5c" not in page
    assert "Primary" in page and "Secondary" in page
    assert 'rel="noopener noreferrer">concepts</a><sup class="cite"><a href="#source-3">3</a></sup>' in page
    assert "and [2]." in page and page.count('<sup class="cite">') == 2


def test_numbered_lists_that_number_on_are_one_source_list_for_the_marks():
    """One list per kind of source, numbered through: a mark into the second list found no entry."""
    sources = (
        "**Primary**\n\n1. [Why Vite](https://vite.dev/guide/why)\n2. [Releases](https://vite.dev/releases)\n\n"
        "**Secondary**\n\n3. [Concepts](https://webpack.js.org/concepts/)"
    )
    page = render_page(
        "> a\n\n## One\nVite is faster [1] and webpack is older [3].\n\n## Sources\n"
        + sources
        + "\n\n## Limitations\nc\n"
    )
    assert 'faster <sup class="cite"><a href="#source-1">1</a></sup>' in page
    assert 'older <sup class="cite"><a href="#source-3">3</a></sup>' in page
    assert '<li id="source-3"><span class="source-number">3</span>' in page and 'id="sources"' not in page
    assert "Sources: 3" in page and page.count('<ol class="sources">') == 2
    # A list that takes a number again is another list, and stays as the report wrote it.
    again = render_page(
        "> a\n\n## One\nb [1].\n\n## Sources\n"
        + sources.replace("3. [Concepts]", "2. [Concepts]")
        + "\n\n## Limitations\nc\n"
    )
    assert "Sources: 2" in again and again.count('<ol class="sources">') == 1


@pytest.mark.parametrize(
    "closing",
    [
        # Sentences that cite: a link opens neither of them.
        "Start with the [guide](https://vite.dev/guide/why). Then read the [concepts](https://webpack.js.org/concepts/).",
        # One sentence that opens with its link, under no label.
        "[Why Vite](https://vite.dev/guide/why) and [Concepts](https://webpack.js.org/concepts/) say the same.",
    ],
    ids=["sentences_that_cite", "a_sentence_that_opens_with_a_link"],
)
def test_a_closing_section_of_sentences_is_not_a_list_of_sources(closing):
    page = render_page(_CITED.format(sources=closing))
    # The page lists the two addresses itself, and the section stays the paragraph it is.
    assert 'id="sources"' in page and page.count('<ol class="sources">') == 1 and "Sources: 2" in page


@pytest.mark.parametrize(
    "report",
    [
        # A table of findings with a source column: the report goes on after it.
        "> a\n\n## Compared\n" + _SOURCE_TABLE + "\n## Why\nb\n\n## Limitations\nc\n",
        # A closing list that holds none of the addresses the report cites.
        "> a\n\n## One\nb ([x](https://a.example/1)) ([y](https://a.example/2))\n\n"
        "## See also\n- https://b.example/1\n- https://b.example/2\n\n## Limitations\nc\n",
    ],
    ids=["a_table_in_the_body", "a_list_of_other_addresses"],
)
def test_a_listing_that_is_not_the_reports_sources_leaves_the_numbering_to_the_page(report):
    page = render_page(report)
    assert 'id="sources"' in page and "<tr id=" not in page and '<ol class="sources"><li id="source-1">' in page


def test_a_mark_that_indexes_a_name_is_not_a_citation():
    """``a[1]`` in prose is an index; a word or a number glued to a mark is how reports cite."""
    prose = "and a[1] and my_list[2] and caching[1] and 35%[2] and"
    page = render_page(_NUMBERED.format(references=_AS_LIST).replace("and [9] and", prose))
    cite = '<sup class="cite"><a href="#source-{0}">{0}</a></sup>'
    assert f"and a[1] and my_list[2] and caching{cite.format(1)} and 35%{cite.format(2)} and" in page


def test_each_line_of_the_answer_is_its_own_paragraph():
    """The contract writes the answer one statement to a line; joined, they read as a wall of text."""
    page = render_page(
        "> Use Vite.\n> It starts in 1.2 s.\n> Retrieved 2026-11-19.\n\n## One\nb\n\n## Limitations\nc\n"
    )
    assert '<div class="answer"><p>Use Vite.</p><p>It starts in 1.2 s.</p><p>Retrieved 2026-11-19.</p></div>' in page


def test_the_contents_list_the_sections_from_three_sections_up():
    page = render_page(_REPORT)
    assert '<nav class="contents"' in page and '<a href="#section-2">Timeline</a>' in page
    assert '<a href="#sources">Sources</a>' in page
    two = render_page("> a\n\n## One\nb\n\n## Limitations\nc\n")
    assert '<nav class="contents"' not in two and 'class="page is-single"' in two


def test_the_page_can_run_nothing_and_load_nothing():
    hostile = (
        "> answer <script>alert(1)</script>\n\n"
        "## Part\n[click](javascript:alert(1)) and ![pixel](https://tracker.example/p.gif) and "
        '<img src=x onerror=alert(1)> and [quote](https://a.example/"onmouseover="x)\n\n'
        "## Limitations\nnone\n"
    )
    page = render_page(hostile)
    assert "<script" not in page and "<img" not in page and "javascript:" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "default-src 'none'" in page and "img-src" not in page
    # Every address the page holds is an anchor's, quoted whole: nothing loads by itself.
    assert set(re.findall(r'<a (?:class="source-link" )?href="([^"]*)"', page)) == {
        "#source-1",
        "#source-2",
        "https://tracker.example/p.gif",
        "https://a.example/&quot;onmouseover=&quot;x",
    }
    assert " src=" not in page.replace("&lt;img src=x", "")


def test_a_line_before_the_answer_does_not_take_the_answers_place():
    """The cover carries the answer also when the reply opens with a line of its own."""
    page = render_page("Both files are saved. Here is the report.\n\n" + _REPORT)
    cover = page.split("</header>")[0]
    assert '<div class="answer"><p>Use Vite: it starts in 1.2 s where webpack takes 8.4 s.</p></div>' in cover
    assert '<div class="preface"><p>Both files are saved. Here is the report.</p></div>' in cover
    assert "<blockquote>" not in page

    titled = render_page("Saved.\n\n# Vite or webpack\n\n" + _REPORT)
    assert "<h1>Vite or webpack</h1>" in titled and '<div class="answer">' in titled
    assert '<div class="preface"><p>Saved.</p></div>' in titled


def test_a_quote_under_the_first_section_is_not_the_answer():
    page = render_page("The answer, as prose.\n\n## One\n> a page said so\n\n## Limitations\nc\n")
    assert 'class="answer' not in page and 'class="preface"' not in page
    assert '<aside class="note"><p>a page said so</p></aside>' in page and "<p>The answer, as prose.</p>" in page


def test_the_page_has_a_palette_for_a_dark_screen():
    """A browser that darkens a page with no dark palette inverts it, and the panels come out as black blocks."""
    page = render_page(_REPORT)
    assert '<meta name="color-scheme" content="light dark">' in page
    light, dark = re.findall(r":root\{([^}]*)\}", STYLE)
    assert "@media screen and (prefers-color-scheme:dark){:root{" + dark in STYLE
    colours = lambda block: set(re.findall(r"--([a-z0-9-]+):#", block))  # noqa: E731
    assert colours(light) == colours(dark) and len(colours(dark)) >= 10
    # Every colour on screen is one of the palette's: a literal would stay light on a dark page.
    rules = re.sub(r":root\{[^}]*\}", "", STYLE.split("@media print")[0])
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", rules)


def test_the_fixed_labels_follow_the_reports_language():
    page = render_page(_ZH_REPORT + "\n[x](https://a.example)\n", language="zh")
    assert f'<p class="kicker">{t_in("zh", "Research report")}</p>' in page
    assert f"<h2>{t_in('zh', 'Sources')}</h2>" in page
    assert t_in("zh", "Sources") != "Sources" and '<html lang="zh-CN">' in page


def test_highlights_are_shown_above_the_body_and_escaped():
    highlights = Highlights(
        figures=(Figure("1.2 s", "Vite <b>cold</b> start"), Figure("8.4 s", "webpack")),
        findings=("Vite starts in 1.2 s", "webpack in 8.4 s"),
        timeline=(Moment("March 2024", "Vite 5.2"), Moment("November 2024", "Vite 6"), Moment("June 2025", "Vite 7")),
    )
    page = render_page(_REPORT, highlights=highlights)
    assert '<p class="figure-value">1.2 s</p><p class="figure-label">Vite &lt;b&gt;cold&lt;/b&gt; start</p>' in page
    assert '<ol class="findings"><li>Vite starts in 1.2 s</li>' in page
    assert '<li><span class="when">March 2024</span>Vite 5.2</li>' in page
    assert page.index('class="figures"') < page.index('id="section-1"')


# --------------------------------------------------------------------------- #
# Highlights: a model selects, the report decides                              #
# --------------------------------------------------------------------------- #


def _offer(**kinds) -> dict:
    return {"figures": [], "findings": [], "timeline": [], **kinds}


def test_only_text_the_report_holds_is_kept():
    offered = _offer(
        figures=[
            {"value": "1.2 s", "label": "Vite starts in"},
            {"value": "8.4 s", "label": "webpack"},
            {"value": "9.9 s", "label": "webpack"},
            {"value": "1.2 s", "label": "Vite is far quicker"},
        ],
        findings=[
            "No shared benchmark on one machine was found.",
            "Vite starts in 1.2 s",
            "Vite is always the better choice for every team",
        ],
    )
    report = "> Vite starts in 1.2 s; webpack takes 8.4 s.\n\n" + _REPORT.split("\n\n", 1)[1]
    highlights, counters = check_highlights(offered, report)
    assert highlights.figures == (Figure("1.2 s", "Vite starts in"), Figure("8.4 s", "webpack"))
    assert highlights.findings == ("No shared benchmark on one machine was found.", "Vite starts in 1.2 s")
    assert (counters["figures_offered"], counters["figures_kept"]) == (4, 2)
    assert (counters["findings_offered"], counters["findings_kept"]) == (3, 2)


def test_a_long_key_figure_is_set_smaller_so_a_tile_holds_it_on_one_line():
    """A real page cut ``190,042,046`` across two lines of its tile."""
    figures = tuple(
        Figure(value, "label")
        for value in ("1.2 s", "190,042,046", "1,234,567 requests", "\u4e00\u4e8c\u4e09\u56db\u4e94")
    )
    page = render_page(_REPORT, highlights=Highlights(figures=figures))
    assert '<p class="figure-value">1.2 s</p>' in page
    assert '<p class="figure-value is-wide">190,042,046</p>' in page
    assert '<p class="figure-value is-wider">1,234,567 requests</p>' in page
    # A CJK character is two columns wide.
    assert '<p class="figure-value is-wide">\u4e00\u4e8c\u4e09\u56db\u4e94</p>' in page
    sizes = dict(re.findall(r"\.figure-value(\.is-\w+)?\{[^}]*?font-size:(\d+)px", STYLE))
    assert int(sizes[""]) > int(sizes[".is-wide"]) > int(sizes[".is-wider"])


def test_a_figure_and_its_label_must_share_a_sentence_or_a_row():
    """A value under another row's label is a claim the report never made."""
    crossed = _offer(figures=[{"value": "1.2 s", "label": "webpack"}, {"value": "8.4 s", "label": "Vite"}])
    assert (
        check_highlights(crossed, "> a\n\n| Tool | Start |\n|--|--|\n| Vite | 1.2 s |\n| webpack | 8.4 s |\n")[
            0
        ].figures
        == ()
    )


def test_a_fragment_or_a_repeated_caption_is_not_a_figure():
    """What a real model returned: one sentence cut into two captions, and one caption under two numbers."""
    report = "> Linear went from 46s to 6s, a drop of 87%.\n\n## One\nb\n\n## Limitations\nc\n"
    offered = _offer(
        figures=[
            {"value": "46s", "label": "Linear went from 46s to 6s"},
            {"value": "6s", "label": "Linear went from 46s to 6s"},
            {"value": "87%", "label": "of"},
            {"value": "87%", "label": "a drop of 87%"},
            {"value": "6s", "label": "6s"},
        ]
    )
    assert check_highlights(offered, report)[0].figures == (
        Figure("46s", "Linear went from 46s to 6s"),
        Figure("87%", "a drop of 87%"),
    )


def test_a_two_character_chinese_label_names_a_figure():
    """A two-character label is a name, not a fragment; the CJK threshold counts it as one."""
    price, last = "\u4ef7\u683c", "\u4e0a\u4e00\u4ee3\u4ef7\u683c"
    report = f"> {price} 299 {last} 399\n\n## {price}\nb\n\n## \u672a\u6838\u9a8c\u9879\n\u65e0\n"
    offered = _offer(figures=[{"value": "299", "label": price}, {"value": "399", "label": last}])
    assert [figure.label for figure in check_highlights(offered, report)[0].figures] == [price, last]


def test_a_short_label_opening_a_body_paragraph_is_set_in_bold():
    """``Scope: ...`` and a Chinese twin read as labels; a sentence with a colon in it does not."""
    risk = "\u95ee\u9898"
    page = render_page(
        f"> Use Vite: it starts fast.\n\n## One\nScope: the numbers are old.\n\n{risk}\uff1aold numbers.\n\n"
        "Use Vite: it starts fast.\n\n## Limitations\nc\n"
    )
    assert "<p><strong>Scope</strong>: the numbers are old.</p>" in page
    assert f"<p><strong>{risk}</strong>\uff1aold numbers.</p>" in page
    assert "<p>Use Vite: it starts fast.</p>" in page
    assert '<div class="answer"><p>Use Vite: it starts fast.</p></div>' in page


def test_a_table_keyed_by_short_names_sets_its_first_column_in_bold():
    keyed = render_page("> a\n\n## One\n| Bank | Target |\n|--|--|\n| UBS | $75 |\n| HSBC | $55 |\n")
    assert '<table class="keyed">' in keyed and ".keyed td:first-child{" in STYLE
    long = "a first cell far too long to be the name of the row it opens"
    loose = render_page(f"> a\n\n## One\n| What | Target |\n|--|--|\n| {long} | $75 |\n| UBS | $55 |\n")
    assert '<table class="keyed">' not in loose


def test_paragraphs_opening_with_bold_sentences_stay_paragraphs():
    """A real outline report opened 23 paragraphs with a bold sentence; set as numbered points, it lost its prose."""
    body = "\n\n".join(f"**Rates rose in month {n}.** The data show it." for n in range(3))
    page = render_page(f"> a\n\n## One\n{body}\n\n## Limitations\nc\n")
    assert '<p class="step-name">' not in page and "<dt>" not in page
    assert "<p><strong>Rates rose in month 0.</strong> The data show it.</p>" in page


def test_a_conclusion_label_neither_joins_nor_vetoes_the_named_points():
    """The outline clause opens each section with ``**Conclusion**:``; it is not the first of the points."""
    points = "**Safety**: audit logs.\n\n**Isolation**: per tenant.\n\n**Approval**: two people."
    for between in ("", "Evidence for it.\n\n"):
        page = render_page(
            f"> a\n\n## One\n**Conclusion**: rates **rise**.\n\n{between}{points}\n\n## Limitations\nc\n"
        )
        assert re.findall(r'<p class="step-name">([^<]*)</p>', page) == ["Safety", "Isolation", "Approval"]
        assert "<p><strong>Conclusion</strong>: rates <strong>rise</strong>.</p>" in page


def test_a_link_check_section_is_outside_the_length_and_a_body_section_is_not():
    from research_flow.support.report_file import body_length

    counts = {"sections": [("Findings", 0, 10), ("Link check", 0, 5), ("LinkedIn verification", 0, 7)]}
    assert body_length(counts) == 17
    # Read whole, and in either word order.
    from research_flow.support.report_file import measure_report

    report = "## Findings\none two three\n\n## Section 4: External Link Check\na b c d e\n\n## \u6838\u9a8c\u94fe\u63a5\nf g\n"
    assert body_length(measure_report(report)) == 4  # the heading word and three


def test_a_sources_list_that_mixes_addresses_and_local_files_is_the_one_list():
    report = (
        "> See [a](https://a.com/x).\n\n## One\nAs [b](https://b.com/y) and /data/notes.md say.\n\n"
        "## Limitations\nnone\n\n## Sources\n\n- A https://a.com/x\n- B https://b.com/y\n"
        "- /data/notes.md\n- /data/more.csv\n"
    )
    page = render_page(report)
    assert page.count("<h2>Sources</h2>") == 1 and '<li id="source-4">' in page
    assert close_with_sources(report) == (report, {})
    # Under a heading that names no sources, a list of paths stays a list of files.
    files = "> a\n\n## One\nb\n\n## Files\n\n- /data/notes.md\n- /data/more.csv\n"
    assert 'class="sources"' not in render_page(files)


def test_a_key_figure_comes_from_the_answer_alone():
    """The numbers the answer rests on are the tiles; one the body gives alone is not."""
    report = "> Vite starts in 1.2 s; webpack takes 8.4 s.\n\n## One\nRspack starts in 0.4 s and Parcel in 3.1 s.\n"
    offered = _offer(
        figures=[
            {"value": "0.4 s", "label": "Rspack starts in"},
            {"value": "3.1 s", "label": "Parcel"},
            {"value": "1.2 s", "label": "Vite starts in"},
            {"value": "8.4 s", "label": "webpack"},
        ]
    )
    assert [figure.value for figure in check_highlights(offered, report)[0].figures] == ["1.2 s", "8.4 s"]


def test_a_date_stands_once_in_the_timeline():
    report = "> a\n\n## One\n- 2020: webpack 5 released\n- 2024: Vite 6 released\n- 2026: Vite 8 released\n\n## Limitations\nc\n"
    offered = _offer(
        timeline=[
            {"when": "2020", "what": "webpack 5 released"},
            {"when": "2020", "what": "webpack 5"},
            {"when": "2024", "what": "Vite 6 released"},
            {"when": "2026", "what": "8"},
            {"when": "2026", "what": "Vite 8 released"},
        ]
    )
    assert [m.what for m in check_highlights(offered, report)[0].timeline] == [
        "webpack 5 released",
        "Vite 6 released",
        "Vite 8 released",
    ]


def test_a_kind_with_too_few_entries_is_dropped_whole_and_a_long_one_is_cut():
    one = _offer(figures=[{"value": "1.2 s", "label": "Vite"}], timeline=[{"when": "March 2024", "what": "Vite 5.2"}])
    highlights, _ = check_highlights(one, _REPORT)
    assert not highlights
    many = _offer(findings=[f"finding number {n} holds" for n in range(9)])
    report = "> a\n\n## One\n" + " ".join(f"finding number {n} holds." for n in range(9)) + "\n\n## Limitations\nc\n"
    assert len(check_highlights(many, report)[0].findings) == 5


def test_copied_text_matches_whatever_spacing_the_model_gave_it():
    # Chinese for "hot update takes 50 milliseconds"; the model drops and adds spaces between scripts.
    second = "\u70ed\u66f4\u65b0\u8017\u65f6 50 \u6beb\u79d2"
    report = _ZH_REPORT.replace(f"> {_ZH_SENTENCE}\n", f"> {_ZH_SENTENCE}\n> {second}\n", 1)
    offered = _offer(
        figures=[
            {"value": "1.2\u79d2", "label": "\u51b7\u542f\u52a8 \u8017\u65f6"},
            {"value": "50\u6beb\u79d2", "label": "\u70ed\u66f4\u65b0"},
            {"value": "1.2 \u79d2", "label": "\u51b7\u542f\u52a8"},
        ]
    )
    figures = check_highlights(offered, report)[0].figures
    # Both are in the report whatever their spacing; the third is the first number again.
    assert [f.value for f in figures] == ["1.2\u79d2", "50\u6beb\u79d2"]


def test_an_answer_that_is_not_the_asked_shape_gives_nothing():
    for data in ({}, {"figures": "1.2 s"}, {"figures": [1, None, {"value": 3}], "findings": [{"a": 1}, 7]}):
        highlights, _ = check_highlights(data, _REPORT)
        assert not highlights


class _Provider:
    def __init__(self, reply=None, *, finish="stop", error=None, delay=0.0):
        self.reply, self.finish, self.error, self.delay = reply, finish, error, delay
        self.calls: list[dict] = []

    async def chat_with_retry(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(content=self.reply, finish_reason=self.finish)


_GOOD = json.dumps(
    {
        "figures": [{"value": "1.2 s", "label": "Vite"}, {"value": "8.4 s", "label": "webpack"}],
        "findings": [],
        "timeline": [
            {"when": "March 2024", "what": "Vite 5.2 shipped"},
            {"when": "November 2024", "what": "Vite 6 shipped"},
            {"when": "June 2025", "what": "Vite 7 shipped"},
        ],
    }
)


@pytest.mark.asyncio
async def test_the_selection_is_one_call_over_the_whole_report():
    provider = _Provider("<think>reading</think>```json\n" + _GOOD + "\n```")
    highlights, record = await select_highlights(provider, _REPORT, model="judge", reasoning_effort=None)
    assert len(highlights.figures) == 2 and len(highlights.timeline) == 3
    assert record == {
        "selected": True,
        "figures_offered": 2,
        "figures_kept": 2,
        "findings_offered": 0,
        "findings_kept": 0,
        "timeline_offered": 3,
        "timeline_kept": 3,
    }
    (call,) = provider.calls
    assert call["model"] == "judge" and call["messages"][1] == {"role": "user", "content": _REPORT}
    assert "reasoning_effort" not in call


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "reason"),
    [
        (_Provider(error=RuntimeError("upstream")), "call_failed:RuntimeError"),
        (_Provider(_GOOD, delay=0.2), "timeout"),
        (_Provider("", finish="length"), "no_answer:length"),
        (_Provider("I could not find any."), "no_json"),
    ],
    ids=["call_failed", "timeout", "cut_off", "prose"],
)
async def test_a_selection_that_fails_gives_no_highlights_and_says_why(provider, reason):
    highlights, record = await select_highlights(provider, _REPORT, timeout_seconds=0.05)
    assert not highlights
    assert record == {"selected": False, "reason": reason}


# --------------------------------------------------------------------------- #
# The file, and the turn that writes it                                        #
# --------------------------------------------------------------------------- #


def _cfg(**page) -> FlowConfig:
    return FlowConfig(
        enabled=True,
        final_shape={"report_depth": True, "report_layout": "reader"},
        report_page={"enabled": True, **page},
    )


def _frame(tmp_path, cfg: FlowConfig | None = None, provider=None) -> TurnFrame:
    cfg = cfg or _cfg()
    return TurnFrame(cfg, SessionStore(tmp_path / "state"), None, page=ReportPage(cfg.report_page, provider))


async def _send(
    frame: TurnFrame, reply: str, messages: list[dict] | None = None, facts: dict | None = None
) -> tuple[str, dict]:
    facts = {} if facts is None else facts
    await frame.after_iteration(AgentHookContext(session_key="cli:t", messages=messages or [], metadata=facts))
    decision = await frame.after_send(AgentHookContext(session_key="cli:t", outbound_content=reply, metadata=facts))
    return decision.modified_content or reply, facts


def _chain(cfg: FlowConfig, provider, tmp_path) -> list:
    return build_chain(
        cfg,
        provider,
        max_iterations=20,
        context_window_tokens=0,
        tools=ToolHandles(),
        store=SessionStore(tmp_path / "state"),
    )


@pytest.fixture
def _no_trail(monkeypatch):
    monkeypatch.setattr(flow_module, "ledger_path", lambda: "ledger")
    monkeypatch.setattr(flow_module, "build_appendix", lambda *a: ("", {"emitted": False}))


@pytest.mark.asyncio
async def test_a_report_gets_its_page_in_the_working_directory_and_the_reply_names_it(tmp_path, monkeypatch):
    monkeypatch.setattr(flow_module, "ledger_path", lambda: "ledger")
    monkeypatch.setattr(flow_module, "build_appendix", lambda *a: (_WARNED_TRAIL, {"emitted": True}))
    with workdir.bind(tmp_path):
        sent, facts = await _send(_frame(tmp_path), _REPORT)

    (page,) = tmp_path.glob("*.html")
    assert re.fullmatch(r"research-report-[0-9a-f]{8}\.html", page.name)
    html = page.read_text(encoding="utf-8")
    assert GENERATOR in html and "<h2>What the sources show</h2>" in html
    # How the turn worked is for whoever ran it. Neither the trail nor a warning of it is on the page.
    assert "Research trail" not in html and "never opened" not in html and "listed.example" not in html
    # The reply is the report it was, then the trail, then the one line that names the page.
    assert "never opened: https://listed.example/b" in sent
    assert sent == _REPORT.rstrip() + "\n" + _WARNED_TRAIL.rstrip() + "\n\n" + page_line(str(page))
    record = facts["observers"]["report_page"]
    assert record["written"] is True and record["path"] == str(page) and record["bytes"] == page.stat().st_size


@pytest.mark.asyncio
async def test_the_same_report_writes_the_same_file(tmp_path, _no_trail):
    with workdir.bind(tmp_path):
        first, _ = await _send(_frame(tmp_path), _REPORT)
        second, _ = await _send(_frame(tmp_path), _REPORT)
    assert first == second and len(list(tmp_path.glob("*.html"))) == 1


@pytest.mark.asyncio
async def test_the_page_stands_beside_the_report_file_and_follows_it(tmp_path, _no_trail):
    report = tmp_path / "vite.md"
    draft = "# Vite or webpack\n\n> draft\n\n## What the sources show\nx\n\n## Timeline\ny\n\n## Limitations\nz\n"
    report.write_text(draft, encoding="utf-8")
    reply = _REPORT.replace("8.4 s.\n\n## What", f"8.4 s.\n> Saved to {report}.\n\n## What", 1)
    wrote = {"role": "tool", "name": "write_file", "content": f"Successfully wrote 9 bytes to {report}"}
    with workdir.bind(tmp_path / "elsewhere"):
        sent, facts = await _send(_frame(tmp_path), reply, [wrote])

    page = tmp_path / "vite.html"
    assert facts["observers"]["report_file"]["synced"] is True
    assert sent.endswith(page_line(str(page)))
    html = page.read_text(encoding="utf-8")
    # The file's title, which the reply leaves out; not the line of the reply that names the file.
    assert "<h1>Vite or webpack</h1>" in html and "Saved to" not in html
    assert not (tmp_path / "elsewhere").exists()
    # Removing the file is a setting of its own, and it is off here.
    assert report.exists() and "removed" not in facts["observers"]["report_file"]


_DRAFT = "# Vite or webpack\n\n> draft\n\n## What the sources show\nx\n\n## Timeline\ny\n\n## Limitations\nz\n"


def _saved(report, facts: str = "") -> str:
    return _REPORT.replace("8.4 s.\n\n## What", f"8.4 s.\n> Saved to {report}{facts}.\n\n## What", 1)


@pytest.mark.asyncio
async def test_the_report_file_is_removed_once_the_page_holds_it(tmp_path, _no_trail):
    report, page = tmp_path / "vite.md", tmp_path / "vite.html"
    report.write_text(_DRAFT, encoding="utf-8")
    reply = _saved(report).rstrip()
    with workdir.bind(tmp_path / "elsewhere"):
        sent, facts = await _send(_frame(tmp_path, _cfg(remove_markdown=True)), reply, [_wrote(report)])

    assert not report.exists() and facts["observers"]["report_file"]["removed"] is True
    # The page holds the file's text, its title included.
    assert "<h1>Vite or webpack</h1>" in page.read_text(encoding="utf-8")
    # The reply still names the file where the model named it, so its last line says the file is gone.
    last = f"Report page (HTML file for the reader): `{page}`. It replaces `{report}`, which is removed."
    assert sent.endswith(last) and last == page_line(str(page), str(report))
    # Nothing above that line changed: a reply that went out as deltas gets the line as one more.
    assert _appended_by_hook(reply, sent) == "\n\n---\n\n" + last
    # A later turn is not told that the session still holds the file.
    assert SessionStore(tmp_path / "state").load("cli:t").report_files == {}


@pytest.mark.asyncio
async def test_a_kept_report_file_is_removed_too(tmp_path, _no_trail):
    report = tmp_path / "vite.md"
    full = f"# Vite or webpack\n\n> Use Vite.\n\n## What the sources show\n{'Only the file says this. ' * 60}\n"
    report.write_text(full + "\n## Timeline\n- a\n\n## Limitations\nz\n", encoding="utf-8")
    with workdir.bind(tmp_path / "elsewhere"):
        _, facts = await _send(_frame(tmp_path, _cfg(remove_markdown=True)), _REPORT, [_wrote(report)])

    record = facts["observers"]["report_file"]
    assert record["kept"] is True and record["removed"] is True and not report.exists()
    assert "Only the file says this." in (tmp_path / "vite.html").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_a_file_the_page_was_not_written_from_stays(tmp_path, _no_trail):
    notes = tmp_path / "notes.md"
    notes.write_text("# Notes\n\n## Queries\na\n\n## Dead ends\nb\n", encoding="utf-8")
    with workdir.bind(tmp_path):
        _, facts = await _send(_frame(tmp_path, _cfg(remove_markdown=True)), _REPORT, [_wrote(notes)])

    assert facts["observers"]["report_file"] == {"files_written": 1, "synced": False, "reason": "no_report_draft"}
    assert facts["observers"]["report_page"]["written"] is True and notes.exists()


@pytest.mark.asyncio
async def test_a_report_file_that_cannot_be_removed_stays_named(tmp_path, _no_trail, monkeypatch):
    def _fails(path: str) -> bool:
        return False

    monkeypatch.setattr(flow_module, "remove_file", _fails)
    report = tmp_path / "vite.md"
    report.write_text(_DRAFT, encoding="utf-8")
    with workdir.bind(tmp_path / "elsewhere"):
        sent, facts = await _send(_frame(tmp_path, _cfg(remove_markdown=True)), _saved(report), [_wrote(report)])

    assert report.exists() and facts["observers"]["report_file"]["removed"] is False
    # The file is still there, so the line does not say that the page replaces it.
    assert f"> Saved to {report}." in sent and sent.endswith(page_line(str(tmp_path / "vite.html")))
    assert str(report) in SessionStore(tmp_path / "state").load("cli:t").report_files


def test_removal_reports_whether_the_file_is_gone(tmp_path):
    from research_flow.support.report_file import remove_file

    report = tmp_path / "report.md"
    report.write_text("x", encoding="utf-8")
    assert remove_file(str(report)) is True and not report.exists()
    assert remove_file(str(report)) is True
    assert remove_file(str(tmp_path)) is False and tmp_path.exists()


_STEPS = "Take it in order. Step 1: measure. Step 2: pilot ten files. Step 3: go on."
_STEPPED = _REPORT.replace("## Timeline\n", "## Path\n" + _STEPS + "\n\n## Timeline\n", 1)
_STEP_ITEMS = "<div><dt>Step 1</dt><dd>measure.</dd></div><div><dt>Step 2</dt><dd>pilot ten files.</dd></div>"


@pytest.mark.asyncio
async def test_steps_run_together_are_a_list_in_the_file_and_on_the_page(tmp_path, _no_trail):
    report = tmp_path / "vite.md"
    report.write_text("> draft\n\n## What the sources show\nx\n\n## Path\np\n\n## Limitations\nz\n", encoding="utf-8")
    with workdir.bind(tmp_path):
        sent, facts = await _send(_frame(tmp_path), _STEPPED, [_wrote(report)])

    assert facts["observers"]["report_file"]["step_lists"] == 1 and sent.startswith(_STEPPED.rstrip())
    held = report.read_text(encoding="utf-8")
    assert "## Path\nTake it in order.\n\n- **Step 1**: measure.\n- **Step 2**: pilot ten files.\n- **Step 3**" in held
    html = (tmp_path / "vite.html").read_text(encoding="utf-8")
    assert '<p>Take it in order.</p><dl class="terms">' + _STEP_ITEMS in html


@pytest.mark.asyncio
async def test_a_page_with_no_report_file_lists_the_steps_too(tmp_path, _no_trail):
    with workdir.bind(tmp_path):
        _, facts = await _send(_frame(tmp_path), _STEPPED)
    (page,) = tmp_path.glob("*.html")
    assert '<p>Take it in order.</p><dl class="terms">' + _STEP_ITEMS in page.read_text(encoding="utf-8")
    assert facts["observers"]["report_page"]["step_lists"] == 1 and "follows" not in facts["observers"]["report_page"]


@pytest.mark.asyncio
async def test_a_report_file_longer_than_the_reply_is_what_the_page_shows(tmp_path, _no_trail):
    """A real turn wrote the full report to its file and replied with a shorter telling: the page showed the reply."""
    report = tmp_path / "vite.md"
    full = (
        f"# Vite or webpack\n\n> Use Vite.\n> Report file: {report}\n\n## What the sources show\n"
        + "Only the file says this at length. " * 40
        + "\n\n## Path\n"
        + _STEPS
        + "\n\n## Timeline\n- a\n\n## Limitations\nz\n"
    )
    report.write_text(full, encoding="utf-8")
    with workdir.bind(tmp_path / "elsewhere"):
        sent, facts = await _send(_frame(tmp_path), _REPORT, [_wrote(report)])

    record = facts["observers"]["report_file"]
    assert record["synced"] is False and record["kept"] is True and record["step_lists"] == 1
    page = tmp_path / "vite.html"
    assert sent.endswith(page_line(str(page))) and facts["observers"]["report_page"]["follows"] == "file"
    html, held = page.read_text(encoding="utf-8"), report.read_text(encoding="utf-8")
    # The page is the file: its title, its text and its steps, and neither names the file.
    assert "<h1>Vite or webpack</h1>" in html and "Only the file says this at length." in html
    assert '<dl class="terms">' + _STEP_ITEMS in html and "- **Step 1**: measure." in held
    assert "Report file" not in html and "Report file" not in held and "Only the file says this" in held
    assert "1.2 s where webpack takes 8.4 s" not in html and not (tmp_path / "elsewhere").exists()


@pytest.mark.asyncio
async def test_the_share_check_reads_the_reply_before_the_source_list_is_added(tmp_path, _no_trail):
    """The list the flow adds is the page's; it must not make a summary look as long as the file."""
    urls = [f"https://source{i}.example/page" for i in range(1, 11)]
    links = " ".join(f"[s{i}]({url})" for i, url in enumerate(urls, 1))
    reply = (
        "> Use Vite.\n\n## What the sources show\n" + ("Vite starts faster. " * 18) + links + "\n\n## Limitations\nz\n"
    )
    report = tmp_path / "vite.md"
    full = "> draft\n\n## What the sources show\n" + ("Only the file says this. " * 40) + "\n\n## Limitations\nz\n"
    report.write_text(full, encoding="utf-8")
    held, pre = len(full.strip()), len(reply.strip())
    closed = len(page_module.tidy(reply, None)[0])
    assert pre < held * 0.8 <= closed, "the test must sit on the boundary it holds"
    with workdir.bind(tmp_path):
        _, facts = await _send(_frame(tmp_path), reply, [_wrote(report)])

    record = facts["observers"]["report_file"]
    assert record["synced"] is False and record["kept"] is True
    assert "Only the file says this." in report.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_the_page_shows_a_kept_report_file_whatever_its_shape(tmp_path, _no_trail):
    """A file with no answer blockquote was not followed: the page showed the reply, the file something else."""
    report = tmp_path / "vite.md"
    full = "# Vite or webpack\n\n## What the sources show\n" + "Only the file says this at length. " * 40
    report.write_text(full + "\n\n## Timeline\n- a\n\n## Limitations\nz\n", encoding="utf-8")
    with workdir.bind(tmp_path):
        _, facts = await _send(_frame(tmp_path), _REPORT, [_wrote(report)])

    assert facts["observers"]["report_file"]["kept"] is True and facts["observers"]["report_page"]["follows"] == "file"
    html = (tmp_path / "vite.html").read_text(encoding="utf-8")
    assert "<h1>Vite or webpack</h1>" in html and "Only the file says this at length." in html
    assert "1.2 s where webpack takes 8.4 s" not in html


@pytest.mark.asyncio
async def test_a_line_of_the_answer_that_names_only_the_page_stands_in_both(tmp_path, _no_trail):
    """The page left it out and the file kept it: the two read differently."""
    report = tmp_path / "vite.md"
    report.write_text(
        "> draft\n\n## What the sources show\nx\n\n## Timeline\ny\n\n## Limitations\nz\n", encoding="utf-8"
    )
    reply = _REPORT.replace("8.4 s.\n\n## What", "8.4 s.\n> The page is vite.html.\n\n## What", 1)
    with workdir.bind(tmp_path):
        await _send(_frame(tmp_path), reply, [_wrote(report)])
    assert "The page is vite.html." in report.read_text(encoding="utf-8")
    assert "<p>The page is vite.html.</p>" in (tmp_path / "vite.html").read_text(encoding="utf-8")


_TALK = "Both files are saved and checked. Here is the report."
# The shape gate takes a reply that opens with a line of its own for a report only when a
# heading reads as the answer; any other such reply is sent back to be rewritten.
_SPOKEN_REPORT = _REPORT.replace("## What the sources show", "## The answer in detail", 1)
_SPOKEN_DRAFT = "{}> draft\n\n## The answer in detail\nx\n\n## Timeline\ny\n\n## Limitations\nz\n"


def _wrote(path) -> dict:
    return {"role": "tool", "name": "write_file", "content": f"Successfully wrote 9 bytes to {path}"}


@pytest.mark.asyncio
async def test_talk_before_the_answer_that_the_draft_does_not_hold_stays_off_the_page(tmp_path, _no_trail):
    """The model wrote the draft as the report: a line it holds nowhere is said to whoever receives the reply."""
    report = tmp_path / "vite.md"
    report.write_text(_SPOKEN_DRAFT.format(""), encoding="utf-8")
    named = _SPOKEN_REPORT.replace("8.4 s.\n\n## The", "8.4 s.\n> Files: vite.md and vite.html\n\n## The", 1)
    reply = f"{_TALK}\n\n{named}"
    with workdir.bind(tmp_path):
        sent, facts = await _send(_frame(tmp_path), reply, [_wrote(report)])

    html = (tmp_path / "vite.html").read_text(encoding="utf-8")
    cover = html.split("</header>")[0]
    assert '<div class="answer"><p>Use Vite: it starts in 1.2 s where webpack takes 8.4 s.</p></div>' in cover
    assert "Both files are saved" not in html and "Files: vite.md" not in html and 'class="preface"' not in html
    assert facts["observers"]["report_file"]["talk_lines"] == 1
    assert facts["observers"]["report_page"]["follows"] == "file"
    # The reply is as the model wrote it. The file holds what the page shows: neither the talk nor its own name.
    assert sent.startswith(reply.rstrip())
    held = report.read_text(encoding="utf-8")
    assert held.startswith("> Use Vite") and "Both files are saved" not in held and "Files: vite.md" not in held


@pytest.mark.asyncio
async def test_a_code_block_before_the_answer_is_kept_or_left_out_whole(tmp_path, _no_trail):
    """Cut at its own blank line, a block the draft held half of left a fence open over the answer."""
    report = tmp_path / "vite.md"
    report.write_text(_SPOKEN_DRAFT.format("```\nnpm create vite\n```\n\n"), encoding="utf-8")
    reply = "```\nnpm create vite\n\nnpm run dev\n```\n\n" + _SPOKEN_REPORT
    with workdir.bind(tmp_path):
        _, facts = await _send(_frame(tmp_path), reply, [_wrote(report)])

    html = (tmp_path / "vite.html").read_text(encoding="utf-8")
    assert '<div class="answer"><p>Use Vite: it starts in 1.2 s where webpack takes 8.4 s.</p></div>' in html
    # The report's three sections and the list of sources the flow closed it with.
    assert len(re.findall(r'id="section-\d+"', html)) == 4 and "npm run dev" not in html
    assert facts["observers"]["report_file"]["talk_lines"] == 5
    assert "npm run dev" not in report.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_a_line_before_the_answer_that_the_draft_holds_is_kept(tmp_path, _no_trail):
    report = tmp_path / "vite.md"
    report.write_text(_SPOKEN_DRAFT.format("Written for the platform team.\n\n"), encoding="utf-8")
    reply = "Written for the **platform team**.\n\n" + _SPOKEN_REPORT
    with workdir.bind(tmp_path):
        _, facts = await _send(_frame(tmp_path), reply, [_wrote(report)])

    html = (tmp_path / "vite.html").read_text(encoding="utf-8")
    assert '<div class="preface"><p>Written for the <strong>platform team</strong>.</p></div>' in html
    assert "talk_lines" not in facts["observers"]["report_file"]
    assert report.read_text(encoding="utf-8").startswith("Written for the **platform team**.")


@pytest.mark.asyncio
async def test_with_no_draft_to_compare_a_line_before_the_answer_is_kept(tmp_path, _no_trail):
    with workdir.bind(tmp_path):
        await _send(_frame(tmp_path), f"{_TALK}\n\n{_SPOKEN_REPORT}")
    (page,) = tmp_path.glob("*.html")
    assert f'<div class="preface"><p>{_TALK}</p></div>' in page.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_a_file_someone_else_wrote_under_the_pages_name_is_left_alone(tmp_path, _no_trail):
    report, theirs = tmp_path / "vite.md", tmp_path / "vite.html"
    report.write_text(
        "> draft\n\n## What the sources show\nx\n\n## Timeline\ny\n\n## Limitations\nz\n", encoding="utf-8"
    )
    theirs.write_text("<html>the user's own page</html>", encoding="utf-8")
    wrote = {"role": "tool", "name": "write_file", "content": f"Successfully wrote 9 bytes to {report}"}
    _, facts = await _send(_frame(tmp_path), _REPORT, [wrote])

    assert theirs.read_text(encoding="utf-8") == "<html>the user's own page</html>"
    written = facts["observers"]["report_page"]["path"]
    assert written != str(theirs) and written.startswith(str(tmp_path / "research-report-"))
    # A page this flow wrote earlier is its own to write over.
    theirs.write_text(f"<html>{GENERATOR}</html>", encoding="utf-8")
    _, facts = await _send(_frame(tmp_path), _REPORT, [wrote])
    assert facts["observers"]["report_page"]["path"] == str(theirs)


@pytest.mark.asyncio
async def test_a_file_under_the_pages_name_that_cannot_be_read_is_not_written_over(tmp_path, _no_trail):
    """An undecodable file is not an absent one: it is someone else's, and it stays."""
    report, theirs = tmp_path / "vite.md", tmp_path / "vite.html"
    report.write_text("> draft\n\n## What the sources show\nx\n\n## Limitations\nz\n", encoding="utf-8")
    theirs.write_bytes(b"\xff\xfe\x00binary")
    wrote = {"role": "tool", "name": "write_file", "content": f"Successfully wrote 9 bytes to {report}"}
    _, facts = await _send(_frame(tmp_path), _REPORT, [wrote])

    assert theirs.read_bytes() == b"\xff\xfe\x00binary"
    written = facts["observers"]["report_page"]["path"]
    assert written != str(theirs) and written.startswith(str(tmp_path / "research-report-"))
    assert GENERATOR in pathlib.Path(written).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_an_html_twin_the_turn_wrote_by_hand_gives_way_to_the_page(tmp_path, _no_trail):
    """Asked for "the report as HTML too", a turn writes one itself: unreviewed, and in a look of its own.

    The dispatcher then delivers the file under the report's name, so the page has to be that file.
    """
    report, twin = tmp_path / "vite.md", tmp_path / "vite.html"
    report.write_text(
        "> draft\n\n## What the sources show\nx\n\n## Timeline\ny\n\n## Limitations\nz\n", encoding="utf-8"
    )
    twin.write_text("<html><body>a page the model wrote by hand</body></html>", encoding="utf-8")
    wrote = [
        {"role": "tool", "name": "write_file", "content": f"Successfully wrote 9 bytes to {report}"},
        {"role": "tool", "name": "write_file", "content": f"Successfully wrote 55 bytes to {twin}"},
    ]
    sent, facts = await _send(_frame(tmp_path), _REPORT, wrote)

    record = facts["observers"]["report_page"]
    assert record["path"] == str(twin) and record["replaced_own_draft"] is True
    assert GENERATOR in twin.read_text(encoding="utf-8") and sent.endswith(page_line(str(twin)))
    assert [p.name for p in tmp_path.glob("*.html")] == ["vite.html"]
    assert "dr_report_pages" not in facts


def test_only_a_file_the_turn_wrote_whole_counts_as_its_own_page():
    def tool(name, text):
        return {"role": "tool", "name": name, "content": text}

    messages = [
        tool("write_file", "Successfully wrote 9 bytes to /w/before.html"),
        tool("write_file", "Successfully wrote 9 bytes to /w/a.html"),
        tool("write_file", "Successfully appended 9 bytes to /w/b.html"),
        tool("edit_file", "Successfully edited /w/c.html"),
        tool("write_file", "Successfully wrote 9 bytes to /w/notes.md"),
        tool("write_file", "Successfully wrote 9 bytes to /w/a.html\nSuccessfully wrote 1 bytes to /w/forged.html"),
        tool("exec", "Successfully wrote 9 bytes to /w/d.html"),
    ]
    assert written_pages(messages, since=1) == ["/w/a.html"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    ["Before I start: which region do you mean?", "> yes\n\n## Part\nbody with no limits section\n"],
    ids=["clarify", "not_all_three_parts"],
)
async def test_a_reply_that_is_not_the_report_gets_no_page(tmp_path, _no_trail, reply):
    with workdir.bind(tmp_path):
        sent, facts = await _send(_frame(tmp_path), reply)
    assert sent == reply and not list(tmp_path.glob("*.html"))
    # The contract promises the page for a reply that is the report: the record says why there is none.
    assert facts["observers"]["report_page"] == {"written": False, "reason": "reply_not_a_report"}


@pytest.mark.asyncio
async def test_a_turn_that_is_not_research_records_no_page(tmp_path, _no_trail):
    token = _RESEARCH_TURN.set(False)
    try:
        with workdir.bind(tmp_path):
            _, facts = await _send(_frame(tmp_path), "Hello.")
    finally:
        _RESEARCH_TURN.reset(token)
    assert "report_page" not in facts.get("observers", {})


@pytest.mark.asyncio
async def test_a_turn_with_nowhere_to_write_ends_as_it_would_have(tmp_path, _no_trail, monkeypatch):
    sent, facts = await _send(_frame(tmp_path), _REPORT)
    assert sent == _REPORT
    assert facts["observers"]["report_page"] == {"written": False, "reason": "no_working_directory"}

    def refuse(path, text):
        raise PermissionError("read-only")

    monkeypatch.setattr(page_module, "write_text", refuse)
    with workdir.bind(tmp_path):
        sent, facts = await _send(_frame(tmp_path), _REPORT)
    assert sent == _REPORT
    assert facts["observers"]["report_page"] == {"written": False, "reason": "write_failed:PermissionError"}


@pytest.mark.asyncio
async def test_the_page_carries_the_highlights_the_report_holds(tmp_path, _no_trail):
    provider = _Provider(_GOOD)
    with workdir.bind(tmp_path):
        _, facts = await _send(_frame(tmp_path, _cfg(highlights=True), provider), _REPORT)
    (page,) = tmp_path.glob("*.html")
    html = page.read_text(encoding="utf-8")
    assert '<p class="figure-value">1.2 s</p>' in html and '<ol class="timeline">' in html
    assert facts["observers"]["report_page"]["highlights"]["figures_kept"] == 2
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_a_failed_selection_still_writes_the_page(tmp_path, _no_trail):
    with workdir.bind(tmp_path):
        _, facts = await _send(_frame(tmp_path, _cfg(highlights=True), _Provider(error=RuntimeError("x"))), _REPORT)
    record = facts["observers"]["report_page"]
    assert record["written"] is True and record["highlights"] == {
        "selected": False,
        "reason": "call_failed:RuntimeError",
    }
    assert 'class="figures"' not in next(tmp_path.glob("*.html")).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_the_page_is_off_unless_the_flow_asks_for_it_under_the_reader_layout(tmp_path, _no_trail):
    reader_only = FlowConfig(enabled=True, final_shape={"report_depth": True, "report_layout": "reader"})
    sections = FlowConfig(enabled=True, report_page={"enabled": True})
    for cfg in (reader_only, sections):
        frame = _chain(cfg, None, tmp_path)[0]
        with workdir.bind(tmp_path):
            sent, facts = await _send(
                frame, _REPORT if cfg is reader_only else "## Answer\na\n\n## Findings\nb\n\n## Limitations\nc\n"
            )
        assert "Report page" not in sent and not list(tmp_path.glob("*.html"))
        assert "report_page" not in facts.get("observers", {})


def test_the_contract_says_the_flow_writes_the_page_only_when_it_does():
    """Told nothing, a model asked for "the report as HTML" writes one by hand, and the page replaces it."""
    said = "Do not write an HTML file of the report yourself"
    contract = " ".join(render_identity_and_contract(_cfg())[1].split())
    assert said in contract
    # The page is promised for a reply that is the report: no other reply gets one.
    assert (
        "When this reply is the report in the three parts above, the flow itself writes it as an HTML page" in contract
    )
    # A link check nobody asked for is a second list of the report's sources on the page.
    asked = "When the task asks to show that links were checked, do it once"
    unasked = "When the task does not ask for that, write no link-check table and no section about link checks"
    assert contract.index(asked) < contract.index(unasked)
    off = FlowConfig(enabled=True, final_shape={"report_depth": True, "report_layout": "reader"})
    assert "HTML" not in render_identity_and_contract(off)[1]
    assert "does not ask for that" not in render_identity_and_contract(off)[1]
    # The page is the reader layout's: the knob moves no other clause, and never the identity.
    assert render_parts(report_depth=True, report_page=True) == render_parts(report_depth=True)
    assert render_parts(report_page=True) == render_parts()
    reader = {"report_depth": True, "report_layout": "reader"}
    assert render_parts(**reader, report_page=True)[0] == render_parts(**reader)[0]


def test_the_contract_closes_the_report_with_its_sources_when_the_flow_writes_the_page():
    contract = " ".join(render_identity_and_contract(_cfg())[1].split())
    limits = "The limits, as the `##` section that closes the body"
    sources = "After the limits, the list of sources closes the report, under the last `##` heading"
    assert contract.index(limits) < contract.index(sources)
    assert f"`## {LIMITS_HEADINGS['zh']}` in Chinese" in contract and "`## \u6765\u6e90` in Chinese" in contract
    assert "Do not close with a list of sources" not in contract
    # A task's length does not count the list.
    assert "leaves out only a link-check table and the list of sources" in contract
    off = FlowConfig(enabled=True, final_shape={"report_depth": True, "report_layout": "reader"})
    plain = " ".join(render_identity_and_contract(off)[1].split())
    assert "Do not close with a list of sources" in plain and "The limits, as the last `##` section" in plain
    assert "\u6765\u6e90" not in plain


def test_the_contract_bounds_a_paragraph_when_the_flow_writes_the_page():
    contract = " ".join(render_identity_and_contract(_cfg())[1].split())
    rule = (
        "Keep each paragraph to one point and to three to five sentences - about 150 to 300 "
        "characters in Chinese, 60 to 120 words in English - and never past 500 characters or 200 words"
    )
    assert rule in contract
    off = FlowConfig(enabled=True, final_shape={"report_depth": True, "report_layout": "reader"})
    assert "Keep each paragraph" not in " ".join(render_identity_and_contract(off)[1].split())


def _section(body: str) -> str:
    page = render_page(f"> The answer.\n\n## One\n{body}\n\n## Limitations\nc\n")
    return page[page.index('id="section-1"') : page.index('id="section-2"')]


def test_a_list_of_names_is_set_as_terms_and_a_numbered_one_as_steps():
    terms = _section("- **Scope**: who owns the work.\n- **Event:** what happened.\n- **Plugin** \u2014 a package.")
    assert (
        '<dl class="terms"><div><dt>Scope</dt><dd>who owns the work.</dd></div>'
        "<div><dt>Event</dt><dd>what happened.</dd></div><div><dt>Plugin</dt><dd>a package.</dd></div></dl>"
    ) in terms
    steps = _section("3. **Pilot**: ten files.\n4. **Roll out**\n\n   - by team\n   - by service")
    assert '<ol class="steps" style="counter-reset:step 2"><li><p class="step-name">Pilot</p>ten files.</li>' in steps
    assert '<li><p class="step-name">Roll out</p><ul><li>by team</li><li>by service</li></ul></li></ol>' in steps
    zh = _section(
        "- **\u4f5c\u7528\u57df**\uff1a\u51b3\u5b9a\u5f52\u5c5e\u3002\n- **\u4e8b\u4ef6**\uff1a\u8bb0\u5f55\u3002"
    )
    assert "<dt>\u4f5c\u7528\u57df</dt><dd>\u51b3\u5b9a\u5f52\u5c5e\u3002</dd>" in zh


def test_a_list_that_only_opens_in_bold_stays_a_list():
    """Bold with no separator is the subject of a sentence, and a line of its own would break the sentence."""
    for body in (
        "- **Vitest** runs on Vite.\n- **Jest** runs on its own transformer.",
        "- **Scope**: who owns the work.\n- an item with no name",
        "- **Only one item**: so there is nothing to set side by side.",
        "- **" + "a name far too long to be a name " * 3 + "**: text\n- **b**: text",
    ):
        section = _section(body)
        assert "<ul><li>" in section and 'class="terms"' not in section, body


def test_a_lead_sentence_in_bold_is_a_name_and_keeps_its_stop():
    """What a model writes unasked: a numbered list of conclusions, each a bold sentence and its grounds."""
    points = _section(
        "1. **Speed favours Vitest.** No like-for-like figure exists.\n2. **Mocks cost most.** The semantics differ."
    )
    assert '<li><p class="step-name">Speed favours Vitest.</p>No like-for-like figure exists.</li>' in points
    zh = _section(
        "- **globals \u9ed8\u8ba4\u5173\u95ed\u3002** Jest \u9ed8\u8ba4\u6ce8\u5165\u3002\n- **\u4e0d\u652f\u6301 done\u3002** \u6539\u5199\u3002"
    )
    assert "<dt>globals \u9ed8\u8ba4\u5173\u95ed\u3002</dt><dd>Jest \u9ed8\u8ba4\u6ce8\u5165\u3002</dd>" in zh


def test_a_run_of_paragraphs_that_open_with_a_name_is_set_as_terms():
    run = _section(
        "Two cases.\n\n**Choose Vitest:** the repo builds with Vite.\n\n**Stay on Jest:** it uses React Native.\n\nThen decide."
    )
    assert (
        '<p>Two cases.</p><dl class="terms"><div><dt>Choose Vitest</dt><dd>the repo builds with Vite.</dd></div>'
        "<div><dt>Stay on Jest</dt><dd>it uses React Native.</dd></div></dl><p>Then decide.</p>"
    ) in run
    # One such paragraph alone opens a passage: no paragraph of that section is set as a term.
    for body in (
        "**Cache:** it is shared.\n\nThen decide.",
        "**Cache.** It is shared.\n\n**Pool.** It forks.\n\nMore on the pool.\n\n**Watch.** It reruns.\n\nMore.",
    ):
        alone = _section(body)
        assert "<p><strong>" in alone and 'class="terms"' not in alone, body


def test_a_quote_in_a_section_is_a_note_and_one_that_names_its_source_a_quotation():
    note = _section("> **Mind the cache**: it is shared.\n>\n> Clear it first.")
    assert '<aside class="note"><p class="note-name">Mind the cache</p><p>it is shared.</p>' in note
    assert "<p>Clear it first.</p></aside>" in note
    quoted = _section("> We do not plan a port.\n>\n> \u2014 [the maintainers](https://example.com/plan), 2026")
    assert (
        '<figure class="quote"><blockquote><p>We do not plan a port.</p></blockquote><figcaption>\u2014 <a href='
        in quoted
    )
    one = _section("> We do not plan a port.\n> -- the maintainers")
    assert "<blockquote><p>We do not plan a port.</p></blockquote><figcaption>-- the maintainers</figcaption>" in one
    # A dash that opens the only line is not a source under a quotation.
    assert '<aside class="note"><p>\u2014 an aside</p></aside>' in _section("> \u2014 an aside")
    # The answer keeps its own form.
    page = render_page("> **Yes**: move.\n\n## One\nx\n\n## Limitations\nc\n")
    assert '<div class="answer"><p><strong>Yes</strong>: move.</p></div>' in page


def test_a_few_short_subsections_are_cards_and_any_other_set_stays_subsections():
    three = _section(
        "Three ways in.\n\n### Managed\nRelay runs it.\n\n### Hooks\nRelay sees it.\n\n### Gateway\n- one\n- two"
    )
    assert (
        '<p>Three ways in.</p><div class="cards is-3"><article class="card"><h3>Managed</h3><p>Relay runs it.</p></article>'
        in three
    )
    assert three.count('<article class="card">') == 3
    two = _section("### A\na\n\n### B\nb")
    assert '<div class="cards"><article class="card"><h3>A</h3>' in two
    for body in (
        "### A\na",
        "### A\na\n\n### B\nb\n\n### C\nc\n\n### D\nd\n\n### E\ne",
        "### A\na\n\n### B\n" + "a long part. " * 60,
        "### A\na\n\n### B\n| k | v |\n|---|---|\n| 1 | 2 |",
        "### A\na\n\n### B\nb\n\n#### B.1\nc",
        "### A\n\n### B\nb",
    ):
        assert 'class="card' not in _section(body), body


def test_the_contract_names_no_page_form_the_reports_did_not_take_up():
    """With the forms named in the clause, two real reports used none they had not used before."""
    contract = " ".join(render_identity_and_contract(_cfg())[1].split())
    assert "the page sets each as a block of its own" not in contract and "one `###` subsection each" not in contract
    assert "never past 500 characters or 200 words" in contract


def test_a_run_of_step_subsections_is_set_as_stages_and_keeps_every_heading():
    """Two real reports wrote a migration plan as seven and eight ``### Step N`` parts."""
    page = _section(
        "Take it in order.\n\n### Step 1: measure\nTime the suite.\n\n### Step 2: pilot\nMove ten files.\n\n"
        "#### Notes\nKeep the old runner.\n\n### Step 3: roll out\nMove the rest.\n\n### Totals\nSix weeks."
    )
    assert '<p>Take it in order.</p><ol class="stages"><li><h3>Step 1: measure</h3><p>Time the suite.</p></li>' in page
    assert "<li><h3>Step 2: pilot</h3><p>Move ten files.</p><h4>Notes</h4><p>Keep the old runner.</p></li>" in page
    # The run ends where the next heading names no step: that part stays a subsection.
    assert "<li><h3>Step 3: roll out</h3><p>Move the rest.</p></li></ol><h3>Totals</h3><p>Six weeks.</p>" in page
    assert 'class="cards' not in page


@pytest.mark.parametrize(
    "body",
    [
        "### Step 1: a\nx\n\n### Step 2: b\ny",
        "### Step 1: a\nx\n\n### Step 2: b\ny\n\n### Step 4: d\nz",
        "### Step 2: b\ny\n\n### Step 3: c\nz\n\n### Step 4: d\nw",
        "### 2.1 Speed\nx\n\n### 2.2 Cache\ny\n\n### 2.3 Cost\nz",
    ],
    ids=["two_steps", "a_gap", "no_first_step", "numbered_subsections"],
)
def test_subsections_that_are_no_run_of_steps_stay_subsections(body):
    assert 'class="stages"' not in _section(body)


_NOTED = (
    "> a\n\n## Speed\nVite starts in 1.2 s.\n\n**Risk**: the gap holds only with the cache on.\n\n"
    "**Conclusion**: Vite is **quicker**.\n\n- Note: in a list it stays an item.\n\n"
    "## Cost\nNote: pin the old runner, or CI breaks.\n\n\u98ce\u9669\uff1a\u7f13\u5b58\u5173\u95ed\u540e\u6301\u5e73\u3002\n\n"
    "## Limitations\n**Note**: no benchmark on one machine was found.\n"
)


def test_a_paragraph_that_opens_with_a_risk_or_note_label_is_a_note_under_it():
    page = render_page(_NOTED)
    assert (
        '<p>Vite starts in 1.2 s.</p><aside class="note"><p class="note-name">Risk</p>'
        "<p>the gap holds only with the cache on.</p></aside>" in page
    )
    # Bold or not, in either language; the label is the report's own.
    assert '<aside class="note"><p class="note-name">Note</p><p>pin the old runner, or CI breaks.</p></aside>' in page
    assert '<p class="note-name">\u98ce\u9669</p><p>\u7f13\u5b58\u5173\u95ed\u540e\u6301\u5e73\u3002</p>' in page
    # A verdict is no note, nor a list item or the limits.
    assert "<p><strong>Conclusion</strong>: Vite is <strong>quicker</strong>.</p>" in page
    assert page.count('class="note"') == 3 and "no benchmark on one machine was found." in page


def test_a_card_is_never_boxed_as_a_note():
    report = (
        "> a\n\n## Memory\nTwo markets.\n\n### DRAM\n**Risk**: HBM takes the wafers.\n\n"
        "### NAND\nEnterprise demand leads.\n\n## Limitations\nnone\n"
    )
    page = render_page(report)
    assert 'class="cards"' in page and 'class="note"' not in page
    assert "<p><strong>Risk</strong>: HBM takes the wafers.</p>" in page


def test_a_paragraph_too_long_for_a_box_is_no_note():
    long = "the gap holds only with the cache on. " * 30
    page = render_page(f"> a\n\n## Speed\n**Risk**: {long}\n\n## Limitations\nnone\n")
    assert 'class="note"' not in page


def test_the_highlights_pick_no_notes():
    highlights, counters = check_highlights(_offer(notes=["Vite starts in 1.2 s."]), _NOTED)
    assert not highlights and "notes_kept" not in counters and "notes" not in _SYSTEM


def test_the_chain_builds_the_page_writer_from_the_flows_configuration(tmp_path):
    frame = _chain(_cfg(highlights=True), _Provider(_GOOD), tmp_path)[0]
    assert isinstance(frame, TurnFrame) and isinstance(frame._page, ReportPage)


def test_the_shipped_product_turns_the_page_and_its_highlights_on():
    flow = json.loads((REPO / "agents" / "raven-research" / "config.json").read_text())["plugins"]["config"][
        "research-flow"
    ]
    cfg = FlowConfig.from_slice(flow)
    assert cfg.report_page.enabled and cfg.report_page.highlights and cfg.report_page.remove_markdown
    assert cfg.final_shape.effective_layout == "reader"


# --------------------------------------------------------------------------- #
# The close: the limits, then the sources                                      #
# --------------------------------------------------------------------------- #


def test_a_report_with_no_source_list_is_closed_with_the_one_the_page_numbers():
    closed, counts = close_with_sources(_REPORT)
    assert counts == {"sources_listed": 2}
    # Each address once, in the order the report first links it.
    assert closed.endswith(
        "## Sources\n\n- vite.dev: <https://vite.dev/guide/why.html>\n- webpack.js.org: <https://webpack.js.org/concepts/>"
    )
    assert closed.startswith(_REPORT.rstrip())
    assert close_with_sources(closed) == (closed, {})
    page = render_page(closed)
    # The list is the page's source list: no second one, and the limits keep their place.
    assert 'id="sources"' not in page and page.count('<li id="source-1">') == 1 and "Sources: 2" in page
    assert re.findall(r'<h2>([^<]*)</h2><div class="panel is-limits">', page) == ["Limitations"]
    assert page.count('<sup class="cite"><a href="#source-1">1</a></sup>') == 2


def test_the_list_takes_the_label_of_the_reports_language_and_holds_a_single_source():
    report = "> \u7528 Vite\u3002\n\n## \u901f\u5ea6\n[Vite](https://vite.dev/guide/) \u66f4\u5feb\u3002\n\n## \u672a\u6838\u9a8c\u9879\n\u65e0\u3002\n"
    closed, counts = close_with_sources(report, "zh")
    assert counts == {"sources_listed": 1}
    assert closed.endswith(f"## {t_in('zh', 'Sources')}\n\n- vite.dev: <https://vite.dev/guide/>")
    page = render_page(closed, language="zh")
    assert 'id="sources"' not in page and page.count('<li id="source-1">') == 1
    assert re.findall(r'<div class="panel is-limits">', page) == ['<div class="panel is-limits">']
    assert page.index("is-limits") < page.index('<li id="source-1">')


def test_a_report_that_links_nothing_gets_no_list():
    report = "> yes\n\n## One\na\n\n## Limitations\nnone\n"
    assert close_with_sources(report) == (report, {})


def test_sources_grouped_under_label_lines_are_all_on_the_page():
    """A real report put a label line directly above "3.": the page listed the first group only."""
    report = (
        "> a\n\n## One\n[A](https://a.example/x), [B](https://b.example/x) and [C](https://c.example/x).\n\n"
        "## Limitations\nnone\n\n## Sources\n\nDocs\n1. A: https://a.example/x\n2. B: https://b.example/x\n\n"
        "Community\n3. C: https://c.example/x\n4. D: https://d.example/x\n"
    )
    assert "Sources: 2" in render_page(report) and "#source-3" not in render_page(report)
    text, counts = page_module.tidy(report, None)
    assert counts == {"lists_parted": 1} and text == report.replace("Community\n", "Community\n\n")
    page = render_page(text)
    assert "Sources: 4" in page and '<li id="source-4">' in page and "<p>Community</p>" in page
    assert 'rel="noopener noreferrer">C</a><sup class="cite"><a href="#source-3">3</a></sup>' in page


def test_a_source_list_written_before_the_limits_is_moved_after_them_whole():
    report = (
        "# T\n\n> a\n\n## One\nb [x](https://a.example/1) [y](https://b.example/2)\n\n"
        "## References\n- [A](https://a.example/1) - the first\n- [B](https://b.example/2) - the second\n\n"
        "## Limitations\n```\n## not a heading\n```\nc\n"
    )
    closed, counts = close_with_sources(report)
    assert counts == {"sources_moved": 1}
    assert [line for line in closed.splitlines() if line.startswith("## ")][-3:] == [
        "## Limitations",
        "## not a heading",
        "## References",
    ]
    assert sorted(closed.split()) == sorted(report.split())
    assert close_with_sources(closed) == (closed, {})
    page = render_page(closed)
    assert re.findall(r'<h2>([^<]*)</h2><div class="panel is-limits">', page) == ["Limitations"]


@pytest.mark.parametrize(
    "tail",
    [
        "## 5. References\n{refs}\n\n## 6. Limitations\nc\n",
        "## \u516d\u3001\u5f15\u7528\n{refs}\n\n## \u4e03\u3001\u5c40\u9650\nc\n",
        "## References\n{refs}\n\n## Link check\n" + _LINK_CHECK + "\n\n## Limitations\nc\n",
        "## Limitations\nc\n\n## References\n{refs}\n",
    ],
    ids=["numbered", "numbered_zh", "two_listings", "already_last"],
)
def test_a_close_the_flow_cannot_move_safely_stays_as_written(tail):
    report = "> a\n\n## One\nb [1] [2]\n\n" + tail.format(refs=_AS_LIST)
    assert close_with_sources(report) == (report, {})


@pytest.mark.asyncio
async def test_the_report_file_and_the_page_close_with_the_same_list(tmp_path, _no_trail):
    report = tmp_path / "vite.md"
    report.write_text(_REPORT, encoding="utf-8")
    with workdir.bind(tmp_path):
        reply, facts = await _send(_frame(tmp_path), _REPORT, [_wrote(report)])

    held = report.read_text(encoding="utf-8")
    assert held.rstrip().endswith("- webpack.js.org: <https://webpack.js.org/concepts/>")
    assert facts["observers"]["report_file"]["sources_listed"] == 2
    html = (tmp_path / "vite.html").read_text(encoding="utf-8")
    assert facts["observers"]["report_page"]["follows"] == "file"
    assert 'id="sources"' not in html and html.count('<li id="source-2">') == 1
    # The reply the conversation shows is the model's own text: the list is the file's and the page's.
    assert "## Sources" not in reply


def test_a_bold_subsection_number_opening_a_paragraph_is_read_as_that_subheading():
    # "**(1) Demand: totals fell.** text" in Chinese parentheses and numerals, the way a model
    # writes the ``###`` subheadings it was asked for; a bold lead with no number stays a lead.
    report = (
        "> The answer.\n\n## Supply and demand\n\n"
        "**\uff08\u4e00\uff09Demand: totals fell.** Industrial use held up.\n\n"
        "**\uff08\u4e8c\uff09Supply**\n\nMine output was flat.\n\n"
        "**(3) Stocks** Vaults kept filling.\n\n"
        "**Outlook.** Rates decide the next leg.\n"
    )
    body = render_page(report).split("<main>", 1)[1]
    assert re.findall(r"<h3>(.*?)</h3>", body) == [
        "\uff08\u4e00\uff09Demand: totals fell",
        "\uff08\u4e8c\uff09Supply",
        "(3) Stocks",
    ]
    assert "<p>Industrial use held up.</p>" in body and "<p>Vaults kept filling.</p>" in body
    assert "<strong>Outlook.</strong>" in body


def test_three_paragraphs_opening_with_names_are_numbered_points_and_two_stay_terms():
    three = "**Rates.** Real yields fell.\n\n**Gold.** The ratio narrowed.\n\n**Industry.** Solar demand grew.\n"
    page = render_page("> The answer.\n\n## Drivers\n\n" + three)
    assert page.count('<p class="step-name">') == 3 and '<ol class="steps">' in page and "<dt>" not in page
    two = "**Rates.** Real yields fell.\n\n**Gold.** The ratio narrowed.\n"
    page = render_page("> The answer.\n\n## Drivers\n\n" + two)
    assert page.count("<dt>") == 2 and '<p class="step-name">' not in page
