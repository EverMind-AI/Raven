"""The reader report layout: what the model is asked for, how the bar reads it, and the file.

The reader layout is the three-part report in the form a reader receives - the answer as
the opening blockquote, the body under its own ``##`` headings, the limits last - so the
streamed reply needs no rewrite after it has gone out. The same text is written over the
markdown file the turn saved, so the chat reply and the file are one report.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

import research_flow.flow as flow_module  # noqa: E402
from research_flow.config import FlowConfig  # noqa: E402
from research_flow.flow import ResearchFlowHook, ToolHandles, TurnFrame, build_chain  # noqa: E402
from research_flow.gates.ask_user import is_prose_clarify  # noqa: E402
from research_flow.gates.base import GateCtx  # noqa: E402
from research_flow.gates.report_length import ReportLengthNote  # noqa: E402
from research_flow.gates.report_shape import (  # noqa: E402
    READER_LAYOUT,
    ReportShape,
    ReportShapeGate,
    interrupted_report_fallback,
    render_reminder,
    rewrite_prompt,
)
from research_flow.prompts import render_identity_and_contract, render_parts  # noqa: E402
from research_flow.state import SessionStore  # noqa: E402
from research_flow.support.report_file import (  # noqa: E402
    file_digest,
    file_text,
    length_bound,
    length_verdict,
    measure_report,
    still_held,
    sync_report_file,
    touched_markdown,
    written_markdown,
)

from raven.agent.loop import turn_synthesis  # noqa: E402
from raven.contracts.loop_hooks import AgentHookContext  # noqa: E402
from raven.security.trust import wrap_untrusted  # noqa: E402

# A Chinese reply in the reader layout. Spelled as escapes: two numbered body
# headings and the "could not be verified" limits heading.
_ZH = "> yes\n> because\n\n## \u4e00\u3001first\nbody\n\n## \u4e8c\u3001second\nbody\n\n## \u672a\u80fd\u6838\u5b9e\nnone material\n"
_EN = "> yes, because.\n\n## What the sources show\nbody\n\n## Limitations\nnone material\n"
# The model's own draft of ``_EN``: other text under the same ``##`` headings.
_DRAFT = "draft answer\n\n## What the sources show\nan earlier body\n\n## Limitations\nnot yet\n"


def _reader_cfg(**final_shape) -> FlowConfig:
    return FlowConfig(enabled=True, final_shape={"report_depth": True, "report_layout": "reader", **final_shape})


# --------------------------------------------------------------------------- #
# Reading a draft                                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("text", [_ZH, _EN, "# A title\n\n" + _EN], ids=["zh", "en", "titled"])
def test_the_reader_layout_reads_as_all_three_parts(text):
    shape = ReportShape(text, READER_LAYOUT)
    assert shape.well_formed
    assert shape.annotated == ()
    assert shape.in_order


def test_the_old_three_headings_still_deliver_every_part():
    """History written in the old form must not cost a bounce: the content is all there."""
    shape = ReportShape("## Answer\nx\n\n## Findings\ny\n\n## Limitations\nz", READER_LAYOUT)
    assert shape.well_formed
    assert shape.annotated == ("Answer",)


def test_a_body_without_headings_counts_under_an_answer_quote():
    shape = ReportShape("> x\n\nthe body as prose\n\n## Limitations\nz", READER_LAYOUT)
    assert shape.well_formed
    assert shape.annotated == ("Findings",)


@pytest.mark.parametrize(
    ("text", "missing"),
    [
        ("> x\n\n## Part\nb", ("Limitations",)),
        (">\n\n## Part\nb\n\n## Limitations\nz", ("Answer",)),
        ("> x\n\n## Limitations\nz", ("Findings",)),
        ("Before I start: which region do you mean?", ("Answer", "Findings", "Limitations")),
    ],
    ids=["no_limits", "empty_quote", "no_body", "prose_clarify"],
)
def test_a_missing_part_is_named(text, missing):
    assert ReportShape(text, READER_LAYOUT).missing == missing


def test_a_section_after_the_limits_is_recorded_out_of_order():
    shape = ReportShape(_EN + "\n## Appendix\nmore", READER_LAYOUT)
    assert shape.well_formed
    assert not shape.in_order


def test_a_first_limits_heading_under_a_body_stays_the_limits():
    """With the body written as prose above it, a listed first heading is the limits, so a
    section after it is still out of order rather than becoming the limits."""
    shape = ReportShape("> x\n\nthe body in prose\n\n## Limitations\nn\n\n## Link checks\nt\n", READER_LAYOUT)
    assert shape.well_formed
    assert not shape.in_order


def test_the_sections_layout_reads_exactly_as_before():
    """The reader parse is opt-in: the old layout's verdicts must not move."""
    assert ReportShape(_EN).missing == ("Answer", "Findings")
    assert ReportShape("## Answer\nx\n\n## Findings\ny\n\n## Limitations\nz").well_formed


# --------------------------------------------------------------------------- #
# What the model is asked for                                                  #
# --------------------------------------------------------------------------- #


def test_the_prompt_asks_for_the_reader_form():
    contract = render_identity_and_contract(_reader_cfg())[1]
    assert "opening blockquote" in contract
    assert "no label comes before the answer" in contract
    assert "no bold label" in contract
    assert "the blockquote gives the\n   deciding ones" in contract
    assert "over the short form the identity" in contract and "limits are\n   present every time" in contract
    assert "cites the file's absolute path instead" in contract
    assert "Conclusion" not in contract
    assert "write no fetch status, HTTP code" in contract
    assert "listing every URL the report cites, none left out" in contract
    assert "A link-check table has one row for each URL that line counts." in contract
    assert "blocked beside it when the fetch says\n   the site blocked it" in contract
    assert (
        "When the task names the report's sections, those names are the\n   headings, word for word with their numbering"
        in contract
    )
    assert "no English word in the heading" in contract
    assert "When the\n   task sets a length, keep the body inside it" in contract
    assert "is the blockquote's last line, naming\n   the file's path, not a section of its own" in contract
    assert "Let the\n   report run as long" not in contract
    assert "`[report length: ...]` line measured from that file" in contract
    assert "it covers prose and tables alike and leaves out\n   only a link-check table" in contract
    assert "Anything the task asks the report to open with" in contract
    assert "that line stays in this reply\n   and is left out of the saved file" in contract
    assert "last `##` section" in contract
    assert "## Answer" not in contract
    assert "## Findings" not in contract


def test_the_reader_form_asks_for_the_whole_subject_and_plain_prose():
    """A report on one segment of a market, worded half in English, read as the whole answer."""
    for cfg in (_reader_cfg(), _reader_cfg(report_style="outline")):
        contract = " ".join(render_identity_and_contract(cfg)[1].split())
        assert "Cover the whole subject the question names." in contract
        assert "the report is about work that first appeared inside it" in contract
        assert "`after:YYYY-MM-DD before:YYYY-MM-DD`" in contract
        assert "never by a page's crawl or render timestamp" in contract
        assert "survey before you dig" in contract
        assert "the blockquote names it and says why" in contract
        assert "every sentence parses" in contract
        assert "keep an English word only for a proper name" in contract
        assert "let no English verb, adjective or common noun stand" in contract
        assert "use the mainland terms, never a Taiwan or Hong Kong variant" in contract
        assert "say in the same sentence what each one measures" in contract
    sections = " ".join(render_parts(report_depth=True)[1].split())
    assert "Cover the whole subject" not in sections
    assert "every sentence parses" not in sections


