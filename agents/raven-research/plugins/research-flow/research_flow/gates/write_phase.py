"""The write step (DR flow): the report is written once the research is over, under its rule.

The report rule sits in the system prompt, and by the time a research turn writes, a
hundred thousand tokens of search listings and pages stand between the two. The model
then keeps the rules stated once in a fixed form and drops the ones that ask it to
judge its content's form. This gate makes the writing a step of its own: the first
draft a research turn produces marks the research as over and is set aside, and the
model is asked again with the report rule appended after the research and no tools
offered, so it writes from the evidence with the rule as the last thing it read.

Once per turn. What follows is the flow as it was: the reviewer reads the report the
write step produced, and a rejection reopens the research with the tools back; the
revision is written in the loop, the brief still in the transcript above it. A turn
that did no research - a plain answer, a clarify - is passed through, as is a draft on
an iteration that offered no web tools for another reason than the budget. A round the
budget note closed (``budgetNote.closeAfter``) withdraws them ahead of this gate, and
its draft is the research's end like any other: it is set aside and the report asked for.

With a skim floor, the report the step writes is also read for its bold: when too few
of its body's paragraphs bold a name, a figure or a few words, it is handed back once
with a request for its bold alone, the tools still withdrawn. The rule asks for that
bold, and one run follows it while the next, under the same rule, bolds nothing.
"""

from __future__ import annotations

import logging
import re
import time

from raven.contracts.loop_hooks import HookDecision
from research_flow.gates.base import Gate, GateCtx
from research_flow.prompts import skim_brief
from research_flow.support.answer_text import closing_tag_bar, visible_answer
from research_flow.support.ledger import ledger_append
from research_flow.support.report_html import names_sources

logger = logging.getLogger(__name__)

_LEDGER_OP = "write_phase"
_STATE = "write_phase"
_WEB_TOOLS = frozenset({"web_search", "web_fetch"})
# A report with fewer paragraphs than this is too short for a share to mean anything.
_SKIM_MIN_PARAGRAPHS = 4
# A paragraph or list item shorter than this, links and URLs aside, is not counted.
_SKIM_MIN_CHARS = 60
# The longest bold that still reads as a name, a figure or a few words.
_SKIM_MAX_BOLD = 24
# More short bold spans than this in one paragraph is past the rule's four or five.
_SKIM_DENSE = 6
_SENTENCE_END = tuple(".!?;:\u3002\uff01\uff1f\uff1b\uff1a")
_FENCE_RE = re.compile(r"^```.*?^```[^\n]*$", re.M | re.S)
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_URL_RE = re.compile(r"https?://\S+")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


