"""Highlights for the report's reading page: selected by a model, checked against the report.

The page can show key figures, key findings and a timeline above the report's body, and
set a few paragraphs of the body as notes. A
model picks them, because picking needs a reading of the report. A model may not word
them: a figure with the wrong label, or a finding restated a shade stronger, would put a
claim on the page that no reviewer saw.

So the model only selects, and this module checks. Every string the model returns must
be text the report holds, and the two halves of a figure or of a dated event must stand
in the same sentence, list item or table row. A string that fails is dropped. A kind
with too few entries left is dropped whole. The page then shows the report without it.

A note is not copied onto the page: the string only names the paragraph, and the page
sets that paragraph as a note where it stands (``report_html.placed_notes``). So a note
picked badly boxes a paragraph of the report, and puts no claim on the page.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

from loguru import logger

from research_flow.support._verdict import json_objects
from research_flow.support.answer_text import visible_answer
from research_flow.support.report_html import Figure, Highlights, Moment, placed_notes
from research_flow.support.report_markdown import parse_blocks, plain_units

if TYPE_CHECKING:
    from raven.contracts.llm_provider import LLMProvider

#: The most entries of each kind, and the fewest that are worth a band on the page.
_FIGURES = (2, 4)
_FINDINGS = (2, 5)
_TIMELINE = (3, 6)
_NOTES = (1, 3)

_VALUE_CHARS = 24
_LABEL_CHARS = 60
#: The fewest characters, spaces apart, that can name a quantity or an event: fewer is a
#: fragment of a phrase. Chinese names things in fewer characters - a two-character label
#: is a name, not a fragment - so text with CJK needs two fewer.
_NAME_CHARS = 4
_CJK_RE = re.compile("[\u3400-\u4dbf\u4e00-\u9fff]")
_EVENT_CHARS = 120
_FINDING_CHARS = (8, 240)
#: A report longer than this is not sent: the selection reads the whole report or none of it.
_REPORT_CHARS = 120_000

_SPACE_RE = re.compile(r"\s+")
_DIGIT_RE = re.compile(r"\d")

_SYSTEM = """You select highlights from a finished research report, for the page a reader opens it on.
You only copy. Every string you return must be one continuous run of characters taken from the
report, character for character, without markdown marks. Never write, shorten, translate or
rephrase: a string that is not in the report is discarded.

Return one JSON object and nothing else:
{"figures": [{"value": "...", "label": "..."}], "findings": ["..."], "timeline": [{"when": "...", "what": "..."}],
 "notes": ["..."]}

- figures: at most 4 numbers the report's answer rests on. "value" is the number with its unit,
  as the report writes it. "label" is the words that say what the number measures, taken from
  the same sentence, list item or table row as the value. A label must make sense when read
  alone beside its number: a name for the quantity, not a fragment that stops mid-phrase. Give
  each figure a label of its own; never the same label twice.
- findings: at most 5 sentences or clauses, each one stating a conclusion of the report.
- timeline: dated events in the history of the subject, only when the report describes a
  sequence of at least 3. "when" is the date as the report writes it. "what" is the words that
  say what happened, taken from the same sentence, list item or table row as the date. The
  dates the report gives for its own work (when it was retrieved or written) are not events.
- notes: at most 3 sentences from paragraphs of the report's body, each from a different `##`
  section. Each one states something the reader must know before acting on that section: the
  condition a result holds under, an exception to it, a step that breaks things when it is
  skipped. Take none from the answer at the top, from the closing section on limits, from a
  list or from a table. A sentence that only states a result is a finding, not a note.