def test_the_reader_clause_keeps_every_evidence_rule_of_the_deep_clause():
    """Derived, not copied: the rules between the edited passages are one text."""
    deep = render_parts(report_depth=True)[1]
    reader = render_parts(report_depth=True, report_layout=READER_LAYOUT)[1]
    for rule in (
        "A number you did not read on a page you opened",
        "A table that carries a rank and a total is ordered by that total",
        "An identifier is not that",
        "Write for a reader who is sharp but new to the topic",
        "put the URL in a markdown link on the words it supports",
    ):
        assert rule in deep and rule in reader, rule


def test_the_reader_form_exists_only_for_the_deep_clause():
    assert render_parts(report_layout=READER_LAYOUT) == render_parts()
    assert FlowConfig(final_shape={"report_layout": "reader"}).final_shape.effective_layout == "sections"


def test_the_sections_layout_renders_the_bytes_it_always_did():
    assert render_parts(report_depth=True, report_layout="sections") == render_parts(report_depth=True)


def test_the_reminder_and_the_rewrite_name_the_reader_parts():
    reminder = render_reminder("", READER_LAYOUT)
    assert "opening blockquote" in reminder and "## Answer" not in reminder
    ask = rewrite_prompt(("Answer", "Limitations"), READER_LAYOUT)
    assert "the opening answer blockquote" in ask
    assert "the closing section on what could not be verified" in ask
    assert "## Answer" not in ask
    # The rule the contract states, carried where recency puts it closest to the reply.
    assert "no English word in the heading" in reminder and "no English word in the heading" in ask
    # A rewrite regenerates the headings, so the brief's own section names ride along.
    assert "every section name the task gave, word for word with its numbering" in ask


def test_the_reminder_checklist_points_at_the_reader_limits():
    reminder = render_reminder("Give me 3 to 5 options", READER_LAYOUT)
    assert "name it in the closing section on what could not be verified" in reminder
    assert "`## Limitations` and say why" in render_reminder("Give me 3 to 5 options")


# --------------------------------------------------------------------------- #
# The outline style                                                            #
# --------------------------------------------------------------------------- #

# An outline report: title, numbered sections and subsections, labelled conclusions, a list
# of parallel items, a table, the fixed limits and a sources list one per line.
_OUTLINE = (
    "# Personal agents in 2026\n\n"
    "> Agents move from answering to acting; memory and permissions decide who wins.\n\n"
    "## 1. What a personal agent is\n\n"
    "**Conclusion**: it acts for **one person**, across sessions.\n\n"
    "Planning sets it apart ([IBM](https://www.ibm.com/a)).\n\n"
    "### 1.1 Against assistants\n\nAssistants answer; agents act ([Baidu](https://intl.cloud.baidu.com/b)).\n\n"
    "### 1.2 Against enterprise agents\n\nThe data boundary differs ([Deloitte](https://www.deloitte.com/c)).\n\n"
    "## 2. Who builds them\n\n**Conclusion**: **platforms** lead. Three groups:\n\n"
    "- **Platforms**: agents ship in the OS ([Wired](https://www.wired.com/d)).\n"
    "- **Model labs**: agents ship with models ([Simular](https://www.simular.ai/e)).\n"
    "- **Vertical apps**: one task end to end ([Timeless](https://www.timeless.ai/f)).\n\n"
    "| Vendor | Memory | Source |\n|---|---|---|\n| A | yes | https://www.wired.com/d |\n\n"
    "## Limitations\n\nMarket sizes were not checked.\n\n"
    "## Sources\n\n"
    "IBM, agents vs assistants, https://www.ibm.com/a\n"
    "Baidu, architecture, https://intl.cloud.baidu.com/b\n"
    "Deloitte, workplace agents, https://www.deloitte.com/c\n"
    "Wired, agent test, https://www.wired.com/d\n"
    "Simular, best agents, https://www.simular.ai/e\n"
    "Timeless, statistics, https://www.timeless.ai/f\n"
)


def test_the_outline_style_asks_for_a_title_numbers_a_lead_table_and_bold_to_skim():
    reader = {"report_depth": True, "report_layout": READER_LAYOUT, "report_page": True}
    prose = render_parts(**reader)[1]
    outline = render_parts(**reader, report_style="outline")[1]
    assert "one `#` line that names the topic and its scope" in outline
    assert "## \u4e00\u3001..." in outline and "### \uff08\u4e00\uff09..." in outline
    assert "The limits and the list of sources keep their fixed headings, unnumbered." in outline
    assert "conclusion in\n   one sentence, with no label before it." in outline
    # Principles, not a rulebook: a survey opens on its comparison table, the form follows
    # the content, and the bold alone carries the findings.
    assert "the first numbered section sets\n   them side by side in one table" in outline
    assert "table where the reader compares items across the same\n   measures" in outline
    assert "so that the bold alone gives the report's findings" in " ".join(outline.split())
    # Bold goes by what a paragraph holds - names, figures, a short verdict - never by
    # where a sentence stands, and never on a whole sentence.
    assert "by\n   what a paragraph holds, not by where it stands" in outline
    assert "never a whole\n   sentence, the opening one included" in outline
    assert "paragraph may open with its point as one bold sentence" not in outline
    # The argument's last step is a part on what is still open, under a heading that says so.
    assert "`Open questions and next steps`" in outline and "break that reading" not in outline
    assert "\u5f00\u653e\u95ee\u9898\u4e0e\u540e\u7eed\u65b9\u5411" in outline
    assert "break that reading" in prose
    for gone in (
        "<where things stand now>",
        "not\n   every name the paragraph mentions",
        "the page sets two to four",
        "\u7ed3\u8bba",
        "Conclusion**",
    ):
        assert gone not in outline, gone
    for kept in (
        "A number you did not read on a page you opened",
        "Keep each\n   paragraph to one point",
        "no bold label",
        "`## \u672a\u6838\u9a8c\u9879` in Chinese",
        "`## \u6765\u6e90` in Chinese",
    ):
        assert kept in outline, kept
    # Without the page, the outline closes with no list of sources.
    bare = render_parts(report_depth=True, report_layout=READER_LAYOUT, report_page=False, report_style="outline")[1]
    assert "`## \u6765\u6e90`" not in bare and "The limits keep their heading unnumbered." in bare
    assert "no heading stands above the body as a\n   whole" in prose and "#` line" not in prose
    assert render_parts(**reader, report_style="prose") == render_parts(**reader)


