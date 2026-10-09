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
from research_flow.flow import ResearchFlowHook, ToolHandles, TurnFrame  # noqa: E402
from research_flow.gates.ask_user import is_prose_clarify  # noqa: E402
from research_flow.gates.base import GateCtx  # noqa: E402
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
from research_flow.support.report_file import sync_report_file, written_markdown  # noqa: E402

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
    assert "Conclusion" not in contract
    assert "write no fetch status, HTTP code" in contract
    assert "listing each URL exactly as you fetched it" in contract
    assert "no English word in the heading" in contract
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


def test_an_unchanged_file_still_counts_as_written():
    text = "File unchanged: /w/r.md already holds exactly these 9 bytes, so nothing was written."
    assert written_markdown([_tool("write_file", text)]) == ["/w/r.md"]


def test_the_last_file_written_is_the_one_replaced(tmp_path):
    first, last = tmp_path / "a.md", tmp_path / "b.md"
    first.write_text("old a", encoding="utf-8")
    last.write_text("old b", encoding="utf-8")

    record = sync_report_file([str(first), str(last)], "\n" + _EN + "\n\n")

    assert record == {"path": str(last), "files_written": 2, "synced": True}
    assert last.read_text(encoding="utf-8") == _EN
    assert first.read_text(encoding="utf-8") == "old a"


def test_a_summary_reply_never_replaces_the_longer_report_it_describes(tmp_path):
    report = tmp_path / "r.md"
    full = _EN + "\n".join(f"finding {i}: a long paragraph of evidence" for i in range(40))
    report.write_text(full, encoding="utf-8")

    record = sync_report_file([str(report)], _EN)

    assert report.read_text(encoding="utf-8") == full
    assert record["synced"] is False
    assert record["reason"] == "reply_shorter_than_file"
    assert record["reply_chars"] < record["file_chars"]


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
    report.write_text("# The model's own file\nanother structure\n", encoding="utf-8")
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
