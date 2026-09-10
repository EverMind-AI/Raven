"""The style a page has to keep when it is not cloned from the template at all.

Two rounds of live decks decided this. Cloning a template's content page and
replacing what is in it works for the pages a template is *made* of -- a cover, a
contents list, a section divider, a closing -- and fails on the pages a deck is
made of. The measured reasons, all from real runs: a six-card prototype filled
with four cards leaves a hole where the other two were; a slot drawn for a
three-word phrase sets a sentence at 10.8pt because the box shrinks its own text;
a portrait picture frame cannot take a landscape figure; and the numbers a review
deck exists to show do not fit the shape of anybody's marketing template.

So a content page is drawn rather than filled, and this is what it borrows: the
layout its background lives on, where the title row sits, the type scale, the
faces, and the area the template keeps its content inside. Measured off the
template's own example pages, because that is where a template's design actually
is -- 85% of it, against 15% on the layouts -- and because a number measured off
the file is a number every page of the deck can be held to afterwards.

What this deliberately does not carry: any of the template's example *arrangements*.
The three-card row, the timeline, the quadrant are exactly what a page has to be
free to not be.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from raven_ppt.services.measure.geometry import EMU_PER_INCH, EMU_PER_POINT, PICTURE, iter_shapes
from raven_ppt.services.measure.type_size import rendered_spans, slots
from raven_ppt.services.template.capacity import row_band
from raven_ppt.services.template.menu import PageEntry, menu

# The top of the page, where a title row lives. Measured over the example pages of
# 119 templates: a page's title sits in the top third and its subtitle immediately
# under it, and taking the top half instead pulled body copy into the title vote.
_TITLE_BAND = 0.35
# Two pages agreeing on a box is a house rule; one page is that page's own idea.
_AGREEMENT = 2
# Under this many characters a text shape is a label or a step number rather than
# body copy, and the deck sets those at whatever size the mark wants.
_BODY_CHARS = 12

_DRAWINGML = "http://schemas.openxmlformats.org/drawingml/2006/main"
_PRESENTATIONML = "http://schemas.openxmlformats.org/presentationml/2006/main"
_NAMESPACE = {"a": _DRAWINGML, "p": _PRESENTATIONML}

# DrawingML's own words for the two settings that decide where a row's ink lands
# inside a box that is otherwise identical, in `ppt_layout.write`'s spelling.
_ANCHORS = {"t": "top", "ctr": "middle", "b": "bottom", "just": "top", "dist": "top"}
_ALIGNS = {"l": "left", "ctr": "center", "r": "right", "just": "left", "dist": "left"}
# What a text box with nothing declared anywhere above it comes out as, which is also
# what `ppt_layout.write` does when the caller names neither.
_DEFAULT_ANCHOR = "top"
_DEFAULT_ALIGN = "left"
# Which way copy past the upper bound leaves a row written at this anchor. Bottom is
# the one that destroys the page rather than spoiling it: at title size the copy grows
# up and its first line goes off the top of the canvas, and 10% of the bundled
# templates' boxes are anchored there while three of the ten anchor none.
_SPILLS_TO = {
    "top": ", and more spills below it",
    "middle": ", and more spills past both edges",
    "bottom": ", and more spills upward off the top",
}


@dataclass(frozen=True)
class Row:
    """A row the template repeats on its content pages: where it is and how it is set."""

    box: tuple[float, float, float, float]
    """(left, top, width, height) in inches."""
    size_pt: float | None
    bold: bool
    colour: str | None
    """rrggbb, or None when the run takes its colour from the theme."""
    face: str | None
    align: str | None
    anchor: str | None = None
    """Where the copy sits inside the box: "top", "middle" or "bottom".

    Measured because the box alone does not say where the ink lands, and the two
    readings of one box differ by most of its height. This template's title
    placeholder declares nothing and its master's declares `anchor="b"`, so a cloned
    page's single-line title sinks to the bottom of a 0.98in row while a composed page
    calling `write` -- which anchors to the top -- puts the same line 0.375in higher.
    Measured on a delivered 20-page deck at 150 DPI: cloned titles start at y=53px,
    composed ones at y=23px, the same 29px glyph height on both."""
    pages: int = 1
    """How many of the template's content pages put this row in the same place."""
    capacity: str = ""
    """How much copy the row takes at this size, from `capacity.row_band`.

    The line reported the box, the size, the face, the alignment and the anchor and
    stopped one number short of the one an author needs before writing into it: the
    11.88x0.98in row eight of the bundled templates share is 56-75 Latin characters at
    the 28pt its pages render, and nothing above said so. A written box never shrinks
    (`ppt_layout.write` sets `auto_size` to none), so past the upper bound the copy
    spills the way `anchor` says it will -- and this row is anchored bottom, which is
    upward, off the top of the slide."""

    def line(self) -> str:
        left, top, width, height = self.box
        parts = [f"at ({left:.2f}, {top:.2f}) {width:.2f}x{height:.2f}in"]
        if self.size_pt:
            parts.append(f"{self.size_pt:g}pt{' bold' if self.bold else ''}")
        if self.face:
            parts.append(self.face)
        if self.colour:
            parts.append(f"#{self.colour}")
        if self.align:
            parts.append(f"{self.align}-aligned")
        if self.anchor:
            parts.append(f"anchored {self.anchor}")
        if self.capacity:
            parts.append(self.capacity + _SPILLS_TO[self.anchor or _DEFAULT_ANCHOR])
        return ", ".join(parts)


