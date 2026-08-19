"""Terminal-answer shaping: give the committed answer a machine-readable form.

Why this exists. ``final_answer`` in the evaluation record is the last non-empty
assistant ``content``, verbatim. Measured on a 302-question live-web batch: the
anchor's median is 1,759 characters with a p90 of 41,683, and only 15.6% of its
answers carry any answer marker at all (28.4% on the treated arm). A sampled
treated answer opens with "This is very helpful. According to the Nagada
website: ..." - mid-reasoning self-talk filed as a final answer. The turn's
*ending* is currently a passive event (something truncated it), not an action
the model took, so a report axis run on this shape would measure truncation
rate rather than report quality.

What this module does, and what it deliberately does NOT do. It is a **pure,
deterministic, additive** transform:

* It never invents content. If the visible answer is empty, it stays empty.
* It never shortens. The shaped text is the visible answer plus, at most, one
  appended canonical ``Answer: <span>`` line. ``_content_len`` is asserted
  non-decreasing and the transform falls back to passthrough if that ever
  fails.
* It never replaces the record. The caller keeps the raw text and records this
  result beside it.

That triple is the whole design, and it is a direct response to a measured
failure class rather than a style preference. An upstream framework we
benchmarked runs an answer-extraction stage that is a zero-gain lossy channel:
gold present in its final summary on 71/120 questions, gold surviving into its
boxed field on 58/120 - it produced nothing that was not already there and
dropped 10.83pp on the way. Our own dr@1.6 salvage seam failed the same way.
The rule that came out of both: **answer shaping may improve a record, never
blank one.** A transform that cannot invent an answer also cannot lose one;
here those are the same property, not two.

Score budget is 0 by construction, not by promise. Nothing here reaches the
model or the persisted message sequence - the caller records the result in the
read-only observer payload - so the generated distribution is byte-identical
with the flag on or off. "Rescuing" answerless turns after the fact was priced
separately and only 3-23% of rescues judge correct; the headroom is in
prevention (dr@1.4's +8.50pp), not in shaping. What shaping buys is an
*observable*: a marker rate, and a guaranteed terminal answer line on the turns
where the model cooperated, which is what a report axis needs before it can
measure anything at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from raven.agent.flow.answer_text import visible_answer

# Marker forms, most explicit first. The first hit wins: a turn carrying both an
# <answer> tag and a stray "Answer:" line in its prose meant the tag.
_ANSWER_TAG_RE = re.compile(r"<answer>(.*?)</answer>", re.S | re.I)
_BOXED_RE = re.compile(r"\\boxed\s*\{(.*?)\}", re.S)
# Anchored to a line start so "the answer: it depends" mid-sentence does not
# match. ``\*\*`` covers the markdown form the model emits most often. The span
# ends at a blank line or end of text - not at the next newline, because
# multi-line answers (a list, a date range) are common and cutting at the first
# newline would be exactly the truncation this module exists to prevent.
_LABEL_RE = re.compile(
    r"(?:\A|\n)[ \t>*_]*(?:\*\*)?\s*"
    r"(?:final\s+answer|answer|最终答案|答案)"
    r"\s*(?:\*\*)?\s*[:：]\s*(.+?)(?=\n[ \t]*\n|\Z)",
    re.S | re.I,
)

_MARKERS = (("answer_tag", _ANSWER_TAG_RE), ("boxed", _BOXED_RE), ("labeled", _LABEL_RE))

#: Canonical terminal line the shaped text is guaranteed to end with whenever a
#: marker was found. Downstream scoring parses this instead of asking a judge to
#: read a wall of prose.
ANSWER_LINE_PREFIX = "Answer: "

_WS_RE = re.compile(r"\s+")


def _content_len(text: str) -> int:
    """Length ignoring whitespace, so re-indentation is not read as loss."""
    return len(_WS_RE.sub("", text or ""))


@dataclass(frozen=True)
class ShapedAnswer:
    """Result of one shaping attempt.

    ``text`` is always safe to record: it is the visible answer unchanged, or
    the visible answer plus an appended canonical answer line. It is never
    shorter in content than the visible answer, and never non-empty when the
    visible answer was empty.
    """

    text: str
    """Shaped text. Equal to ``visible`` when nothing could be improved."""

    visible: str
    """The input this was computed from (raw content with reasoning folded)."""

    form: str
    """One of: ``answer_tag`` / ``boxed`` / ``labeled`` (a marker was found and
    a canonical line is present), ``unmarked`` (no marker; passthrough),
    ``empty`` (no visible answer at all; nothing to shape)."""

    span: str | None
    """The marked answer span, when a marker was found."""

    shaped: bool
    """True only when ``text`` differs from ``visible``."""

    reason: str
    """Why this outcome. ``refused_*`` values mean a guard fired and the result
    was forced back to passthrough - those are the values worth alerting on."""

    @property
    def marked(self) -> bool:
        """Whether the model emitted an explicit answer marker."""
        return self.form in ("answer_tag", "boxed", "labeled")

    def counters(self) -> dict[str, object]:
        """Observer payload. Read-only; recorded beside the raw answer."""
        return {
            "form": self.form,
            "marked": self.marked,
            "shaped": self.shaped,
            "reason": self.reason,
            "visible_chars": len(self.visible),
            "shaped_chars": len(self.text),
            "span_chars": len(self.span) if self.span else 0,
        }


def _find_span(visible: str) -> tuple[str, str] | None:
    for form, rx in _MARKERS:
        hits = rx.findall(visible)
        if not hits:
            continue
        # Last hit, not first: a model that restates its answer at the end has
        # its final word there, and a template that echoes the instruction
        # ("put the answer in \boxed{}") would otherwise win over the answer.
        span = (hits[-1] or "").strip()
        if span:
            return form, span
        # A marker with nothing in it is the exact upstream failure this module
        # is built against: extraction produced an empty answer from a turn
        # that had one. Do not fall through to a weaker marker on the strength
        # of an empty strong one - report the refusal.
        return "refused_empty_marker:" + form, ""
    return None


def shape_final_answer(
    raw: str | None,
    *,
    closing_tag_required: bool = False,
) -> ShapedAnswer:
    """Shape one terminal answer. Pure; no I/O, no model call, no mutation.

    ``closing_tag_required`` is forwarded to :func:`visible_answer` so the hop
    reads the same visible answer its arm is judged on. Passing the arm's own
    value matters: the anchor runs with it False and the treated arm with it
    True, and a hop that hardcoded either would report one arm's marker rate
    against the other arm's definition of "visible".
    """
    visible = visible_answer(raw, closing_tag_required=closing_tag_required)
    if not visible:
        return ShapedAnswer(
            text="", visible="", form="empty", span=None, shaped=False,
            reason="no_visible_answer",
        )

    found = _find_span(visible)
    if found is None:
        return ShapedAnswer(
            text=visible, visible=visible, form="unmarked", span=None, shaped=False,
            reason="no_marker",
        )
    form, span = found
    if form.startswith("refused_"):
        return ShapedAnswer(
            text=visible, visible=visible, form="unmarked", span=None, shaped=False,
            reason=form,
        )

    # Additive only. If the visible text already ends with the canonical line we
    # are done; otherwise append it. Nothing is removed in either branch, which
    # is what makes the guard below unreachable rather than merely unlikely.
    tail = ANSWER_LINE_PREFIX + span
    if visible.rstrip().endswith(tail):
        shaped_text, did = visible, False
    else:
        shaped_text, did = visible.rstrip() + "\n\n" + tail, True

    # The guard. Unreachable by construction above - which is precisely why it
    # stays: it is the assertion that keeps a future edit to the branch above
    # from silently turning this module into the lossy channel it replaces. A
    # ``refused_shorter`` in any back-feed report means that edit happened.
    if _content_len(shaped_text) < _content_len(visible):
        return ShapedAnswer(
            text=visible, visible=visible, form="unmarked", span=span, shaped=False,
            reason="refused_shorter",
        )

    return ShapedAnswer(
        text=shaped_text, visible=visible, form=form, span=span, shaped=did,
        reason="shaped" if did else "already_canonical",
    )


__all__ = ["ANSWER_LINE_PREFIX", "ShapedAnswer", "shape_final_answer"]