def test_without_limits_the_pages_report_is_the_answer_and_the_body_closing_with_its_sources():
    reader = {"report_depth": True, "report_layout": READER_LAYOUT, "report_page": True}
    for style in ("outline", "prose"):
        bare = render_parts(**reader, report_style=style, report_limits=False)[1]
        assert "in two parts" in bare and "the report in the two parts above" in bare
        assert "Limitations" not in bare and "\u672a\u6838\u9a8c\u9879" not in bare
        assert "Write no section on what could not be verified" in bare
        assert "`## \u6765\u6e90` in Chinese" in bare and "its limits are" not in bare
    assert (
        "The list of sources keeps its fixed heading, unnumbered."
        in render_parts(**reader, report_style="outline", report_limits=False)[1]
    )
    # Only the page's report closes without its limits.
    off_page = {**reader, "report_page": False}
    assert render_parts(**off_page, report_limits=False) == render_parts(**off_page)


def test_without_limits_the_bar_reminder_and_wrap_up_ask_for_none():
    report = "> yes\n\n## \u4e00\u3001A\nb\n\n## \u4e8c\u3001B\nc\n\n## Sources\n- [A](https://a.example)\n"
    bare = ReportShape(report, READER_LAYOUT, True, limits=False)
    assert bare.well_formed and bare.present == ("Answer", "Findings")
    # With limits in the contract, the last body section stands in for them, annotated.
    assert ReportShape(report, READER_LAYOUT, True).annotated == ("Limitations",)
    assert ReportShape(
        "> yes\n\n## Sources\n- [A](https://a.example)\n", READER_LAYOUT, True, limits=False
    ).missing == ("Findings",)
    reminder = render_reminder("", READER_LAYOUT, True, True, limits=False)
    assert "in its two parts" in reminder and "`#` title line" in reminder
    assert "with no section on what could not be verified" in reminder
    ask = rewrite_prompt(("Answer",), READER_LAYOUT, True, limits=False)
    assert "in its two\n" in ask or "in its two " in ask
    assert "closing with the list of the pages" in ask
    fallback = interrupted_report_fallback("stopped", READER_LAYOUT, "en", limits=False)
    assert "Limitations" not in fallback and ReportShape(fallback, READER_LAYOUT, True, limits=False).well_formed


def test_the_outline_style_touches_only_the_reader_layout():
    assert render_parts(report_style="outline") == render_parts()
    assert render_parts(report_depth=True, report_style="outline") == render_parts(report_depth=True)
    cfg = _reader_cfg(report_style="outline")
    assert (
        render_identity_and_contract(cfg)[1]
        == render_parts(
            report_depth=True, report_layout=READER_LAYOUT, report_style="outline", require_answer_marker=True
        )[1]
    )
    assert FlowConfig.model_validate({"finalShape": {"reportStyle": "outline"}}).final_shape.report_style == "outline"


def test_the_titled_reminder_and_rewrite_start_at_the_title():
    titled = render_reminder("", READER_LAYOUT, True, True)
    assert "the report's `#` title line, then the report in its three parts" in titled
    assert "`#` title" not in render_reminder("", READER_LAYOUT, True)
    ask = rewrite_prompt(("Answer",), READER_LAYOUT, True, True)
    assert "begin it at the `#` title line" in ask and "begin it at the blockquote directly" not in ask
    assert "begin it at the blockquote directly" in rewrite_prompt(("Answer",), READER_LAYOUT, True)


@pytest.mark.asyncio
async def test_the_turn_reminder_and_the_bar_know_the_outline_style(tmp_path):
    cfg = _reader_cfg(report_bounce=True, report_style="outline")
    ctx = AgentHookContext(session_key="cli:t", inbound_content="report on personal agents")
    decision = await TurnFrame(cfg, SessionStore(tmp_path), None).before_user_inbound(ctx)
    assert "the report's `#` title line" in decision.modified_content
    ctx = AgentHookContext(session_key="cli:t", inbound_content="report on personal agents")
    decision = await TurnFrame(_reader_cfg(), SessionStore(tmp_path), None).before_user_inbound(ctx)
    assert "`#` title" not in decision.modified_content
    chain = build_chain(
        cfg, None, max_iterations=40, context_window_tokens=65536, tools=ToolHandles(), store=SessionStore(tmp_path)
    )
    assert next(o for o in chain if o.name == "ReportShapeGate")._titled is True


def test_an_outline_report_passes_the_bar_and_reads_on_the_page():
    from research_flow.support.report_html import close_with_sources, render_page

    assert ReportShape(_OUTLINE, READER_LAYOUT, True).well_formed
    page = render_page(_OUTLINE, show_limits=False)
    assert "<h1" in page and "Personal agents in 2026" in page
    assert "<h2>1. What a personal agent is</h2>" in page and page.count("<h3") == 2
    assert "Market sizes were not checked" not in page
    assert page.count('class="sources"') == 1 and 'id="sources"' not in page
    assert close_with_sources(_OUTLINE) == (_OUTLINE, {})


@pytest.mark.asyncio
async def test_an_interrupted_turn_gets_the_reader_policy(tmp_path):
    cfg = _reader_cfg(report_bounce=True)
    ctx = AgentHookContext(session_key="cli:t", inbound_content="who founded X?")
    await ResearchFlowHook(cfg, None, ToolHandles(), SessionStore(tmp_path)).before_user_inbound(ctx)

    policy = turn_synthesis(ctx.metadata)
    assert "opening blockquote" in policy.guidance
    assert policy.repair_prompt(_EN) is None
    assert policy.repair_prompt("## Answer\nonly") is not None
    assert ReportShape(policy.format_fallback("Time ran out."), READER_LAYOUT).well_formed
    assert ReportShape(interrupted_report_fallback("x", READER_LAYOUT), READER_LAYOUT).well_formed


# --------------------------------------------------------------------------- #
# The bar and the clarify exemption                                            #
# --------------------------------------------------------------------------- #


class _Response:
    def __init__(self, content):
        self.content = content
        self.has_tool_calls = False
        self.reasoning_content = None


@pytest.mark.asyncio
async def test_the_bar_passes_a_reader_report_and_bounces_a_missing_limits_part():
    bar = ReportShapeGate(layout=READER_LAYOUT)
    assert not (await bar.after_iteration(GateCtx(session_key="s", response=_Response(_EN)))).rollback

    decision = await bar.after_iteration(GateCtx(session_key="s", response=_Response("> x\n\n## A\nb")))
    assert decision.rollback
    assert "the closing section on what could not be verified" in decision.rollback_inject[-1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "heading",
    [
        "\u672a\u80fd\u9a8c\u8bc1\u7684\u5185\u5bb9",
        "\u691c\u8a3c\u3067\u304d\u306a\u304b\u3063\u305f\u70b9",
        "Lo que no se pudo verificar",
    ],
)
async def test_the_last_section_is_the_limits_whatever_language_names_it(heading):
    """The contract asks for the limits in the reply's own words, which no word list covers."""
    bar = ReportShapeGate(layout=READER_LAYOUT)
    reply = f"> the answer\n\n## Body\nb\n\n## {heading}\nn\n"
    assert not (await bar.after_iteration(GateCtx(session_key="s", response=_Response(reply)))).rollback