@dataclass(frozen=True)
class House:
    """Everything a page drawn from scratch needs to still look like this template."""

    canvas: tuple[float, float]
    layout: str | None
    """The layout the template's own content pages sit on, so a new page added to it
    inherits the same background and decoration rather than a blank one."""
    title: Row | None
    subtitle: Row | None
    scale: dict[str, float] = field(default_factory=dict)
    """Sizes in points, by role: title, subtitle, heading, body, caption."""
    faces: dict[str, str] = field(default_factory=dict)
    safe: tuple[float, float, float, float] | None = None
    """The area the template keeps its content inside, (left, top, width, height) in
    inches, measured off where its own pages put text."""
    content_pages: tuple[int, ...] = ()
    structural: dict[str, int] = field(default_factory=dict)
    """role -> the template page that plays it, for the pages that are cloned."""

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["title"] = self.title.line() if self.title else None
        payload["subtitle"] = self.subtitle.line() if self.subtitle else None
        return payload

    @property
    def body_area(self) -> tuple[float, float, float, float] | None:
        """What is left of the safe area under the title row: where content goes.

        Computed rather than reported separately because the two numbers an author
        needs are "where the title goes" and "where everything else goes", and the
        second is the first subtracted from the area the template keeps inside.
        """
        if self.safe is None:
            return None
        left, top, width, height = self.safe
        rows = [row for row in (self.title, self.subtitle) if row is not None and row.pages >= _AGREEMENT]
        if rows:
            below = max(row.box[1] + row.box[3] for row in rows)
            top, height = max(top, below + 0.12), height - max(0.0, below + 0.12 - top)
        return (round(left, 2), round(top, 2), round(width, 2), round(max(height, 0.0), 2))

    def brief(self) -> dict[str, Any]:
        """The house style as an author reads it: every box in corners, and a line to paste.

        Two corners throughout, the reading `ppt_layout.Box` takes, because every call an
        author makes with these numbers is a `ppt_layout` one -- `Box.columns`, `.rows`,
        `.grid`, `plane()`, `write()`, `clear_region` -- and all of them take a box rather
        than a size. This used to report `(left, top, width, height)` one line above a
        paste-able `Box.corners(...)` built from the same rectangle, which is two silently
        different readings of it a line apart: one live run read the whole call by the two
        numbers they share and wrote a size into a box of its own. An author that wants the
        python-pptx size has `Box.at`, `.w`, `.h` and `.pptx()` to get it from a box; an
        author holding a bare size has no safe way back to a box, which is why the box is
        what ships.
        """
        payload: dict[str, Any] = {"canvas_in": [round(self.canvas[0], 2), round(self.canvas[1], 2)]}
        if self.layout:
            payload["layout_for_a_page_you_draw"] = self.layout
        if self.title:
            payload["title_row"] = self.title.line()
            payload["title_row_box_corners_in"] = _corners(self.title.box)
            payload["title_row_on_pages"] = self.title.pages
            payload["title_row_as_code"] = _as_code("TITLE_ROW", self.title)
        if self.subtitle and self.subtitle.pages >= _AGREEMENT:
            payload["subtitle_row"] = self.subtitle.line()
            payload["subtitle_row_box_corners_in"] = _corners(self.subtitle.box)
            payload["subtitle_row_as_code"] = _as_code("SUBTITLE_ROW", self.subtitle)
        if self.scale:
            payload["type_pt"] = dict(self.scale)
        if self.faces.get("text"):
            payload["face"] = self.faces["text"]
        if self.safe:
            payload["safe_area_corners_in"] = _corners(self.safe)
        body = self.body_area
        if body:
            payload["body_area_corners_in"] = _corners(body)
            payload["body_area_as_code"] = (
                f"from ppt_layout import Box; body = Box.corners({body[0]:g}, {body[1]:g}, "
                f"{body[0] + body[2]:g}, {body[1] + body[3]:g})"
            )
        return payload


