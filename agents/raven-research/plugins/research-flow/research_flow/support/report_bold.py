"""A report's bold: how its body paragraphs hold it, and bold chosen anew when they do not.

The report rule asks for bold a reader can skim: the names the text is about, its
deciding figures, at most one short verdict, never a sentence. A model writing the
report follows that one run and not the next - bolding nothing, or every number in a
paragraph, or a whole clause - and a request to rewrite the report for its bold swings
it from one to the other while it rewrites the text too. So the bold is chosen apart
from the text: a side call reads the body's paragraphs, numbered, and names at most a
few spans in each, and the spans are set in code, which holds the cap and the length
and leaves every other character as it was.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from loguru import logger

from research_flow.support._verdict import json_objects
from research_flow.support.answer_text import visible_answer
from research_flow.support.report_html import names_sources

if TYPE_CHECKING:
    from raven.contracts.llm_provider import LLMProvider

# A report with fewer paragraphs than this is too short for a share to mean anything.
MIN_PARAGRAPHS = 4
# A paragraph or list item shorter than this, links and URLs aside, is not counted.
_MIN_CHARS = 60
# The longest bold that still reads as a name, a figure or a few words: a Latin name
# takes more characters than a CJK one.
_MAX_BOLD = 24
_MAX_BOLD_LATIN = 40
# More fitting bold spans than this in one paragraph is past the rule's four or five.
_DENSE = 6
# What the chosen bold may set in one paragraph.
_PICKS = 3
# A body longer than this is not sent; the report keeps the bold it has.
_MAX_BODY_CHARS = 60000
_SENTENCE_END = tuple(".!?;:\u3002\uff01\uff1f\uff1b\uff1a")
_HEADING_RE = re.compile(r"^##(?!#)([^\n]*)$", re.M)
_BLOCK_RE = re.compile(r"(?:^[^\n]*\S[^\n]*(?:\n|\Z))+", re.M)
_FENCE_RE = re.compile(r"^```.*?^```[^\n]*$", re.M | re.S)
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_URL_RE = re.compile(r"https?://\S+")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
# A paper's arXiv number names where it is, not what it found.
_ARXIV_ID_RE = re.compile(r"^\d{4}\.\d{4,5}(?:v\d+)?$")
# Where a span may not be set: a link's target, a bare URL, inline code.
_UNTOUCHABLE_RE = re.compile(r"\]\([^)]*\)|https?://\S+|`[^`\n]*`")

_SYSTEM = """You choose the bold of a research report for a reader who skims.

You get the report's body paragraphs, numbered. For each, name at most three spans to set in bold, each copied exactly from that paragraph:
- the name the paragraph is about - a method, model, work, product, benchmark or organization - where it first appears;
- the figure its point rests on - a score, a gain, a cost, a count, a date;
- at most one verdict of a few words.
A figure alone, not the comparison around it: `2.40x`, not `from 64.0% to 70.4%`. A name alone, not the clause that explains it. Never a sentence, never a clause. Choose so that the bold alone gives the report's findings: the deciding figure, not every number; a paragraph with nothing a reader scans for gets none.

Answer with one JSON object mapping each paragraph's number to its list of spans, and nothing else:
{"1": ["DARE", "90%"], "2": [], "3": ["TIES-Merging"]}"""


@dataclass(frozen=True)
class BoldCheck:
    """How a report's counted body paragraphs hold their bold."""

    held: int
    total: int
    dense: int
    long: int

    def fails(self, floor: float) -> bool:
        """Short of the floor, or with a paragraph bolded past the cap, or a bold that is a clause."""
        if self.total < MIN_PARAGRAPHS:
            return False
        return self.held < floor * self.total or self.dense > 0 or self.long > 0


def check_bold(report: str) -> BoldCheck:
    held = dense = long = 0
    spans = body_spans(report)
    for start, end in spans:
        bolds = _BOLD_RE.findall(report[start:end])
        fitting = sum(map(fits, bolds))
        held += fitting > 0
        dense += fitting > _DENSE
        long += len(bolds) - fitting
    return BoldCheck(held, len(spans), dense, long)


def fits(span: str) -> bool:
    """Whether a bold span reads as a name, a figure or a few words, not a clause."""
    words = _LINK_RE.sub(r"\1", span).strip()
    cap = _MAX_BOLD_LATIN if words.isascii() else _MAX_BOLD
    return 0 < len(words) <= cap and not words.endswith(_SENTENCE_END)


def body_spans(report: str) -> list[tuple[int, int]]:
    """Where the body's paragraphs and list items are, as offsets into ``report``.

    The body is the report under its `##` headings, less the list of sources that closes
    it. Tables, quotes, headings and code are not counted, nor a paragraph under
    ``_MIN_CHARS`` once links and URLs are taken out.
    """
    fences = [m.span() for m in _FENCE_RE.finditer(report)]

    def fenced(start: int, end: int) -> bool:
        return any(a < end and start < b for a, b in fences)

    heads = [m for m in _HEADING_RE.finditer(report) if not fenced(m.start(), m.end())]
    sections = [
        (h.group(1), h.end(), heads[i + 1].start() if i + 1 < len(heads) else len(report)) for i, h in enumerate(heads)
    ]
    # The list of sources is the last section, as ``fixed_closing`` reads it: a body
    # section on data sources ("data and sources") is still body.
    if sections and names_sources(sections[-1][0].strip().strip("#").strip()):
        sections.pop()
    spans: list[tuple[int, int]] = []
    for _, start, end in sections:
        for block in _BLOCK_RE.finditer(report, start, end):
            if fenced(block.start(), block.end()):
                continue
            for a, b in _units(report, block.start(), block.end()):
                text = report[a:b]
                if len(_URL_RE.sub("", _LINK_RE.sub(r"\1", text)).replace("**", "").strip()) >= _MIN_CHARS:
                    spans.append((a, b))
    return spans