# A first body heading that names a limit, with the limits in the reply's own words.
_LIMIT_WORD_IN_THE_BODY = [
    "> x\n\n## \u4e00\u3001\u51fa\u53e3\u9650\u5236\u7684\u6f14\u53d8\nb\n\n## \u672a\u80fd\u9a8c\u8bc1\u7684\u5185\u5bb9\nn\n",
    "> x\n\n## \u4eba\u624b\u4e0d\u8db3\u306e\u73fe\u72b6\nb\n\n## \u691c\u8a3c\u3067\u304d\u306a\u304b\u3063\u305f\u70b9\nn\n",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", _LIMIT_WORD_IN_THE_BODY)
async def test_a_first_body_heading_that_names_a_limit_is_the_body(reply):
    """The body comes before the limits: a listed word in the first heading, with no body
    above it, does not make that heading the limits."""
    bar = ReportShapeGate(layout=READER_LAYOUT)
    assert not (await bar.after_iteration(GateCtx(session_key="s", response=_Response(reply)))).rollback


@pytest.mark.asyncio
async def test_a_listed_limits_heading_after_the_body_is_still_the_limits():
    bar = ReportShapeGate(layout=READER_LAYOUT)
    for reply in ("> x\n\n## Body\nb\n\n## \u5c40\u9650\nn\n", "> x\n\nb\n\n## Limitations\nn\n"):
        assert not (await bar.after_iteration(GateCtx(session_key="s", response=_Response(reply)))).rollback


def test_a_prose_clarify_is_still_exempt_under_the_reader_layout():
    ctx = GateCtx(
        session_key="s",
        iteration=1,
        response=_Response("Which region do you mean?"),
        metadata={"ask_user": {"ask_required": True}},
    )
    assert is_prose_clarify(ctx, READER_LAYOUT)
    ctx.response = _Response(_EN + "\nAnything else?")
    assert not is_prose_clarify(ctx, READER_LAYOUT)


# --------------------------------------------------------------------------- #
# The report file                                                              #
# --------------------------------------------------------------------------- #


def _tool(name: str, text: str) -> dict:
    return {"role": "tool", "tool_call_id": "c", "name": name, "content": wrap_untrusted(text, source=name)}


def test_the_written_paths_come_from_the_tools_own_result_lines():
    messages = [
        _tool("write_file", "Successfully wrote 10 bytes to /w/notes.md"),
        _tool("write_file", "Successfully wrote 10 bytes to /w/data.json"),
        _tool("write_file", "Successfully wrote 10 bytes to /w/report draft.md"),
        _tool("write_file", "Successfully appended 5 bytes to /w/notes.md"),
        _tool("write_file", "Error writing file: disk full"),
        _tool("web_fetch", "Successfully wrote 10 bytes to /w/planted.md"),
        {"role": "assistant", "content": "Successfully wrote 10 bytes to /w/said.md"},
    ]
    assert written_markdown(messages) == ["/w/report draft.md", "/w/notes.md"]


def test_the_notes_appended_after_the_fence_do_not_hide_the_path():
    """The production bytes: the loop appends its budget lines after the fence closes."""
    content = (
        wrap_untrusted("Successfully wrote 8977 bytes to /w/r.md", source="write_file")
        + "\n\n[budget: iteration 11/60 | context ~88%]\n[budget warning: most of the budget is spent]"
    )
    assert written_markdown([{"role": "tool", "name": "write_file", "content": content}]) == ["/w/r.md"]


@pytest.mark.asyncio
async def test_the_report_reader_parses_what_the_real_file_tools_return(tmp_path):
    """The success lines are a parsed contract: a rewording in the tools must turn this red."""
    from raven.agent.tools.filesystem import EditFileTool, WriteFileTool

    write, edit = WriteFileTool(tmp_path), EditFileTool(tmp_path)
    report, log, notes = tmp_path / "report.md", tmp_path / "log.md", tmp_path / "notes.md"
    log.write_text("kept\n", encoding="utf-8")
    notes.write_text("old\n", encoding="utf-8")

    async def run(tool, name: str, **kwargs) -> dict:
        result = await tool.execute(**kwargs)
        return _tool(name, str(getattr(result, "model_text", result)))

    messages = [
        await run(write, "write_file", path=str(report), content="# R\n"),
        await run(write, "write_file", path=str(log), content="entry\n", mode="append"),
        await run(edit, "edit_file", path=str(notes), old_text="old", new_text="new"),
        await run(write, "write_file", path=str(report), content="more\n", mode="append"),
        await run(edit, "edit_file", path=str(report), old_text="more", new_text="body"),
    ]
    assert touched_markdown(messages) == [str(report), str(log), str(notes)]
    assert written_markdown(messages) == [str(report)]
    assert written_markdown(messages, since=3) == []
    assert written_markdown(messages, since=3, held=[str(report)]) == [str(report)]
    assert written_markdown(messages, since=4, held=[str(report)]) == [str(report)]

    same = await run(write, "write_file", path=str(report), content=report.read_text(encoding="utf-8"))
    assert "File unchanged" in same["content"]
    assert written_markdown([same]) == [str(report)]


@pytest.mark.asyncio
async def test_an_unchanged_rewrite_names_its_own_path_whatever_the_path_holds(tmp_path):
    """A directory name holding the unchanged line's own words must not end the path early:
    the reader would name ``notes.md`` beside it, a file no tool call wrote."""
    from raven.agent.tools.filesystem import WriteFileTool

    write = WriteFileTool(tmp_path)
    (tmp_path / "notes.md").write_text("## Entries\nthe user's own\n", encoding="utf-8")
    messages = []
    for decoy in (
        "notes.md already holds x/r.md",
        "a.md already holds exactly these 3 bytes, so nothing was written./r.md",
    ):
        path = tmp_path / decoy
        for _ in range(2):
            result = await write.execute(path=str(path), content="## Entries\n")
            messages.append(_tool("write_file", str(getattr(result, "model_text", result))))
        assert written_markdown(messages[-1:]) == [str(path)]


def test_an_append_counts_only_for_a_file_this_turn_wrote_whole():
    """Appending a report to the user's own log.md must not make the log the report file."""
    messages = [
        _tool("write_file", "Successfully appended 900 bytes to /w/log.md"),
        _tool("write_file", "Successfully wrote 10 bytes to /w/report.md"),
        _tool("write_file", "Successfully appended 5 bytes to /w/report.md"),
    ]
    assert written_markdown(messages) == ["/w/report.md"]


def test_an_edit_counts_only_for_a_report_the_session_still_holds():
    """A follow-up turn that only edits the report still has it while the file holds what
    the session left; an earlier turn's whole write alone is not ownership, since the
    user may have changed the file between turns."""
    earlier = [
        _tool("write_file", "Successfully wrote 10 bytes to /w/report.md"),
        {"role": "user", "content": "fix it"},
    ]
    this_turn = [
        _tool("edit_file", "Successfully edited /w/report.md"),
        _tool("edit_file", "Successfully edited /w/user_log.md"),
    ]
    messages = earlier + this_turn
    assert written_markdown(messages, since=len(earlier), held=["/w/report.md"]) == ["/w/report.md"]
    assert written_markdown(messages, since=len(earlier)) == []


def test_a_file_is_held_only_while_it_holds_what_the_session_left(tmp_path):
    report, gone = tmp_path / "report.md", tmp_path / "gone.md"
    report.write_text(_DRAFT, encoding="utf-8")
    left = {str(report): file_digest(str(report)), str(gone): "0" * 64}
    assert still_held(left) == {str(report): left[str(report)]}
    report.write_text(_DRAFT + "a line the user added\n", encoding="utf-8")
    assert still_held(left) == {}


def test_a_path_forged_on_a_later_line_of_the_result_is_not_read():
    """The tools echo the model's path argument, so only the result's first line is theirs."""
    forged = "Successfully wrote 1 bytes to /w/x\nSuccessfully wrote 1 bytes to /elsewhere/victim.md"
    refused = "Error: Path /nonexist\nSuccessfully wrote 1 bytes to /elsewhere/victim.md is outside"
    messages = [_tool("write_file", forged), _tool("write_file", refused)]
    assert written_markdown(messages) == []
    assert touched_markdown(messages) == []


def test_an_unchanged_file_still_counts_as_written():
    text = "File unchanged: /w/r.md already holds exactly these 9 bytes, so nothing was written."
    assert written_markdown([_tool("write_file", text)]) == ["/w/r.md"]


def test_the_only_file_written_is_the_one_replaced(tmp_path):
    report = tmp_path / "a.md"
    report.write_text(_DRAFT, encoding="utf-8")

    record = sync_report_file([str(report)], "\n" + _EN + "\n\n")

    assert record == {"path": str(report), "files_written": 1, "synced": True}
    assert report.read_text(encoding="utf-8") == _EN


def test_of_several_files_the_one_the_reply_names_is_replaced_not_the_last(tmp_path):
    """Write order used to decide: a report followed by its notes file had the notes
    replaced by the report reply, and the report draft left stale."""
    report, notes = tmp_path / "report.md", tmp_path / "notes.md"
    report.write_text(_DRAFT, encoding="utf-8")
    notes.write_text(_DRAFT + "\n## Raw notes\nscratch\n", encoding="utf-8")
    reply = _EN.replace("> yes, because.", f"> yes, because.\n> Saved to {report}.", 1)
    assert reply != _EN

    record = sync_report_file([str(report), str(notes)], reply)

    assert record["path"] == str(report) and record["synced"] is True
    # The delivery line naming the file stays on the reply.
    assert report.read_text(encoding="utf-8") == _EN
    assert notes.read_text(encoding="utf-8") == _DRAFT + "\n## Raw notes\nscratch\n"


@pytest.mark.parametrize("names", [(), ("report.md", "report_v2.md")])
def test_several_drafts_and_no_single_one_named_leaves_every_file_alone(tmp_path, names):
    report, second = tmp_path / "report.md", tmp_path / "report_v2.md"
    report.write_text(_DRAFT, encoding="utf-8")
    second.write_text(_DRAFT, encoding="utf-8")
    reply = _EN + "".join(f"\nSee {name}." for name in names)

    record = sync_report_file([str(report), str(second)], reply)

    assert record == {"files_written": 2, "synced": False, "reason": "report_file_ambiguous"}
    assert report.read_text(encoding="utf-8") == _DRAFT
    assert second.read_text(encoding="utf-8") == _DRAFT


def test_a_lone_notes_file_is_not_the_report(tmp_path):
    """A turn that saved only its notes: none of their headings are the reply's, and the
    reply written over them would erase them."""
    notes = tmp_path / "notes.md"
    held = "# Notes\n\n## Sources to check\na, b\n\n## Open questions\nc\n"
    notes.write_text(held, encoding="utf-8")

    record = sync_report_file([str(notes)], _EN)

    assert record == {"files_written": 1, "synced": False, "reason": "no_report_draft"}
    assert notes.read_text(encoding="utf-8") == held


def test_of_a_report_and_its_notes_the_report_is_replaced_without_being_named(tmp_path):
    report, notes = tmp_path / "report.md", tmp_path / "notes.md"
    report.write_text(_DRAFT, encoding="utf-8")
    notes.write_text("## Sources to check\na, b\n", encoding="utf-8")

    record = sync_report_file([str(report), str(notes)], _EN)

    assert record["path"] == str(report) and record["synced"] is True
    assert notes.read_text(encoding="utf-8") == "## Sources to check\na, b\n"


def test_a_summary_reply_never_replaces_the_longer_report_it_describes(tmp_path):
    report = tmp_path / "r.md"
    full = _EN + "\n".join(f"finding {i}: a long paragraph of evidence" for i in range(40))
    report.write_text(full, encoding="utf-8")

    record = sync_report_file([str(report)], _EN)

    assert report.read_text(encoding="utf-8") == full
    assert record["synced"] is False
    assert record["reason"] == "reply_shorter_than_file"
    assert record["reply_chars"] < record["file_chars"]


def test_the_files_title_survives_a_reply_that_has_none(tmp_path):
    """The chat reply opens at the quote; the file the model wrote opened with its title."""
    report = tmp_path / "r.md"
    report.write_text("# The report\n\n" + _EN, encoding="utf-8")

    record = sync_report_file([str(report)], _EN)

    assert report.read_text(encoding="utf-8") == "# The report\n\n" + _EN
    assert record["title_kept"] is True


def test_a_reply_with_its_own_title_replaces_the_files(tmp_path):
    report = tmp_path / "r.md"
    report.write_text("# Draft title\n\n" + _EN, encoding="utf-8")

    record = sync_report_file([str(report)], "# Final title\n\n" + _EN)

    assert report.read_text(encoding="utf-8") == "# Final title\n\n" + _EN
    assert "title_kept" not in record


def test_a_file_that_cannot_be_written_is_recorded_not_raised(tmp_path, monkeypatch):
    report = tmp_path / "r.md"
    report.write_text(_DRAFT, encoding="utf-8")

    def refuse(*_a, **_k):
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "write_text", refuse)
    record = sync_report_file([str(report)], _EN)
    assert record["synced"] is False
    assert record["reason"].startswith("write_failed:")