def _corners(box: tuple[float, float, float, float]) -> list[float]:
    """One measured `(left, top, width, height)` as the two corners a box is given in.

    The subtraction lives here rather than in the reader: a model doing it in its head is
    a page half an inch off the grid the template keeps, and a model not realising it was
    owed is a rectangle of the wrong size entirely.
    """
    left, top, width, height = box
    return [round(left, 2), round(top, 2), round(left + width, 2), round(top + height, 2)]


def _as_code(name: str, row: Row) -> str:
    """The row as the one `write` call that puts copy where the template puts it.

    The same argument `body_area_as_code` is here for, one field further on. The box
    alone was reported and the box alone is not where the ink lands: the anchor and
    the alignment decide that, both of them are the template's decision, and both of
    `ppt_layout.write`'s defaults are the other answer. A live deck read the box,
    wrote the title with the defaults, and every composed page put its title 0.375in
    above every cloned one -- 5% of the page, on alternating pages, at the one place a
    reader's eye returns to on every page of a deck.
    """
    left, top, width, height = row.box
    call = [
        f"{name} = Box.corners({left:g}, {top:g}, {left + width:g}, {top + height:g})",
        f"write(slide, {name}, claim",
    ]
    if row.size_pt:
        call.append(f"size={row.size_pt:g}")
    if row.bold:
        call.append("bold=True")
    call.append(f'align="{row.align or _DEFAULT_ALIGN}"')
    call.append(f'anchor="{row.anchor or _DEFAULT_ANCHOR}"')
    return f"from ppt_layout import Box, write; {call[0]}; {', '.join(call[1:])}, colour=INK, font=FACE, cjk_font=HAN)"


def house_style(path: Path, entries: tuple[PageEntry, ...] | None = None, pdf: Path | None = None) -> House | None:
    """Measure the house style off a template's example pages.

    `entries` is the menu, when the caller already has one -- the roles decide which
    pages are content pages, and reading the file twice to find that out is the cost
    this parameter avoids.

    `pdf` is a render of the same file, and without it the type scale is a guess. A
    template's title placeholder usually declares no size at all -- it inherits one
    from its layout, and a run in it says only "bold" -- so reading sizes off the file
    put this template's title row at 24pt when its pages render it at 28, and picked a
    7pt footnote as the body size. The renderer has resolved every inheritance and every
    autofit, so with a render the numbers are the ones on the page.
    """
    try:
        from pptx import Presentation
    except ImportError:  # pragma: no cover -- python-pptx ships with the extra
        return None
    try:
        presentation = Presentation(str(path))
    except Exception:  # noqa: BLE001 -- a malformed file is simply not a template
        return None
    listed = entries if entries is not None else menu(path)
    if not listed:
        return None
    canvas = (presentation.slide_width / EMU_PER_INCH, presentation.slide_height / EMU_PER_INCH)
    content = [entry.number for entry in listed if not entry.role]
    slides = list(presentation.slides)
    set_at = _rendered_sizes(path, pdf)
    titles, subtitles, sizes, faces, extents, layouts = [], [], Counter(), Counter(), [], Counter()
    for number in content:
        if number > len(slides):
            continue
        slide = slides[number - 1]
        layouts[slide.slide_layout.name or ""] += 1
        on_page = set_at.get(number, {})
        title, subtitle = _rows(slide, canvas, on_page)
        if title is not None:
            titles.append(title)
        if subtitle is not None:
            subtitles.append(subtitle)
        for size, chars in _sizes(slide, canvas, on_page).items():
            sizes[size] += chars
        for face, chars in _faces(slide).items():
            faces[face] += chars
        box = _text_extent(slide, canvas)
        if box is not None:
            extents.append(box)
    return House(
        canvas=canvas,
        layout=layouts.most_common(1)[0][0] if layouts else None,
        title=_agreed(titles),
        subtitle=_agreed(subtitles),
        scale=_scale(titles, subtitles, sizes, _ladder(presentation)),
        faces=_face_roles(faces),
        safe=_safe(extents, canvas),
        content_pages=tuple(content),
        structural={entry.role: entry.number for entry in listed if entry.role},
    )


