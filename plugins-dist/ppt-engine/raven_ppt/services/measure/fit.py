"""Whether the copy fits its box, decided before anything is rendered.

Ported from the previous engine's `text_fit`, which owned this and was left
behind. Without it the pipeline could only find out after a render: copy runs
past its box, LibreOffice paints it anyway, and the render checks report the
words that ended up on top of other words -- three runs' worth of collisions
whose actual cause was a box too small for what was put in it.

Measuring it here answers the cause. Greedy wrap at the real width, count the
lines, compare against how many lines the box's height holds. A box that cannot
hold its copy is a finding on the build, in the same round, with the numbers in
it: six lines at 16pt needs 1.51in and the box gives 0.90in.

Two rules carried over from the engine, both load-bearing:

* CJK breaks per character and Latin per word, with a hyphen or slash inside a
  word counting as a break opportunity -- without that last part a narrow column
  renders "Forward-Looking" as "Forward-Loo / king".
* Every line keeps `RENDER_DRIFT_HEADROOM` of the width in reserve, because the
  renderer's metrics are not the measurer's. The engine calibrated the gap
  against LibreOffice 7.4: 0.14% on the longest repro line, so 3% is ~20x the
  observed drift and costs one or two characters a line.
"""

from __future__ import annotations

from pathlib import Path

from raven_ppt.contracts.findings import Finding, Severity
from raven_ppt.services.measure.geometry import EMU_PER_INCH, iter_shapes, open_deck
from raven_ppt.services.measure.width import WidthMeasurer, is_cjk_char

# A line box is this much of the type size, and the first line needs its ascent
# and descent before any leading is added.
LINE_HEIGHT_FACTOR = 1.3
TEXT_ASCENT_FACTOR = 0.85
TEXT_DESCENT_FACTOR = 0.35
RENDER_DRIFT_HEADROOM = 1.03

# Break after one of these inside a word rather than mid-syllable.
_WORD_BREAK_AFTER = frozenset("-–—/")

# Under this many characters a box is a label, and `wrapped_labels` already has
# the label case with its own slack. This check is about copy.
MIN_COPY_CHARS = 24
# How much past its box the copy has to run before it is reported. One line of
# slack: a box sized to its text exactly is normal authoring, and the renderer's
# leading differs from the measurer's by less than a line.
OVERSET_SLACK_LINES = 1


class LineOverflowError(ValueError):
    """A single character is wider than the space, so no wrap can succeed."""


def wrap(
    text: str,
    width_px: float,
    font_px: int,
    *,
    bold: bool = False,
    measurer: WidthMeasurer,
) -> list[str]:
    """Greedy wrap, every line fitting `width_px` with the drift reserve held back."""
    room = width_px / RENDER_DRIFT_HEADROOM
    lines: list[str] = []
    for paragraph in text.split("\n"):
        stripped = paragraph.strip()
        if stripped:
            lines.extend(_wrap_paragraph(measurer, stripped, room, font_px, bold))
    return lines or [""]