Leave a list empty when the report has nothing of that kind. Most reports have no timeline,
and some have no note."""


def _squeeze(text: str) -> str:
    """``text`` with no whitespace: a model re-spaces what it copies, most of all between scripts."""
    return _SPACE_RE.sub("", text)


def _names(text: str) -> bool:
    """Whether ``text`` is long enough to name something."""
    squeezed = _squeeze(text)
    return len(squeezed) >= (_NAME_CHARS - 2 if _CJK_RE.search(squeezed) else _NAME_CHARS)


def _clean(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text if 0 < len(text) <= limit else ""


class _Report:
    """The report as a reader sees it, in units a pair of strings must share."""

    def __init__(self, report: str) -> None:
        self._units = [_squeeze(unit) for unit in plain_units(parse_blocks(report))]

    def holds(self, *texts: str) -> bool:
        """Whether one unit of the report holds every one of ``texts``."""
        wanted = [_squeeze(text) for text in texts]
        return all(wanted) and any(all(text in unit for text in wanted) for unit in self._units)


def _entries(data: dict, key: str) -> list:
    value = data.get(key)
    return value if isinstance(value, list) else []


def check_highlights(data: dict, report: str) -> tuple[Highlights, dict[str, Any]]:
    """The highlights of ``data`` that the report holds, and how many of each kind were offered and kept."""
    held = _Report(report)
    figures: list[Figure] = []
    for entry in _entries(data, "figures"):
        if not isinstance(entry, dict):
            continue
        value, label = _clean(entry.get("value"), _VALUE_CHARS), _clean(entry.get("label"), _LABEL_CHARS)
        # One figure to a value and to a label: two tiles under one caption read as one number twice.
        taken = any(
            _squeeze(value) == _squeeze(kept.value) or _squeeze(label) == _squeeze(kept.label) for kept in figures
        )
        named = _names(label) and _squeeze(label) != _squeeze(value)
        if value and named and not taken and _DIGIT_RE.search(value) and held.holds(value, label):
            figures.append(Figure(value, label))
    findings: list[str] = []
    for entry in _entries(data, "findings"):
        finding = _clean(entry, _FINDING_CHARS[1])
        taken = any(_squeeze(finding) == _squeeze(kept) for kept in findings)
        if len(finding) >= _FINDING_CHARS[0] and not taken and held.holds(finding):
            findings.append(finding)
    timeline: list[Moment] = []
    for entry in _entries(data, "timeline"):
        if not isinstance(entry, dict):
            continue
        when, what = _clean(entry.get("when"), _VALUE_CHARS), _clean(entry.get("what"), _EVENT_CHARS)
        taken = any(_squeeze(when) == _squeeze(kept.when) for kept in timeline)
        named = _names(what)
        if when and named and not taken and _DIGIT_RE.search(when) and held.holds(when, what):
            timeline.append(Moment(when, what))

    def band(entries: list, bounds: tuple[int, int]) -> tuple:
        fewest, most = bounds
        return tuple(entries[:most]) if len(entries) >= fewest else ()

    offered = [note for entry in _entries(data, "notes") if (note := _clean(entry, _FINDING_CHARS[1]))]
    notes = placed_notes(report, [note for note in offered if len(note) >= _FINDING_CHARS[0]], _NOTES[1])
    highlights = Highlights(
        band(figures, _FIGURES), band(findings, _FINDINGS), band(timeline, _TIMELINE), band(notes, _NOTES)
    )
    counters = {
        "figures_offered": len(_entries(data, "figures")),
        "figures_kept": len(highlights.figures),
        "findings_offered": len(_entries(data, "findings")),
        "findings_kept": len(highlights.findings),
        "timeline_offered": len(_entries(data, "timeline")),
        "timeline_kept": len(highlights.timeline),
        "notes_offered": len(_entries(data, "notes")),
        "notes_kept": len(highlights.notes),
    }
    return highlights, counters


async def select_highlights(
    provider: LLMProvider,
    report: str,
    *,
    model: str | None = None,
    max_tokens: int = 8192,
    reasoning_effort: str | None = "low",
    timeout_seconds: float = 60.0,
) -> tuple[Highlights, dict[str, Any]]:
    """Ask ``provider`` for the report's highlights; what the report holds of them, and the record.

    Never raises. A call that fails, runs out of time or returns no usable JSON gives no
    highlights and says why in the record: the page is the report either way.
    """
    if len(report) > _REPORT_CHARS:
        return Highlights(), {"selected": False, "reason": "report_too_long"}
    # An absent effort leaves the provider's default; an explicit None would suppress the parameter.
    effort = {"reasoning_effort": reasoning_effort} if reasoning_effort is not None else {}
    try:
        response = await asyncio.wait_for(
            provider.chat_with_retry(
                messages=[{"role": "system", "content": _SYSTEM}, {"role": "user", "content": report}],
                model=model,
                max_tokens=max_tokens,
                temperature=0.0,
                **effort,
            ),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "report-highlights: no answer within {:.0f}s; the page ships without highlights", timeout_seconds
        )
        return Highlights(), {"selected": False, "reason": "timeout"}
    except Exception as exc:  # noqa: BLE001 - the page is the report either way
        logger.warning("report-highlights: the call failed ({}); the page ships without highlights", type(exc).__name__)
        return Highlights(), {"selected": False, "reason": f"call_failed:{type(exc).__name__}"}
    finish = getattr(response, "finish_reason", "") or ""
    text = visible_answer(getattr(response, "content", None) or "")
    if finish in ("length", "error") or not text:
        return Highlights(), {"selected": False, "reason": f"no_answer:{finish or 'empty'}"}
    data = next(json_objects(text), None)
    if data is None:
        return Highlights(), {"selected": False, "reason": "no_json"}
    highlights, counters = check_highlights(data, report)
    return highlights, {"selected": bool(highlights), **counters}


__all__ = ["check_highlights", "select_highlights"]