def _rendered_sizes(path: Path, pdf: Path | None) -> dict[int, dict[tuple[int, int], float]]:
    """page -> (left, top) in whole points -> the size that box's copy came out at.

    Keyed on position because that is what a shape and a measured slot share: the slot
    was built from the shape's own declared box, so rounding both to the point matches
    them exactly and needs no identity to travel between the two modules.
    """
    if pdf is None or not pdf.is_file():
        return {}
    spans = rendered_spans(pdf)
    if not spans:
        return {}
    found: dict[int, dict[tuple[int, int], float]] = {}
    for slot in slots(path, spans):
        if slot.rendered_pt is None:
            continue
        found.setdefault(slot.page, {})[(round(slot.box.x0), round(slot.box.y0))] = slot.rendered_pt
    return found


def _set_at(shape, on_page: dict[tuple[int, int], float]) -> float | None:
    """The size this shape's copy came out at, or what the file says when nothing did."""
    if shape.left is not None and shape.top is not None:
        rendered = on_page.get((round(shape.left / EMU_PER_POINT), round(shape.top / EMU_PER_POINT)))
        if rendered is not None:
            return rendered
    return _shape_size(shape)


def _rows(slide, canvas: tuple[float, float], on_page: dict[tuple[int, int], float]) -> tuple[Row | None, Row | None]:
    """The title and subtitle rows of one page, largest type first.

    Both come out of the same pass because the subtitle is defined by the title: it
    is the next-largest row under it, and a page whose title was picked wrongly gets
    its subtitle wrong too.
    """
    band = canvas[1] * _TITLE_BAND
    up_top = []
    placeholder = None
    for shape in iter_shapes(slide.shapes):
        if not getattr(shape, "has_text_frame", False) or shape.top is None:
            continue
        if not shape.text_frame.text.strip() or shape.top / EMU_PER_INCH > band:
            continue
        # The page's title placeholder, when it has one, is the title -- whatever size it
        # declares. Without this the fallback (no render, so declared sizes only) picked
        # the largest *stated* size in the band, and a title placeholder states none: on
        # a real template it chose a decorative 24pt line at (0.72, 1.72) over the title
        # row at (0.72, 0.14) that seven of its pages agree on, and every check built on
        # the answer went quiet.
        if getattr(shape, "is_placeholder", False) and "TITLE" in str(
            getattr(getattr(shape, "placeholder_format", None), "type", "")
        ):
            placeholder = placeholder or shape
        up_top.append((_set_at(shape, on_page) or 0.0, shape))
    if not up_top:
        return (None, None)
    if placeholder is not None:
        rest = [shape for _size, shape in up_top if shape._element is not placeholder._element]
        rest.sort(key=lambda shape: shape.top)
        below = [shape for shape in rest if shape.top >= placeholder.top]
        return (_row(placeholder, on_page, slide), _row(below[0], on_page, slide) if below else None)
    up_top.sort(key=lambda pair: (-pair[0], pair[1].top))
    title = _row(up_top[0][1], on_page, slide)
    rest = [shape for size, shape in up_top[1:] if shape.top >= up_top[0][1].top]
    return (title, _row(rest[0], on_page, slide) if rest else None)