async def _run_turn(frame: TurnFrame, messages: list[dict], reply: str) -> tuple[str, dict]:
    facts: dict = {}
    await frame.after_iteration(GateCtx(session_key="cli:t", messages=messages, metadata=facts))
    decision = await frame.after_send(GateCtx(session_key="cli:t", outbound_content=reply, metadata=facts))
    return decision.modified_content or reply, facts


@pytest.mark.asyncio
async def test_the_reply_and_the_file_are_one_text_and_the_trail_stays_on_the_reply(tmp_path, monkeypatch):
    report = tmp_path / "report.md"
    report.write_text(_DRAFT, encoding="utf-8")
    trail = "\n---\n**Research trail** - 3 searches"
    monkeypatch.setattr(flow_module, "ledger_path", lambda: "ledger")
    monkeypatch.setattr(flow_module, "build_appendix", lambda *a: (trail, {"emitted": True}))

    frame = TurnFrame(_reader_cfg(), SessionStore(tmp_path), None)
    sent, facts = await _run_turn(frame, [_tool("write_file", f"Successfully wrote 9 bytes to {report}")], _EN)

    assert report.read_text(encoding="utf-8") == _EN
    assert sent.startswith(_EN.rstrip()) and sent.endswith(trail)
    assert facts["observers"]["report_file"]["synced"] is True
    assert "dr_report_files" not in facts


