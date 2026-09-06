"""Reading a template's horizontal grid off its own example pages.

`house_style` already measures where a template puts its title row and how wide
its type ladder is, and it measures those to hand an author numbers to build with.
The same reading answers a second question the author never asks and every check
does: which strip of the page is the title's, which is the footer's, and which is
left for content. Stating it once, in the template's inches, is what lets a check
say "this card climbed into the title row" without each check inventing an idea
of where the title row is.

Two edges have to be measured and both are measured the same way, by agreement
across the template's own content pages. The title band ends at the bottom of the
title row the pages agree on -- `house_style` votes on that box already, so this
reads its answer rather than repeating the vote. The footer band begins at the top
of the furniture the pages carry along their bottom edge: the footer, date and
slide-number placeholders a layout reserves, and any small mark or hairline the
pages themselves repeat down there.

The bar for "agree" is a majority of the content pages, not two of them. Measured
over the twelve templates that ship here, the real signal is on every content page
of every one -- 9 of 9 up to 20 of 20 for the footer, 7 of 9 up to 17 of 20 for the
title row -- while the near misses (a rule on one page, a caption on another) are
each on exactly one page. A two-page bar would let two pages out of nineteen name
the deck's grid.

A template whose pages do not agree gets None, and a None here means the checks
built on it return nothing. That is the point: there is no default grid to fall
back on, and a coordinate system nobody measured would be answering questions it
cannot answer.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from raven_ppt.contracts.masters import Bands
from raven_ppt.services.measure.geometry import iter_shapes, page_box
from raven_ppt.services.measure.rendered import RULE_MAX_HEIGHT_PT
from raven_ppt.services.template.house import house_style
from raven_ppt.services.template.inventory import template_dir
from raven_ppt.services.template.menu import menu

BANDS_FILE = "bands.json"

# The placeholder roles a layout reserves the bottom strip with. Empty on every
# template checked, and the reservation is the reading: PowerPoint keeps that
# strip for the page number on every page built on the layout, so copy written
# into it is copy written into the template's own furniture slot.
_FOOTER_ROLES = ("FOOTER", "DATE", "SLIDE_NUMBER")

# Where the search for footer furniture stops. The twelve shipped templates all
# reserve their bottom strip at 7.01in of a 7.5in canvas -- the bottom 6.5% --
# and inside the bottom 15% nothing else on any of them is repeated on more than
# one page. Taking the bottom half instead pulled body copy and card edges in:
# 3.7in to 5.7in, on the middle of the page, on every one of the twelve.
_FOOTER_REGION = 0.85

# Under this many characters a text shape down there is a page number or a
# wordmark rather than copy that ran to the bottom of the page.
_MARK_CHARS = 12

# A hairline, borrowed from the rendered-rule reading so the two agree on what a
# rule is rather than each carrying a number.
_RULE_HEIGHT_IN = RULE_MAX_HEIGHT_PT / 72

# Two pages agreeing is the floor, the same one `house_style` uses; a majority is
# the bar. They differ only for a template with three or four content pages.
_AGREEMENT = 2

# What has to be left between the two edges for them to describe a page. The
# shipped templates leave 78% of the canvas, so this refuses a title row that
# swallowed the page rather than trimming a real one.
_MIN_BODY = 0.5


def bands_of(template: Path, pdf: Path | None = None) -> Bands | None:
    """The grid this template's content pages agree on, or None if they do not.

    `pdf` is a render of the same file, passed through to `house_style`: on a
    template whose pages carry no title placeholder the title row is picked by
    which copy is set largest, and the file's declared sizes are not what the
    reader saw.
    """
    try:
        from pptx import Presentation
    except ImportError:  # pragma: no cover -- python-pptx ships with the extra
        return None
    try:
        presentation = Presentation(str(template))
    except Exception:  # noqa: BLE001 -- a malformed file is simply not a template
        return None
    slides = list(presentation.slides)
    listed = menu(template)
    content = [entry.number for entry in listed if not entry.role and entry.number <= len(slides)]
    if len(content) < _AGREEMENT:
        return None
    house = house_style(template, listed, pdf)
    if house is None or house.title is None:
        return None
    agreed = _bar(len(content))
    if house.title.pages < agreed:
        return None
    canvas_w, canvas_h = round(house.canvas[0], 2), round(house.canvas[1], 2)
    title_bottom = round(house.title.box[1] + house.title.box[3], 2)
    # No footer furniture is a measurement too: nothing is reserved down there, so
    # the content band runs to the bottom edge and the footer band is empty.
    footer_top = _footer_top(slides, content, canvas_h, agreed)
    body_bottom = canvas_h if footer_top is None else footer_top
    if not 0 < title_bottom < body_bottom <= canvas_h:
        return None
    if body_bottom - title_bottom < canvas_h * _MIN_BODY:
        return None
    return Bands(
        title_top=0.0,
        title_bottom=title_bottom,
        body_bottom=body_bottom,
        footer_bottom=canvas_h,
        canvas_w=canvas_w,
        canvas_h=canvas_h,
    )


def bands_path(project) -> Path:
    return template_dir(project) / BANDS_FILE


def write_bands(project, bands: Bands) -> Path:
    """Keep the grid beside the template, so later builds do not re-measure it."""
    path = bands_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bands.to_json(), indent=1), encoding="utf-8")
    return path


def read_bands(project) -> Bands | None:
    """The grid measured for this deck, or None before anything measured one.

    A file that cannot be read is a deck without a grid rather than a deck that
    fails to build -- the checks that wanted it return nothing and the rest of the
    build is unaffected.
    """
    path = bands_path(project)
    if not path.is_file():
        return None
    try:
        return Bands.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _bar(pages: int) -> int:
    return max(_AGREEMENT, pages // 2 + 1)


def _footer_top(slides: list, content: list[int], canvas_h: float, agreed: int) -> float | None:
    """The top of the bottom strip the content pages agree on, or None.

    A page's layout is read along with the page: the reservation and any hairline
    a template draws across its footer live on the layout, they render on every
    page built on it, and nothing copies them onto the slide -- so a reading taken
    off the slides alone finds a footer on none of the twelve shipped templates.
    """
    floor = canvas_h * _FOOTER_REGION
    tops: Counter[float] = Counter()
    for number in content:
        slide = slides[number - 1]
        found: set[float] = set()
        for source in (slide, slide.slide_layout):
            for shape in iter_shapes(source.shapes):
                box = page_box(shape)
                if box is None or box.y0 < floor:
                    continue
                if _is_footer_role(shape) or _is_mark(shape, box.height):
                    found.add(round(box.y0, 2))
        for top in found:
            tops[top] += 1
    stable = [top for top, count in tops.items() if count >= agreed]
    return min(stable) if stable else None


def _is_footer_role(shape) -> bool:
    if not getattr(shape, "is_placeholder", False):
        return False
    kind = str(getattr(getattr(shape, "placeholder_format", None), "type", ""))
    return any(role in kind for role in _FOOTER_ROLES)


def _is_mark(shape, height: float) -> bool:
    if height <= _RULE_HEIGHT_IN:
        return True
    if not getattr(shape, "has_text_frame", False):
        return False
    text = shape.text_frame.text.strip()
    return bool(text) and len(text) <= _MARK_CHARS