def capacity_lines(height_px: float, font_px: int) -> int:
    """How many lines of `font_px` a box `height_px` tall holds."""
    first = (TEXT_ASCENT_FACTOR + TEXT_DESCENT_FACTOR) * font_px
    if height_px < first:
        return 0
    return 1 + int((height_px - first) // (LINE_HEIGHT_FACTOR * font_px))


def needed_height_px(line_count: int, font_px: int) -> float:
    """How tall a box must be to hold `line_count` lines of `font_px`."""
    if line_count <= 0:
        return 0.0
    first = (TEXT_ASCENT_FACTOR + TEXT_DESCENT_FACTOR) * font_px
    return first + (line_count - 1) * LINE_HEIGHT_FACTOR * font_px


def overset_copy(pptx_path: Path, measurer: WidthMeasurer | None = None) -> list[Finding]:
    """Boxes holding more copy than their height can show, measured not rendered."""
    if measurer is None:
        from raven_ppt.services.assets.text_metrics import measurer as font_measurer

        measurer = font_measurer()
    findings: list[Finding] = []
    for number, slide in enumerate(open_deck(pptx_path).slides, start=1):
        for shape in iter_shapes(slide.shapes):
            if not getattr(shape, "has_text_frame", False):
                continue
            finding = _overset(shape, number, measurer)
            if finding is not None:
                findings.append(finding)
    return findings


def _overset(shape, page: int, measurer: WidthMeasurer) -> Finding | None:
    frame = shape.text_frame
    text = frame.text.strip()
    if len(text) < MIN_COPY_CHARS or frame.word_wrap is False:
        return None
    size_pt = _size_pt(frame)
    if size_pt is None:
        return None
    width_in = (shape.width - _margins(frame, horizontal=True)) / EMU_PER_INCH
    height_in = (shape.height - _margins(frame, horizontal=False)) / EMU_PER_INCH
    if width_in <= 0 or height_in <= 0:
        return None
    font_px = int(round(size_pt * 96 / 72))
    try:
        lines = wrap(text, width_in * 96, font_px, bold=_bold(frame), measurer=measurer)
    except LineOverflowError:
        lines = [text]
    holds = capacity_lines(height_in * 96, font_px)
    if len(lines) <= holds + OVERSET_SLACK_LINES:
        return None
    needed_in = needed_height_px(len(lines), font_px) / 96
    return Finding(
        kind="overset_copy",
        severity=Severity.WARNING,
        page=page,
        message=(
            f"'{_head(text)}' wraps to {len(lines)} lines at {size_pt:g}pt in a {width_in:.2f}in column, "
            f"which needs {needed_in:.2f}in of height, and the box gives {height_in:.2f}in. Give it the room "
            f"or give it less to say -- do not answer this by dropping the type size"
        ),
        detail={
            "lines": len(lines),
            "holds": holds,
            "needs_in": round(needed_in, 2),
            "box_in": round(height_in, 2),
            "size_pt": size_pt,
        },
    )


def _wrap_paragraph(measurer: WidthMeasurer, text: str, room: float, font_px: int, bold: bool) -> list[str]:
    lines: list[str] = []
    current = ""
    for token in _tokenize(text):
        if token == " ":  # noqa: S105 -- a text token, not a credential
            if current:
                current += " "
            continue
        candidate = current + token
        if measurer.width(candidate.rstrip(), font_px, bold) <= room:
            current = candidate
            continue
        if current.strip():
            lines.append(current.rstrip())
            current = ""
            if measurer.width(token, font_px, bold) <= room:
                current = token
                continue
        rest = token
        while rest:
            prefix = _longest_fitting(measurer, rest, room, font_px, bold)
            if prefix == 0:
                raise LineOverflowError(f"{rest[0]!r} at {font_px}px is wider than {room:.0f}px")
            if prefix == len(rest):
                current, rest = rest, ""
            else:
                lines.append(rest[:prefix])
                rest = rest[prefix:]
    if current.strip():
        lines.append(current.rstrip())
    return lines


def _tokenize(text: str) -> list[str]:
    """Latin words, single CJK characters, explicit spaces."""
    tokens: list[str] = []
    word: list[str] = []
    for char in text:
        if char == " ":
            if word:
                tokens.append("".join(word))
                word = []
            tokens.append(" ")
        elif is_cjk_char(char):
            if word:
                tokens.append("".join(word))
                word = []
            tokens.append(char)
        elif char in _WORD_BREAK_AFTER and word:
            word.append(char)
            tokens.append("".join(word))
            word = []
        else:
            word.append(char)
    if word:
        tokens.append("".join(word))
    return tokens


def _longest_fitting(measurer: WidthMeasurer, text: str, room: float, font_px: int, bold: bool) -> int:
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if measurer.width(text[:mid], font_px, bold) <= room:
            low = mid
        else:
            high = mid - 1
    return low


def _size_pt(frame) -> float | None:
    """The largest size any run in the frame states, or None when none does."""
    sizes = [
        run.font.size.pt
        for para in frame.paragraphs
        for run in para.runs
        if run.font.size is not None and run.text.strip()
    ]
    return max(sizes) if sizes else None


def _bold(frame) -> bool:
    return any(run.font.bold for para in frame.paragraphs for run in para.runs)


def _margins(frame, *, horizontal: bool) -> int:
    if horizontal:
        return (frame.margin_left or 0) + (frame.margin_right or 0)
    return (frame.margin_top or 0) + (frame.margin_bottom or 0)


def _head(text: str, limit: int = 34) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