async def _session_turn(frame: TurnFrame, history: list[dict], calls: list[tuple], reply: str) -> dict:
    """One turn through the frame's own phases, in the loop's order: the tool ``calls``
    (each ``(tool, kwargs)``) run after the turn starts, over the session's history."""
    facts: dict = {}
    await frame.before_user_inbound(GateCtx(session_key="cli:t", inbound_content="go", metadata=facts))
    this_turn = []
    for tool, kwargs in calls:
        result = await tool.execute(**kwargs)
        this_turn.append(_tool(tool.name, str(getattr(result, "model_text", result))))
    messages = history + this_turn
    await frame.after_iteration(GateCtx(session_key="cli:t", messages=messages, turn_base=len(history), metadata=facts))
    await frame.after_send(GateCtx(session_key="cli:t", outbound_content=reply, metadata=facts))
    history.extend(this_turn)
    return facts


@pytest.mark.asyncio
async def test_a_follow_up_turn_that_only_edits_the_report_syncs_it(tmp_path, monkeypatch):
    from raven.agent.tools.filesystem import EditFileTool, WriteFileTool

    monkeypatch.setattr(flow_module, "ledger_path", lambda: "ledger")
    monkeypatch.setattr(flow_module, "build_appendix", lambda *a: ("", {"emitted": False}))
    report = tmp_path / "report.md"
    frame, history = TurnFrame(_reader_cfg(), SessionStore(tmp_path / "state"), None), []
    first = await _session_turn(
        frame, history, [(WriteFileTool(tmp_path), {"path": str(report), "content": _DRAFT})], _EN
    )
    assert first["observers"]["report_file"]["synced"] is True

    edit = {"path": str(report), "old_text": "none material", "new_text": "none at all"}
    facts = await _session_turn(
        frame, history, [(EditFileTool(tmp_path), edit)], _EN.replace("none material", "none at all")
    )

    assert facts["observers"]["report_file"]["synced"] is True
    assert report.read_text(encoding="utf-8") == _EN.replace("none material", "none at all")
    assert "dr_held_report_files" not in facts


@pytest.mark.asyncio
async def test_a_user_edit_between_turns_is_never_written_over(tmp_path, monkeypatch):
    """The session's earlier whole write says what the file held when that turn ended, not
    now: a report the user added to, and a log the session only started, are theirs."""
    from raven.agent.tools.filesystem import EditFileTool, WriteFileTool

    monkeypatch.setattr(flow_module, "ledger_path", lambda: "ledger")
    monkeypatch.setattr(flow_module, "build_appendix", lambda *a: ("", {"emitted": False}))
    write, edit = WriteFileTool(tmp_path), EditFileTool(tmp_path)
    report, log = tmp_path / "report.md", tmp_path / "log.md"
    frame, history = TurnFrame(_reader_cfg(), SessionStore(tmp_path / "state"), None), []
    await _session_turn(
        frame,
        history,
        [(write, {"path": str(report), "content": _DRAFT}), (write, {"path": str(log), "content": "# Log\n"})],
        _EN,
    )
    assert report.read_text(encoding="utf-8") == _EN

    with report.open("a", encoding="utf-8") as fh:
        fh.write("\nthe user's own note\n")
    log.write_text("# Log\n\n" + "".join(f"- entry {i}\n" for i in range(9)), encoding="utf-8")

    # A full report as the reply, long enough that only ownership stands between it and
    # either file.
    reply = _EN.replace("body", "body " + "word " * 100)
    calls = [
        (edit, {"path": str(report), "old_text": "none material", "new_text": "none at all"}),
        (write, {"path": str(log), "content": "\n" + reply, "mode": "append"}),
    ]
    facts = await _session_turn(frame, history, calls, reply)

    assert "the user's own note" in report.read_text(encoding="utf-8")
    assert all(f"- entry {i}" in log.read_text(encoding="utf-8") for i in range(9))
    assert "report_file" not in facts.get("observers", {})


@pytest.mark.asyncio
async def test_a_reply_that_is_not_the_report_leaves_the_file_alone(tmp_path):
    report = tmp_path / "report.md"
    report.write_text("the model's own file", encoding="utf-8")

    frame = TurnFrame(_reader_cfg(), SessionStore(tmp_path), None)
    _, facts = await _run_turn(
        frame, [_tool("write_file", f"Successfully wrote 9 bytes to {report}")], "Which region do you mean?"
    )

    assert report.read_text(encoding="utf-8") == "the model's own file"
    assert facts["observers"]["report_file"] == {"synced": False, "reason": "reply_not_a_report"}


@pytest.mark.asyncio
async def test_the_sections_layout_never_touches_a_file(tmp_path):
    report = tmp_path / "report.md"
    report.write_text("the model's own file", encoding="utf-8")

    frame = TurnFrame(FlowConfig(enabled=True, final_shape={"report_depth": True}), SessionStore(tmp_path), None)
    _, facts = await _run_turn(
        frame,
        [_tool("write_file", f"Successfully wrote 9 bytes to {report}")],
        "## Answer\nx\n\n## Findings\ny\n\n## Limitations\nz",
    )

    assert report.read_text(encoding="utf-8") == "the model's own file"
    assert "report_file" not in facts.get("observers", {})


# --------------------------------------------------------------------------- #
# The measured length                                                          #
# --------------------------------------------------------------------------- #

# Escapes for CJK: two characters of prose, one in a table, one in a heading.
_MEASURED = (
    "> \u4e16\u754c two words\n\n"
    "## \u5c40 Part\n"
    "see [the page](https://example.com/a-long-url) and https://bare.example/x\n\n"
    "| \u9650 | cell |\n|---|---|\n"
)


def test_the_length_splits_prose_from_tables_and_leaves_links_out():
    c = measure_report(_MEASURED)
    assert (c["prose_cjk"], c["table_cjk"]) == (3, 1)
    # "two words" + "Part" + "see the page and"; no URL survives as words.
    assert c["prose_words"] == 2 + 1 + 4
    assert c["table_words"] == 1
    # A section counts its tables too, so one the brief leaves out comes off whole.
    assert c["sections"] == [("(opening)", 2, 2), ("\u5c40 Part", 2, 6)]


def test_the_url_count_is_every_distinct_address_in_the_report():
    """A link-check table that lists every cited URL has exactly this many rows."""
    text = (
        "> answer ([one](https://a.example/x)).\n\n"
        "## \u5c40\n"
        "see https://b.example/y\uff0c\u4ee5\u53ca https://a.example/x.\n\n"
        "| URL | status |\n|---|---|\n"
        "| https://a.example/x | 200 |\n"
        "| https://c.example/z | 403 |\n"
    )
    # a, b and c: the prose's glued full-width comma and closing period are not the address.
    assert measure_report(text)["urls"] == 3


def test_the_length_line_counts_cited_urls_the_way_the_trail_reads_them():
    """Two zh.wikipedia pages cut at their first ideograph would read as one address."""
    text = (
        "| source | status |\n|---|---|\n"
        "| https://zh.wikipedia.org/wiki/\u5317\u4eac | 200 |\n"
        "| https://zh.wikipedia.org/wiki/\u4e0a\u6d77 | 200 |\n"
    )
    assert measure_report(text)["urls"] == 2


