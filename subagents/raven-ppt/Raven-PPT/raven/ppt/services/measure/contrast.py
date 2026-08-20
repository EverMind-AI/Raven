"""Whether the words can be read off the ground they were rendered onto.

Every other check here asks about geometry -- is the box big enough, do two boxes
overlap, is the type above the floor. A live deck cleared all of them and still had
pages a reader could not use: its page 9 set the title in #1A1A1A on #000000, and four
of its labels in #1A1A1A on #1A1A1A, the same colour exactly. Page 12's title was
#1A1A1A on black too. Nothing measured it.

The cause is one confusion, and naming it is most of the fix. A deck's theme carries
`background`, `surface` and `foreground`; on a dark template those are black, near-black
and white. An author reaching for "the dark one" for its type picks `surface` -- which is
what the ground is painted with -- and the page comes out with black text on black.

Measured against the render, because that is the only place the ground has an answer:
what sits behind a text box is a layout's artwork, a photograph, a panel three shapes
down, or the master's gradient, and resolving that stack by hand reproduces the renderer.
The declared ink is read off the file, where it is stated exactly; the ground is the
modal pixel under the box, where the renderer has already resolved everything.

An earlier version segmented the crop into ink and ground by quantile and reported the
ratio between them. It flagged every page, and what it flagged were the commas: a `·` is
a few pixels wide, its crop is nearly all ground, and both quantiles land on the same
colour -- 1.0:1 meaning "no glyph here", not "unreadable".
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from raven.ppt.contracts.findings import Audience, Finding, Severity
from raven.ppt.services.measure.geometry import EMU_PER_INCH, iter_shapes, open_deck, page_box

# WCAG AA asks 4.5:1 for body copy and 3:1 for large text, and 3:1 applied to everything
# is where this started. It refused a deck for its own template's design: the agenda page
# every deck is required to clone sets white numerals on the template's orange circles,
# which measures 2.5:1 and is perfectly readable -- a live run rebuilt that page five
# times and could not publish, because the only fix was to break the template.
#
# So the two answers are separated, and the numbers come from the two real cases:
#
#   #1A1A1A on #000000  ->  1.1:1   invisible; the case this check was built for
#   #FFFFFF on #FF8400  ->  2.5:1   the template's own agenda numerals, shipped, legible
#
# Under 2:1 nothing is legible and the deck is refused. Between 2 and 3 it is thin, which
# is worth saying on a page this deck drew and is not worth refusing a deck over.
MIN_RATIO = 3.0
UNREADABLE_RATIO = 2.0
# Below this the crop cannot say what its ground is.
_MIN_PIXELS = 24
_HEAD = 30


def contrast_findings(
    pptx_path: Path, pdf_path: Path | None, dpi: int = 72, pages: list[Path] | None = None
) -> list[Finding]:
    """Text whose declared colour is too close to the ground it landed on.

    `pages` are rendered pages when the caller already has them, which is also how a
    test hands over a page without a renderer on the machine.
    """
    if pages is None and (pdf_path is None or not Path(pdf_path).is_file()):
        return []
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover -- Pillow ships with the route
        return []
    from raven.ppt.services.render import pdf as pdf_render  # noqa: F401 -- used when pages is None

    if pages is None:
        try:
            pages = pdf_render.to_pngs(Path(pdf_path), Path(pdf_path).parent, dpi=dpi)
        except Exception:  # noqa: BLE001 -- no render, no measurement
            return []
    presentation = open_deck(pptx_path)
    canvas_w = (presentation.slide_width or 1) / EMU_PER_INCH
    canvas_h = (presentation.slide_height or 1) / EMU_PER_INCH

    findings: list[Finding] = []
    for number, slide in enumerate(presentation.slides, start=1):
        png = next((p for p in pages if f"{number:03d}" in p.name), None)
        if png is None:
            continue
        with Image.open(png) as opened:
            image = opened.convert("RGB")
            worst: tuple[float, str, str, tuple[int, int, int]] | None = None
            count = 0
            for shape in iter_shapes(slide.shapes):
                measured = _measure(shape, image, canvas_w, canvas_h)
                if measured is None:
                    continue
                ratio, text, ink, ground = measured
                if ratio >= MIN_RATIO:
                    continue
                count += 1
                if worst is None or ratio < worst[0]:
                    worst = (ratio, text, ink, ground)
        if worst is None:
            continue
        ratio, text, ink, ground = worst
        others = f" and {count - 1} more block(s) on the page" if count > 1 else ""
        invisible = ratio < UNREADABLE_RATIO
        findings.append(
            Finding(
                kind="unreadable" if invisible else "thin_contrast",
                severity=Severity.BLOCKING if invisible else Severity.WARNING,
                page=number,
                audience=Audience.AUTHOR,
                message=(
                    f"'{text[:_HEAD]}' is set in #{ink} on a ground that renders "
                    f"#{'%02X%02X%02X' % ground}{others} -- {ratio:.1f}:1, "
                    + (
                        f"under the {UNREADABLE_RATIO:g}:1 at which the characters stop being there at all. On a "
                        f"dark deck the type colour is the theme's `foreground`; `surface` and `background` are "
                        f"what the ground is painted with, and reaching for one of those gives you black on black"
                        if invisible
                        else f"under the {MIN_RATIO:g}:1 WCAG asks for large type. Legible, thin: if this page is "
                        f"yours to draw, take the ink up or the ground down. If it came from the template's own "
                        f"page, it is the template's choice and this is a note rather than a fault"
                    )
                ),
                detail={"ratio": round(ratio, 2), "blocks": count, "ink": ink, "text": text[:60]},
            )
        )
    return findings


def _measure(shape: Any, image: Any, canvas_w: float, canvas_h: float):
    """(ratio, text, ink hex, ground rgb) for one text block, or None when it cannot say."""
    if not getattr(shape, "has_text_frame", False):
        return None
    text = " ".join(shape.text_frame.text.split())
    if not text:
        return None
    ink = _declared(shape.text_frame)
    if ink is None:
        return None  # inherited from the theme or the layout: not stated here, not judged here
    # Through the groups above it, or the crop lands somewhere else on the page and
    # the ground this reads is not the ground the words sit on.
    where = page_box(shape)
    if where is None:
        return None
    left = int(where.x0 / canvas_w * image.width)
    top = int(where.y0 / canvas_h * image.height)
    right = int(where.x1 / canvas_w * image.width)
    bottom = int(where.y1 / canvas_h * image.height)
    crop = image.crop((max(left, 0), max(top, 0), min(right, image.width), min(bottom, image.height)))
    if crop.width < 2 or crop.height < 2:
        return None
    pixels = list(crop.getdata())
    if len(pixels) < _MIN_PIXELS:
        return None
    ground = Counter(pixels).most_common(1)[0][0]
    return _ratio(_rgb(ink), ground), text, ink, ground


def _declared(frame: Any) -> str | None:
    """The colour the file states for this text, or None when it inherits one.

    The largest run wins where a block mixes them: a heading with one accented word is
    judged on the heading.
    """
    weighed: Counter = Counter()
    for para in frame.paragraphs:
        for run in para.runs:
            if not run.text.strip():
                continue
            colour = run.font.color
            try:
                if colour is None or colour.type is None or colour.rgb is None:
                    continue
                weighed[str(colour.rgb).upper()] += len(run.text)
            except (AttributeError, TypeError, ValueError):
                continue
    if not weighed:
        return None
    return weighed.most_common(1)[0][0]


def _rgb(value: str) -> tuple[int, int, int]:
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def _ratio(ink: tuple[int, int, int], ground: tuple[int, int, int]) -> float:
    first, second = _luminance(ink), _luminance(ground)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def _luminance(colour: tuple[int, int, int]) -> float:
    """WCAG relative luminance."""
    channels = []
    for value in colour:
        share = value / 255
        channels.append(share / 12.92 if share <= 0.04045 else ((share + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]