def inherited_placeholders(slide, shape) -> list:
    """`shape`, then every placeholder it inherits from, nearest first.

    A slide's placeholder declares almost nothing -- the one on this template's own
    pages is a bare `<a:bodyPr/>` -- and everything that decides how its copy is set
    lives one or two files up: the layout's placeholder of the same `idx`, and the
    master's of the same kind. python-pptx models the three files and none of the
    inheritance between them, so a reading taken off the slide alone reports "nothing
    declared" for every setting the template actually made.

    The master is matched by kind rather than by index, which is how PowerPoint
    resolves it: a master carries one title and one body placeholder, and every
    body-ish placeholder on a layout inherits from that one body.
    """
    chain = [shape]
    if not getattr(shape, "is_placeholder", False):
        return chain
    try:
        kind = str(shape.placeholder_format.type or "")
        index = shape.placeholder_format.idx
    except (AttributeError, ValueError):
        return chain
    layout = getattr(slide, "slide_layout", None)
    if layout is None:
        return chain
    for candidate in layout.placeholders:
        if candidate.placeholder_format.idx == index:
            chain.append(candidate)
            break
    master = getattr(layout, "slide_master", None)
    if master is None:
        return chain
    titled = "TITLE" in kind
    for candidate in master.placeholders:
        on_master = str(candidate.placeholder_format.type or "")
        if titled == ("TITLE" in on_master) and ("TITLE" in on_master or "BODY" in on_master):
            chain.append(candidate)
            break
    return chain


def title_anchor(slide, shape) -> str:
    """Where this row's copy sits inside its box, resolved the way a renderer does."""
    for candidate in inherited_placeholders(slide, shape):
        body = candidate.text_frame._txBody.find(f"{{{_DRAWINGML}}}bodyPr")  # noqa: SLF001 -- no API for bodyPr
        stated = body.get("anchor") if body is not None else None
        if stated:
            return _ANCHORS.get(stated, _DEFAULT_ANCHOR)
    return _DEFAULT_ANCHOR


def _align(slide, shape) -> str:
    """How this row's copy is set across its box, resolved the same way.

    The master's text styles are the last stop rather than the first: `titleStyle`
    states the alignment every title inherits, and this template's says `l` while not
    one placeholder in the chain above it says anything at all.
    """
    for candidate in inherited_placeholders(slide, shape):
        for para in candidate.text_frame.paragraphs:
            properties = para._pPr  # noqa: SLF001 -- python-pptx maps alignment but not inheritance
            stated = properties.get("algn") if properties is not None else None
            if stated:
                return _ALIGNS.get(stated, _DEFAULT_ALIGN)
        listed = candidate.text_frame._txBody.find(f"{{{_DRAWINGML}}}lstStyle/{{{_DRAWINGML}}}lvl1pPr")  # noqa: SLF001
        stated = listed.get("algn") if listed is not None else None
        if stated:
            return _ALIGNS.get(stated, _DEFAULT_ALIGN)
    return _styled_align(slide, shape)


def _styled_align(slide, shape) -> str:
    """The master's `titleStyle` / `bodyStyle` answer for this kind of row."""
    layout = getattr(slide, "slide_layout", None)
    master = getattr(layout, "slide_master", None) if layout is not None else None
    if master is None:
        return _DEFAULT_ALIGN
    kind = str(getattr(getattr(shape, "placeholder_format", None), "type", "")) if shape.is_placeholder else ""
    style = "p:titleStyle" if "TITLE" in kind else "p:bodyStyle"
    node = master.element.find(f"p:txStyles/{style}/a:lvl1pPr", _NAMESPACE)
    stated = node.get("algn") if node is not None else None
    return _ALIGNS.get(stated, _DEFAULT_ALIGN) if stated else _DEFAULT_ALIGN