class WritePhaseGate(Gate):
    """Set aside a research turn's first draft and ask for the report again, tools withdrawn."""

    def __init__(self, brief: str, closing_tag_required: bool = False, skim_floor: float = 0.0) -> None:
        self._brief = brief
        self._closing_tag_required = closing_tag_required
        self._skim_floor = skim_floor

    @property
    def name(self) -> str:
        return "WritePhaseGate"

    async def before_iteration(self, ctx: GateCtx) -> HookDecision:
        state = ctx.metadata.setdefault(_STATE, {})
        if state.get("writing"):
            return HookDecision(modified_tools=[], notes=["write_phase: tools withdrawn for the report"])
        if ctx.tools is not None:
            names = {(t.get("function") or {}).get("name") for t in ctx.tools if isinstance(t, dict)}
            # The budget note runs first and strips the web tools from ``ctx.tools`` once it
            # closes the research; those tools were withdrawn, not absent.
            closed = bool((ctx.metadata.get("budget_note") or {}).get("closed"))
            state["web_tools_offered"] = bool(names & _WEB_TOOLS) or closed
        return HookDecision()

    async def after_iteration(self, ctx: GateCtx) -> HookDecision:
        state = ctx.metadata.setdefault(_STATE, {})
        if state.pop("writing", False):
            return self._written(ctx, state)
        if state.get("asked") or getattr(ctx.response, "has_tool_calls", False):
            return HookDecision()
        draft = self._visible(ctx)
        if not draft:
            return HookDecision()
        if not state.get("web_tools_offered", True):
            return HookDecision(notes=["write_phase: skipped, no web tools this iteration"])
        if not _researched(ctx.messages or [], ctx.turn_base or 0):
            return HookDecision(notes=["write_phase: skipped, no research this turn"])
        state.update(asked=True, writing=True, draft_chars=len(draft))
        ledger_append(
            {
                "ts": time.time(),
                "op": _LEDGER_OP,
                "event": "asked",
                "iteration": ctx.iteration,
                "draft_chars": len(draft),
            }
        )
        logger.warning(
            "write-phase: research over at iteration %s; setting the %d-char draft aside and asking for the report",
            ctx.iteration,
            len(draft),
        )
        return HookDecision(
            rollback=True,
            # Synthetic: the persisted turn keeps the report, not the brief that asked for it.
            rollback_inject=[{"role": "user", "content": self._brief, "_recovery_synthetic": True}],
            notes=["write_phase: draft set aside, report asked for"],
        )

    def _visible(self, ctx: GateCtx) -> str:
        return visible_answer(
            getattr(ctx.response, "content", None) or "",
            closing_tag_required=closing_tag_bar(
                self._closing_tag_required, getattr(ctx.response, "reasoning_content", None)
            ),
        )

    def _written(self, ctx: GateCtx, state: dict) -> HookDecision:
        """The report the step wrote: passed through, or handed back once for its bold."""
        chars = len(getattr(ctx.response, "content", None) or "")
        report = self._visible(ctx)
        if state.get("skim_asked"):
            held, total = skim_count(report)
            state.update(skim_chars=chars, skim_held_after=held, skim_dense_after=skim_dense(report))
            return HookDecision(notes=["write_phase: bold revised"])
        state["written_chars"] = chars
        if self._skim_floor <= 0 or not report or getattr(ctx.response, "has_tool_calls", False):
            return HookDecision(notes=["write_phase: report written"])
        held, total = skim_count(report)
        state.update(skim_held=held, skim_total=total, skim_dense=skim_dense(report))
        if total < _SKIM_MIN_PARAGRAPHS or held >= self._skim_floor * total:
            return HookDecision(notes=["write_phase: report written"])
        state.update(skim_asked=True, writing=True)
        ledger_append(
            {
                "ts": time.time(),
                "op": _LEDGER_OP,
                "event": "skim_asked",
                "iteration": ctx.iteration,
                "held": held,
                "total": total,
            }
        )
        logger.warning(
            "write-phase: %d of %d paragraphs bold a name or a figure; asking for the bold once", held, total
        )
        return HookDecision(
            rollback=True,
            rollback_inject=[
                {"role": "assistant", "content": report, "_recovery_synthetic": True},
                {"role": "user", "content": skim_brief(total - held, total), "_recovery_synthetic": True},
            ],
            notes=["write_phase: report handed back for its bold"],
        )


def _researched(messages: list[dict], turn_base: int) -> bool:
    """Whether this turn called a web tool."""
    return any(
        isinstance(m, dict) and m.get("role") == "tool" and m.get("name") in _WEB_TOOLS for m in messages[turn_base:]
    )


def skim_count(report: str) -> tuple[int, int]:
    """How many of the body's paragraphs and list items bold a name, a figure or a few words, of how many.

    The body is the report under its `##` headings, less the list of sources that closes
    it. Tables, quotes and headings are not counted, nor a paragraph under
    ``_SKIM_MIN_CHARS`` once links and URLs are taken out.
    """
    counted = [sum(map(_short_bold, _BOLD_RE.findall(unit))) for unit in _body_units(report)]
    return sum(1 for spans in counted if spans), len(counted)


def skim_dense(report: str) -> int:
    """How many of the body's counted paragraphs bold more than ``_SKIM_DENSE`` short spans."""
    return sum(1 for unit in _body_units(report) if sum(map(_short_bold, _BOLD_RE.findall(unit))) > _SKIM_DENSE)


def _body_units(report: str) -> list[str]:
    parts = re.split(r"^##(?!#)([^\n]*)$", _FENCE_RE.sub("", report), flags=re.M)
    sections = list(zip(parts[1::2], parts[2::2]))
    # The list of sources is the last section, as ``fixed_closing`` reads it: a body
    # section on data sources ("data and sources") is still body.
    if sections and names_sources(sections[-1][0].strip().strip("#").strip()):
        sections.pop()
    units: list[str] = []
    for _, section in sections:
        for block in re.split(r"\n\s*\n", section):
            for unit in _units(block):
                if len(_URL_RE.sub("", _LINK_RE.sub(r"\1", unit)).replace("**", "").strip()) >= _SKIM_MIN_CHARS:
                    units.append(unit)
    return units


def _units(block: str) -> list[str]:
    """A prose block as one unit, a list as one per item; tables, quotes and headings as none."""
    text = block.strip()
    if not text or text.startswith(("#", "|", ">")):
        return []
    if not _ITEM_RE.match(text):
        return [text]
    items: list[str] = []
    for line in text.splitlines():
        if _ITEM_RE.match(line) or not items:
            items.append(line)
        else:
            items[-1] += " " + line
    return items


def _short_bold(span: str) -> bool:
    words = _LINK_RE.sub(r"\1", span).strip()
    return 0 < len(words) <= _SKIM_MAX_BOLD and not words.endswith(_SENTENCE_END)


__all__ = ["WritePhaseGate", "skim_count", "skim_dense"]
