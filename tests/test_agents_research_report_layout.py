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
    file_text,
    measure_report,
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
    assert "no bold lead-in" in contract
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


def test_an_unchanged_file_still_counts_as_written():
    text = "File unchanged: /w/r.md already holds exactly these 9 bytes, so nothing was written."
    assert written_markdown([_tool("write_file", text)]) == ["/w/r.md"]


def test_the_only_file_written_is_the_one_replaced(tmp_path):
    report = tmp_path / "a.md"
    report.write_text("old a", encoding="utf-8")

    record = sync_report_file([str(report)], "\n" + _EN + "\n\n")

    assert record == {"path": str(report), "files_written": 1, "synced": True}
    assert report.read_text(encoding="utf-8") == _EN


def test_of_several_files_the_one_the_reply_names_is_replaced_not_the_last(tmp_path):
    """Write order used to decide: a report followed by its notes file had the notes
    replaced by the report reply, and the report draft left stale."""
    report, notes = tmp_path / "report.md", tmp_path / "notes.md"
    report.write_text("old report", encoding="utf-8")
    notes.write_text("old notes", encoding="utf-8")
    reply = _EN.replace("> yes, because.", f"> yes, because.\n> Saved to {report}.", 1)
    assert reply != _EN

    record = sync_report_file([str(report), str(notes)], reply)

    assert record["path"] == str(report) and record["synced"] is True
    # The delivery line naming the file stays on the reply.
    assert report.read_text(encoding="utf-8") == _EN
    assert notes.read_text(encoding="utf-8") == "old notes"


@pytest.mark.parametrize("names", [(), ("report.md", "notes.md")])
def test_several_files_and_no_single_one_named_leaves_every_file_alone(tmp_path, names):
    report, notes = tmp_path / "report.md", tmp_path / "notes.md"
    report.write_text("old report", encoding="utf-8")
    notes.write_text("old notes", encoding="utf-8")
    reply = _EN + "".join(f"\nSee {name}." for name in names)

    record = sync_report_file([str(report), str(notes)], reply)

    assert record == {"files_written": 2, "synced": False, "reason": "report_file_ambiguous"}
    assert report.read_text(encoding="utf-8") == "old report"
    assert notes.read_text(encoding="utf-8") == "old notes"


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


def test_a_file_that_cannot_be_written_is_recorded_not_raised(tmp_path):
    record = sync_report_file([str(tmp_path / "missing-dir" / "r.md")], _EN)
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
    report.write_text("the model's own draft\nanother structure\n", encoding="utf-8")
    trail = "\n---\n**Research trail** - 3 searches"
    monkeypatch.setattr(flow_module, "ledger_path", lambda: "ledger")
    monkeypatch.setattr(flow_module, "build_appendix", lambda *a: (trail, {"emitted": True}))

    frame = TurnFrame(_reader_cfg(), SessionStore(tmp_path), None)
    sent, facts = await _run_turn(frame, [_tool("write_file", f"Successfully wrote 9 bytes to {report}")], _EN)

    assert report.read_text(encoding="utf-8") == _EN
    assert sent.startswith(_EN.rstrip()) and sent.endswith(trail)
    assert facts["observers"]["report_file"]["synced"] is True
    assert "dr_report_files" not in facts


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


def test_a_quote_that_only_names_the_file_is_kept_whole():
    reply = "> report at /w/r.md\n\n## A\nb\n\n## Limitations\nz"
    assert file_text(reply, "/w/r.md") == (reply, 0)
    assert file_text(_EN, "/w/r.md") == (_EN.strip(), 0)


@pytest.mark.asyncio
async def test_the_reply_keeps_the_delivery_line_and_the_file_does_not(tmp_path, monkeypatch):
    report = tmp_path / "r.md"
    report.write_text("draft\n", encoding="utf-8")
    monkeypatch.setattr(flow_module, "ledger_path", lambda: "ledger")
    monkeypatch.setattr(flow_module, "build_appendix", lambda *a: ("\n---\n**Research trail**", {"emitted": True}))
    reply = _DELIVERED.replace("/w/r.md", str(report))

    frame = TurnFrame(_reader_cfg(), SessionStore(tmp_path), None)
    sent, facts = await _run_turn(frame, [_tool("write_file", f"Successfully wrote 9 bytes to {report}")], reply)

    assert str(report) in sent
    assert str(report) not in report.read_text(encoding="utf-8")
    assert report.read_text(encoding="utf-8").startswith("> yes, because.")
    assert facts["observers"]["report_file"]["reply_only_lines"] == 2