def _row(shape, on_page: dict[tuple[int, int], float], slide=None) -> Row:
    frame = shape.text_frame
    run = next(
        (run for para in frame.paragraphs for run in para.runs if run.text.strip()),
        None,
    )
    para = next((para for para in frame.paragraphs if para.text.strip()), None)
    colour = None
    if run is not None:
        try:
            colour = str(run.font.color.rgb)
        except Exception:  # noqa: BLE001 -- a theme colour has no rgb of its own
            colour = None
    return Row(
        box=(
            round(shape.left / EMU_PER_INCH, 2),
            round(shape.top / EMU_PER_INCH, 2),
            round((shape.width or 0) / EMU_PER_INCH, 2),
            round((shape.height or 0) / EMU_PER_INCH, 2),
        ),
        size_pt=_set_at(shape, on_page),
        bold=bool(run is not None and run.font.bold),
        colour=colour,
        face=(run.font.name if run is not None else None),
        align=(
            str(para.alignment).split()[0].lower()
            if para is not None and para.alignment
            else (_align(slide, shape) if slide is not None else None)
        ),
        anchor=title_anchor(slide, shape) if slide is not None else None,
        # The rendered size and not the declared one: a template's title placeholder
        # usually declares none, and a band computed off a layout's 24pt for a row the
        # page draws at 28pt is 14% of a headline out.
        capacity=row_band(shape, _set_at(shape, on_page)),
        pages=1,
    )


def _agreed(rows: list[Row]) -> Row | None:
    """The row the content pages agree on, with the agreement counted.

    The most common box wins rather than the first: a template's thirteen pages
    include one that puts its title somewhere else, and that page is not the rule.
    """
    if not rows:
        return None
    boxes = Counter(row.box for row in rows)
    box, count = boxes.most_common(1)[0]
    agreed = next(row for row in rows if row.box == box)
    return Row(**{**asdict(agreed), "pages": count}) if count >= _AGREEMENT else Row(**{**asdict(rows[0]), "pages": 1})


def _shape_size(shape) -> float | None:
    sizes = [
        run.font.size.pt
        for para in shape.text_frame.paragraphs
        for run in para.runs
        if run.font.size is not None and run.text.strip()
    ]
    if sizes:
        return round(max(sizes), 1)
    sizes = [para.font.size.pt for para in shape.text_frame.paragraphs if para.font.size is not None]
    return round(max(sizes), 1) if sizes else None


def _sizes(slide, canvas: tuple[float, float], on_page: dict[tuple[int, int], float]) -> dict[float, int]:
    """The sizes this page's body copy comes out at, by how many characters are at each.

    Below the title band and copy rather than marks, because the ladder wanted here is
    body / heading / caption -- a page number and a step bubble are set at whatever the
    mark wants and would otherwise be voted in as the deck's caption size.
    """
    band = canvas[1] * _TITLE_BAND
    found: dict[float, int] = {}
    for shape in iter_shapes(slide.shapes):
        if not getattr(shape, "has_text_frame", False) or shape.top is None:
            continue
        text = shape.text_frame.text.strip()
        if len(text) < _BODY_CHARS or shape.top / EMU_PER_INCH <= band:
            continue
        size = _set_at(shape, on_page)
        if size is None:
            continue
        key = round(size, 1)
        found[key] = found.get(key, 0) + len(text)
    return found


def _faces(slide) -> dict[str, int]:
    found: dict[str, int] = {}
    for shape in iter_shapes(slide.shapes):
        if not getattr(shape, "has_text_frame", False):
            continue
        for para in shape.text_frame.paragraphs:
            for run in para.runs:
                text = run.text.strip()
                if not text or not run.font.name:
                    continue
                found[run.font.name] = found.get(run.font.name, 0) + len(text)
    return found


def _face_roles(faces: Counter) -> dict[str, str]:
    """The face the pages actually use, which is not always the one the theme names.

    One template's theme declares 微软雅黑 and all 203 runs on its example pages are
    set in Arial. An author told the theme's answer sets its deck in a face the
    template never uses, and every page of it looks subtly foreign next to the
    template's own.
    """
    return {"text": faces.most_common(1)[0][0]} if faces else {}


