"""Report length, measured: a line on the tool result of every markdown write.

A research brief often sets a length ("2000-3000 characters"), and the model has no
way to count one. Across four product runs it estimated from ``write_file``'s size
line instead and stated the estimate as the body's length: 40% low on one run, 4%
low and under the brief's floor on the next. This observer measures the file the
iteration just wrote or edited and appends the counts to that tool result, so a
length check and any count the report states rest on a number rather than a guess.

The same measurement can keep a turn revising. On one product run the first draft
stood at iteration 15 and the next 45 iterations, to the iteration cap, edited it a
few characters at a time toward the brief's bound; the edits also dropped ``##``
headings, and each dropped heading read to the model as a broken file it rewrote
whole. So the observer also says when an edit removed a heading, and, with a
revision budget, counts the iterations that changed a report file already written
this turn: at ``note_after`` it asks for the reply, at ``stop_after`` it withholds
the file-writing tools until the turn ends.
"""

from __future__ import annotations

import logging
import re
from collections import Counter

from raven.contracts.loop_hooks import HookDecision
from research_flow.gates.base import Gate, GateCtx
from research_flow.support.report_file import WRITE_TOOL, length_note, read_text, touched_markdown

logger = logging.getLogger(__name__)

_EDIT_TOOL = "edit_file"
_FILE_TOOLS = frozenset({WRITE_TOOL, _EDIT_TOOL})
_HEADING_RE = re.compile(r"#{1,6}[ \t]+\S.*")
_FENCE_MARKS = ("```", "~~~")

_LOST_NOTE = (
    "[report structure: this change removed {lost} from {path}. Put back any heading "
    "that was not meant to go with one edit_file call; the rest of the file is intact, "
    "so do not rewrite it whole.]"
)
_ASK_NOTE = (
    "[report revisions: {path} has been changed in {count} iterations since it was "
    "first written. Stop revising it: make one more change only if a section or a "
    "heading is missing, then send the reply, stating the length the last "
    "[report length] line measured.]"
)
_STOP_NOTE = (
    "[report revisions: {path} has been changed in {count} iterations since it was "
    "first written. write_file and edit_file are withdrawn for the rest of this turn; "
    "send the reply now, from the file as it stands.]"
)


def headings(text: str) -> list[str]:
    """The markdown heading lines of ``text``, in order, fenced code left out."""
    found: list[str] = []
    fenced = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(_FENCE_MARKS):
            fenced = not fenced
            continue
        if not fenced and _HEADING_RE.fullmatch(line):
            found.append(line)
    return found


def _named(lines: list[str]) -> str:
    shown = ", ".join(f"`{line}`" for line in lines[:4])
    more = f" and {len(lines) - 4} more" if len(lines) > 4 else ""
    return f"the heading{'s' if len(lines) > 1 else ''} {shown}{more}"


class ReportLengthNote(Gate):
    """Append ``[report length: ...]`` after an iteration that changed a markdown file.

    ``note_after`` / ``stop_after`` are the revision budget, off when None.
    """

    def __init__(self, *, note_after: int | None = None, stop_after: int | None = None) -> None:
        self._note_after = note_after
        self._stop_after = stop_after

    async def before_iteration(self, ctx: GateCtx) -> HookDecision:
        state = ctx.metadata.get("report_drafts") or {}
        if not state.get("stopped") or ctx.tools is None:
            return HookDecision()
        kept = [t for t in ctx.tools if (t.get("function") or {}).get("name") not in _FILE_TOOLS]
        return HookDecision(modified_tools=kept) if len(kept) != len(ctx.tools) else HookDecision()

    async def before_execute_tools(self, ctx: GateCtx) -> HookDecision:
        state = ctx.metadata.get("report_drafts") or {}
        if state.get("stopped"):
            calls = getattr(ctx.response, "tool_calls", None) or []
            named = sum(1 for call in calls if getattr(call, "name", None) in _FILE_TOOLS)
            if named:
                # Withholding un-offers a tool, it does not unregister it: observation only.
                state["called_when_withheld"] = state.get("called_when_withheld", 0) + named
        return HookDecision()

    async def after_iteration(self, ctx: GateCtx) -> HookDecision:
        if not getattr(ctx.response, "has_tool_calls", False):
            return HookDecision()
        messages = ctx.messages or []
        results: list[dict] = []
        for message in reversed(messages):
            if message.get("role") != "tool":
                break
            results.append(message)
        touched = touched_markdown(reversed(results))
        if not touched:
            return HookDecision()
        state = ctx.metadata.setdefault("report_drafts", {"headings": {}, "revisions": 0})
        seen: dict[str, list[str]] = state["headings"]
        notes: list[str] = []
        revised: str | None = None
        for path in touched:
            if note := length_note(path):
                notes.append(note)
            text = read_text(path)
            now = headings(text) if text is not None else None
            before = seen.get(path)
            if before is not None:
                revised = revised or path
                if now is not None:
                    lost = list((Counter(before) - Counter(now)).elements())
                    if lost:
                        state["headings_lost"] = state.get("headings_lost", 0) + len(lost)
                        notes.append(_LOST_NOTE.format(lost=_named(lost), path=path))
            if now is not None:
                seen[path] = now
        if revised is not None:
            state["revisions"] += 1
            notes.extend(self._budget(state, revised))
        return HookDecision(append_note="\n".join(notes)) if notes else HookDecision()

    def _budget(self, state: dict, path: str) -> list[str]:
        count = state["revisions"]
        if self._stop_after is not None and count >= self._stop_after and not state.get("stopped"):
            state["stopped"] = True
            logger.warning("report-drafts: withdrew the file tools after %d revisions of %s", count, path)
            return [_STOP_NOTE.format(path=path, count=count)]
        if self._note_after is not None and count >= self._note_after and not state.get("asked"):
            state["asked"] = True
            return [_ASK_NOTE.format(path=path, count=count)]
        return []


__all__ = ["ReportLengthNote", "headings"]