def test_write_and_edit_results_name_the_files_to_measure():
    messages = [
        _tool("write_file", "Successfully wrote 10 bytes to /w/r.md"),
        _tool("edit_file", "Successfully edited /w/r.md"),
        _tool("edit_file", "Successfully edited /w/other.md"),
        _tool("write_file", "Successfully wrote 10 bytes to /w/data.json"),
        _tool("write_file", "File unchanged: /w/same.md already holds exactly these 3 bytes, so nothing was written."),
        _tool("web_fetch", "Successfully edited /w/planted.md"),
    ]
    assert touched_markdown(messages) == ["/w/r.md", "/w/other.md"]


@pytest.mark.asyncio
async def test_the_iteration_that_wrote_a_report_is_told_its_length(tmp_path):
    report = tmp_path / "r.md"
    report.write_text(_MEASURED, encoding="utf-8")
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c"}]},
        _tool("write_file", f"Successfully wrote 9 bytes to {report}"),
    ]
    called = _Response("")
    called.has_tool_calls = True
    ctx = GateCtx(session_key="s", messages=messages, response=called)

    note = (await ReportLengthNote().after_iteration(ctx)).append_note

    assert note.startswith(
        f"[report length: {report} - total 4 CJK characters and 8 other words (prose 3/7, tables 1/1);"
    )
    assert "(opening) 2/2; \u5c40 Part 2/6" in note
    assert note.endswith("; 2 distinct URLs cited]")
    # An earlier iteration's write is not this iteration's news.
    messages.append({"role": "assistant", "content": "", "tool_calls": [{"id": "d"}]})
    messages.append(_tool("web_search", "results"))
    assert (await ReportLengthNote().after_iteration(ctx)).append_note is None


@pytest.mark.parametrize(
    ("task", "bound"),
    [
        ("a report of 5k-7.5k\u5b57", (5000, 7500)),
        ("5000-7500\u5b57", (5000, 7500)),
        ("5-7.5k\u5b57", (5000, 7500)),
        ("5000-7.5k\u5b57", (5000, 7500)),
        ("1500 to 2000 words", (1500, 2000)),
        ("\u4e0d\u5c11\u4e8e3000\u5b57", (3000, None)),
        ("3000\u5b57\u4ee5\u5185", (None, 3000)),
        ("at most 800 words", (None, 800)),
        ("compare 3 to 5 vendors", None),
        ("a detailed report", None),
    ],
)
def test_the_length_a_task_sets_is_read(task, bound):
    assert length_bound(task) == bound


def test_the_verdict_counts_a_twentieth_either_side_as_meeting_the_bound():
    assert "within it" in length_verdict(7600, (5000, 7500)) and "do not lengthen or trim" in length_verdict(
        4800, (5000, 7500)
    )
    assert "400 over its ceiling" in length_verdict(7900, (5000, 7500))
    assert "400 under its floor" in length_verdict(4600, (5000, 7500))
    assert "within it" in length_verdict(9000, (3000, None)) and "within it" in length_verdict(10, (None, 3000))


@pytest.mark.asyncio
async def test_the_length_line_says_whether_the_body_meets_the_tasks_bound(tmp_path):
    report = tmp_path / "r.md"
    body = "\u5b57" * 600
    report.write_text(
        f"> a\n\n## One\n{body}\n\n## Sources\n- {'\u5b57' * 300} https://a.example/x\n", encoding="utf-8"
    )
    called = _Response("")
    called.has_tool_calls = True

    def ctx(task):
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c"}]},
            _tool("write_file", f"Successfully wrote 9 bytes to {report}"),
        ]
        gate_ctx = GateCtx(session_key="s", messages=messages, response=called)
        gate_ctx.turn_question = task
        return gate_ctx

    note = (await ReportLengthNote().after_iteration(ctx("about 600-700\u5b57"))).append_note
    # The sources list is not part of the body the task's length counts; the answer and headings are.
    assert "sources list and link-check section left out: 602 against the task's 600-700: within it" in note
    assert note.endswith("so the length is done - do not lengthen or trim the report for it]")
    note = (await ReportLengthNote().after_iteration(ctx("a short note"))).append_note
    assert "against the task's" not in note and note.endswith("distinct URLs cited]")


def test_the_prompt_says_a_body_inside_the_bound_is_done():
    contract = render_identity_and_contract(_reader_cfg())[1]
    assert "a body that\n   meets it is done as far as length goes" in contract
    assert "do not lengthen it for depth or\n   trim it for the count" in contract
    assert "State the report's own length or its number of\n   sources only when the task asks for them" in contract
    assert "the line also says whether the body\n   meets it, and that verdict stands" in contract


def test_only_the_reader_layout_measures(tmp_path):
    def names(final_shape):
        cfg = FlowConfig(enabled=True, final_shape=final_shape)
        chain = build_chain(
            cfg, None, max_iterations=40, context_window_tokens=65536, tools=ToolHandles(), store=SessionStore(tmp_path)
        )
        return [o.name for o in chain]

    reader = {"report_structure": True, "report_depth": True, "report_layout": "reader"}
    assert any("ReportLengthNote" in n for n in names(reader))
    assert not any("ReportLengthNote" in n for n in names({**reader, "report_layout": "sections"}))


def _revision(path, messages, tool="edit_file"):
    """One iteration that changed ``path``: its call, then the tool's own result line."""
    line = f"Successfully edited {path}" if tool == "edit_file" else f"Successfully wrote 9 bytes to {path}"
    messages.append({"role": "assistant", "content": "", "tool_calls": [{"id": "c"}]})
    messages.append(_tool(tool, line))


def _called(messages, metadata, tools=None):
    response = _Response("")
    response.has_tool_calls = True
    return GateCtx(session_key="s", messages=messages, response=response, metadata=metadata, tools=tools)


@pytest.mark.asyncio
async def test_an_edit_that_drops_a_heading_is_named_on_its_result(tmp_path):
    report = tmp_path / "r.md"
    report.write_text("# T\n\n## One\n\ntext\n\n## Two\n\nmore\n", encoding="utf-8")
    gate, messages, metadata = ReportLengthNote(), [], {}
    _revision(report, messages, tool="write_file")
    first = (await gate.after_iteration(_called(messages, metadata))).append_note
    assert "[report structure:" not in first

    report.write_text("# T\n\n## One\n\ntext\nmore\n", encoding="utf-8")
    _revision(report, messages)
    note = (await gate.after_iteration(_called(messages, metadata))).append_note

    assert f"removed the heading `## Two` from {report}" in note
    assert metadata["report_drafts"]["headings_lost"] == 1
    # Put back, nothing is lost the next time.
    report.write_text("# T\n\n## One\n\ntext\n\n## Two\n\nmore\n", encoding="utf-8")
    _revision(report, messages)
    assert "[report structure:" not in (await gate.after_iteration(_called(messages, metadata))).append_note


def test_a_heading_inside_a_code_fence_is_not_a_heading():
    from research_flow.gates.report_length import headings

    assert headings("## A\n```\n## not one\n```\n### B\n") == ["## A", "### B"]