def _scale(titles: list[Row], subtitles: list[Row], sizes: Counter, ladder: dict[str, float]) -> dict[str, float]:
    """The template's own type ladder, in points.

    The master's text styles first, because that is the ladder the template's designer
    wrote down: `titleStyle` and `bodyStyle` carry one size per outline level, and on
    this template they say 28 / 18 / 16 / 14 / 12 -- which the measured title row
    independently renders at 28. Reading the example pages instead gave 8pt for body
    copy, and it was not wrong about the pixels: the template's own placeholder
    paragraphs are longer than the slots they sit in, so every one of them is autofit
    down to 8pt. A size a template shrank its own filler to is not the size it means.

    The rendered pages are the fallback for a master that declares nothing, and the
    measured title row wins over `titleStyle` when both exist -- a title placeholder
    moved and resized on the pages themselves is the more specific statement.
    """
    scale: dict[str, float] = {}
    measured = Counter(row.size_pt for row in titles if row.size_pt)
    if measured:
        scale["title"] = measured.most_common(1)[0][0]
    elif ladder.get("title"):
        scale["title"] = ladder["title"]
    found = Counter(row.size_pt for row in subtitles if row.size_pt)
    if found:
        scale["subtitle"] = found.most_common(1)[0][0]
    for role in ("body", "secondary", "caption"):
        if ladder.get(role):
            scale[role] = ladder[role]
    if "body" not in scale and sizes:
        # No ladder in the master: the largest size the example pages set real copy at.
        # The largest rather than the most common, because autofit only ever shrinks and
        # the biggest surviving setting is the one closest to what the slot was drawn for.
        scale["body"] = max(sizes)
    return scale


def _ladder(presentation) -> dict[str, float]:
    """The master's declared sizes, by the role each outline level plays."""
    namespace = {
        "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
        "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    }
    for master in presentation.slide_masters:
        styles = master.element.find("p:txStyles", namespace)
        if styles is None:
            continue
        found: dict[str, float] = {}
        title = styles.find("p:titleStyle/a:lvl1pPr/a:defRPr", namespace)
        if title is not None and title.get("sz"):
            found["title"] = int(title.get("sz")) / 100
        for level, role in ((1, "body"), (2, "secondary"), (3, "caption")):
            node = styles.find(f"p:bodyStyle/a:lvl{level}pPr/a:defRPr", namespace)
            if node is not None and node.get("sz"):
                found[role] = int(node.get("sz")) / 100
        if found:
            return found
    return {}


def _text_extent(slide, canvas: tuple[float, float]) -> tuple[float, float, float, float] | None:
    """Where this page keeps its text, ignoring anything that bleeds off the canvas."""
    boxes = []
    for shape in iter_shapes(slide.shapes):
        if getattr(shape, "shape_type", None) == PICTURE or not getattr(shape, "has_text_frame", False):
            continue
        if shape.left is None or not shape.text_frame.text.strip():
            continue
        left, top = shape.left / EMU_PER_INCH, shape.top / EMU_PER_INCH
        right = left + (shape.width or 0) / EMU_PER_INCH
        bottom = top + (shape.height or 0) / EMU_PER_INCH
        if left < -0.1 or top < -0.1 or right > canvas[0] + 0.1 or bottom > canvas[1] + 0.1:
            continue
        boxes.append((left, top, right, bottom))
    if not boxes:
        return None
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _safe(extents: list[tuple[float, float, float, float]], canvas: tuple[float, float]) -> tuple[float, ...] | None:
    """The area the template's content pages keep inside, as (left, top, width, height).

    The median edge rather than the union: one page bleeding a caption into the margin
    would otherwise hand every page of the deck permission to do the same.
    """
    if not extents:
        return None

    def middle(values: list[float]) -> float:
        ordered = sorted(values)
        return ordered[len(ordered) // 2]

    left = middle([box[0] for box in extents])
    top = middle([box[1] for box in extents])
    right = middle([box[2] for box in extents])
    bottom = middle([box[3] for box in extents])
    left, top = max(left, 0.0), max(top, 0.0)
    right, bottom = min(right, canvas[0]), min(bottom, canvas[1])
    if right - left <= 1 or bottom - top <= 1:
        return None
    return (round(left, 2), round(top, 2), round(right - left, 2), round(bottom - top, 2))