def _units(report: str, start: int, end: int) -> list[tuple[int, int]]:
    """A prose block as one unit, a list as one per item; tables, quotes and headings as none."""
    text = report[start:end]
    lead = len(text) - len(text.lstrip())
    body = text.strip()
    if not body or body.startswith(("#", "|", ">")):
        return []
    if not _ITEM_RE.match(body):
        return [(start + lead, start + lead + len(body))]
    items: list[list[int]] = []
    offset = start
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        if stripped.strip() and (_ITEM_RE.match(stripped) or not items):
            items.append([offset, offset + len(stripped)])
        elif stripped.strip():
            items[-1][1] = offset + len(stripped)
        offset += len(line)
    return [(a, b) for a, b in items]


def unbold(text: str) -> str:
    return _BOLD_RE.sub(r"\1", text)


def apply_bold(report: str, picks: dict[int, list[str]]) -> tuple[str, dict[str, int]]:
    """``report`` with its body's bold replaced by ``picks``, paragraph number to spans.

    A span is set where it first appears in its paragraph, outside a link's target, a URL
    or code, and apart from the spans already set; one that does not fit, is not found or
    is past the paragraph's ``_PICKS`` is dropped. Every other character stays as it was.
    """
    out: list[str] = []
    last = set_count = dropped = 0
    for number, (start, end) in enumerate(body_spans(report), 1):
        plain = unbold(report[start:end])
        marked, kept, lost = _set(plain, picks.get(number) or [])
        out += [report[last:start], marked]
        last, set_count, dropped = end, set_count + kept, dropped + lost
    out.append(report[last:])
    return "".join(out), {"set": set_count, "dropped": dropped}


def _set(plain: str, spans: list[str]) -> tuple[str, int, int]:
    blocked = [m.span() for m in _UNTOUCHABLE_RE.finditer(plain)]
    taken: list[tuple[int, int]] = []
    lost = 0
    for raw in spans:
        span = unbold(str(raw)).strip()
        if len(taken) >= _PICKS or not span or not fits(span) or "[" in span or "]" in span or _ARXIV_ID_RE.match(span):
            lost += 1
            continue
        at = _find(plain, span, blocked + taken)
        if at is None:
            lost += 1
            continue
        taken.append((at, at + len(span)))
    out, last = [], 0
    for a, b in sorted(taken):
        out += [plain[last:a], "**", plain[a:b], "**"]
        last = b
    out.append(plain[last:])
    return "".join(out), len(taken), lost


def _find(plain: str, span: str, avoid: list[tuple[int, int]]) -> int | None:
    at = plain.find(span)
    while at >= 0:
        if not any(a < at + len(span) and at < b for a, b in avoid):
            return at
        at = plain.find(span, at + 1)
    return None


def _picks(data: dict[str, Any]) -> dict[int, list[str]]:
    picks: dict[int, list[str]] = {}
    for key, value in data.items():
        try:
            number = int(str(key).strip())
        except ValueError:
            continue
        if isinstance(value, list):
            picks[number] = [v for v in value if isinstance(v, str)]
    return picks


async def choose_bold(
    provider: LLMProvider,
    report: str,
    *,
    model: str | None = None,
    max_tokens: int = 16384,
    reasoning_effort: str | None = "low",
    timeout_seconds: float = 90.0,
) -> tuple[str | None, dict[str, Any]]:
    """``report`` with its bold chosen by ``provider``, or None; and the record.

    ``max_tokens`` covers the reasoning too: a flash model spent from 3.7k to past 8k
    reasoning tokens on the same 23 paragraphs before a JSON answer of a few hundred.

    Never raises. A call that fails, runs out of time or returns no usable JSON gives
    None and says why: the report then ships with the bold it was written with.
    """
    paragraphs = [unbold(report[a:b]) for a, b in body_spans(report)]
    listing = "\n\n".join(f"[{n}] {text}" for n, text in enumerate(paragraphs, 1))
    if not paragraphs or len(listing) > _MAX_BODY_CHARS:
        return None, {"chosen": False, "reason": "body_too_long" if paragraphs else "no_body"}
    effort = {"reasoning_effort": reasoning_effort} if reasoning_effort is not None else {}
    try:
        response = await asyncio.wait_for(
            provider.chat_with_retry(
                messages=[{"role": "system", "content": _SYSTEM}, {"role": "user", "content": listing}],
                model=model,
                max_tokens=max_tokens,
                temperature=0.0,
                **effort,
            ),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        logger.warning("report-bold: no answer within {:.0f}s; the report keeps its bold", timeout_seconds)
        return None, {"chosen": False, "reason": "timeout"}
    except Exception as exc:  # noqa: BLE001 - the report ships either way
        logger.warning("report-bold: the call failed ({}); the report keeps its bold", type(exc).__name__)
        return None, {"chosen": False, "reason": f"call_failed:{type(exc).__name__}"}
    finish = getattr(response, "finish_reason", "") or ""
    text = visible_answer(getattr(response, "content", None) or "")
    if finish in ("length", "error") or not text:
        return None, {"chosen": False, "reason": f"no_answer:{finish or 'empty'}"}
    data = next(json_objects(text), None)
    picks = _picks(data) if isinstance(data, dict) else {}
    if not picks:
        return None, {"chosen": False, "reason": "no_json"}
    bolded, counters = apply_bold(report, picks)
    if not counters["set"]:
        return None, {"chosen": False, "reason": "nothing_set", **counters}
    return bolded, {"chosen": True, **counters}


__all__ = ["MIN_PARAGRAPHS", "BoldCheck", "apply_bold", "body_spans", "check_bold", "choose_bold", "fits", "unbold"]