@pytest.mark.asyncio
async def test_the_revision_budget_asks_for_the_reply_then_withholds_the_file_tools(tmp_path):
    report = tmp_path / "r.md"
    report.write_text("## One\n\ntext\n", encoding="utf-8")
    gate, messages, metadata = ReportLengthNote(note_after=2, stop_after=3), [], {}
    tools = [{"function": {"name": n}} for n in ("web_search", "write_file", "edit_file", "read_file")]
    notes = []
    for i in range(4):
        _revision(report, messages, tool="write_file" if i == 0 else "edit_file")
        notes.append((await gate.after_iteration(_called(messages, metadata))).append_note)

    # The first write is the draft, not a revision.
    assert metadata["report_drafts"]["revisions"] == 3
    assert "[report revisions:" not in notes[0] and "[report revisions:" not in notes[1]
    assert "changed in 2 iterations" in notes[2] and "send the reply" in notes[2]
    # The contract states a length only when the task asks for one, and so does the note.
    assert "only when the task asks for it" in notes[2]
    assert "withdrawn" in notes[3]
    offered = (await gate.before_iteration(_called(messages, metadata, tools))).modified_tools
    assert [t["function"]["name"] for t in offered] == ["web_search", "read_file"]


@pytest.mark.asyncio
async def test_without_a_budget_the_file_tools_stay(tmp_path):
    report = tmp_path / "r.md"
    report.write_text("## One\n", encoding="utf-8")
    gate, messages, metadata = ReportLengthNote(), [], {}
    for _ in range(20):
        _revision(report, messages)
        note = (await gate.after_iteration(_called(messages, metadata))).append_note
        assert "[report revisions:" not in note
    tools = [{"function": {"name": "edit_file"}}]
    assert (await gate.before_iteration(_called(messages, metadata, tools))).modified_tools is None


def test_the_budget_is_wired_only_when_the_config_turns_it_on(tmp_path):
    def gate(**extra):
        cfg = FlowConfig(
            enabled=True,
            final_shape={"report_structure": True, "report_depth": True, "report_layout": "reader"},
            **extra,
        )
        chain = build_chain(
            cfg, None, max_iterations=40, context_window_tokens=65536, tools=ToolHandles(), store=SessionStore(tmp_path)
        )
        return next(o for o in chain if o.name == "ReportLengthNote")

    assert gate()._stop_after is None
    on = gate(draft_revisions={"enabled": True, "note_after": 5, "stop_after": 9})
    assert (on._note_after, on._stop_after) == (5, 9)


# --------------------------------------------------------------------------- #
# The delivery line stays on the reply                                         #
# --------------------------------------------------------------------------- #

_DELIVERED = (
    "> yes, because.\n>\n> searched 2026-10-09, papers and project pages\n>\n"
    "> delivered: /w/r.md, 2,300 characters, 26 links\n\n"
    "## What the sources show\nbody, saved as r.md\n\n## Limitations\nnone material\n"
)


def test_the_file_leaves_out_the_quote_line_that_names_it():
    text, dropped = file_text(_DELIVERED, "/w/r.md")
    assert dropped == 2  # the line, and the empty quote line it leaves dangling
    assert text.startswith("> yes, because.\n>\n> searched 2026-10-09, papers and project pages\n\n## What")
    assert "body, saved as r.md" in text  # outside the quote the body may name it
    assert file_text(_DELIVERED.replace("/w/r.md", "r.md"), "/w/r.md")[1] == 2


def test_the_answer_is_found_under_a_line_the_reply_puts_before_it():
    """A model still opens with a line of its own now and then; the receipt under it is a receipt all the same."""
    spoken = "Both files are saved. Here is the report.\n\n" + _DELIVERED
    text, dropped = file_text(spoken, "/w/r.md")
    assert dropped == 2 and "delivered:" not in text
    assert text.startswith("Both files are saved. Here is the report.\n\n> yes, because.")


def test_a_quote_under_the_first_section_is_not_the_answer():
    reply = "the answer, as prose\n\n## A\n> a page said: see /w/r.md\n> and more\n\n## Limitations\nz"
    assert file_text(reply, "/w/r.md") == (reply, 0)
    fenced = "```\n> not a quote, /w/r.md\n> x\n```\n\n## A\nb\n\n## Limitations\nz"
    assert file_text(fenced, "/w/r.md") == (fenced, 0)


def test_a_quote_that_only_names_the_file_is_kept_whole():
    reply = "> report at /w/r.md\n\n## A\nb\n\n## Limitations\nz"
    assert file_text(reply, "/w/r.md") == (reply, 0)
    assert file_text(_EN, "/w/r.md") == (_EN.strip(), 0)


@pytest.mark.asyncio
async def test_the_reply_keeps_the_delivery_line_and_the_file_does_not(tmp_path, monkeypatch):
    report = tmp_path / "r.md"
    report.write_text(_DRAFT, encoding="utf-8")
    monkeypatch.setattr(flow_module, "ledger_path", lambda: "ledger")
    monkeypatch.setattr(flow_module, "build_appendix", lambda *a: ("\n---\n**Research trail**", {"emitted": True}))
    reply = _DELIVERED.replace("/w/r.md", str(report))

    frame = TurnFrame(_reader_cfg(), SessionStore(tmp_path), None)
    sent, facts = await _run_turn(frame, [_tool("write_file", f"Successfully wrote 9 bytes to {report}")], reply)

    assert str(report) in sent
    assert str(report) not in report.read_text(encoding="utf-8")
    assert report.read_text(encoding="utf-8").startswith("> yes, because.")
    assert facts["observers"]["report_file"]["reply_only_lines"] == 2


def test_limits_sharing_their_heading_are_missing_and_stay_on_the_page():
    """A report wrote its advice and its limits under one heading; the page left both off."""
    from research_flow.gates.report_shape import rewrite_prompt
    from research_flow.support.report_html import render_page

    shared = (
        "> The answer.\n\n## \u4e00\u3001Methods\nSLERP and soups.\n\n"
        "## \u5341\u3001Advice \u4e0e \u672a\u6838\u9a8c\u9879\nPick SLERP for twins. The scores were not rerun.\n\n"
        "## Sources\n\n- A https://a.com/x\n- B https://b.com/y\n"
    )
    assert ReportShape(shared, READER_LAYOUT, True).missing == ("Limitations",)
    assert "under its fixed heading and no other" in rewrite_prompt(("Limitations",), READER_LAYOUT, True)
    assert "Pick SLERP for twins" in render_page(shared, show_limits=False)
    alone = shared.replace(
        "## \u5341\u3001Advice \u4e0e \u672a\u6838\u9a8c\u9879\nPick SLERP for twins. ", "## \u672a\u6838\u9a8c\u9879\n"
    )
    assert ReportShape(alone, READER_LAYOUT, True).well_formed
    assert "The scores were not rerun" not in render_page(alone, show_limits=False)
    numbered = alone.replace("## \u672a\u6838\u9a8c\u9879", "## \u4e8c\u3001\u672a\u6838\u9a8c\u9879")
    assert ReportShape(numbered, READER_LAYOUT, True).well_formed
