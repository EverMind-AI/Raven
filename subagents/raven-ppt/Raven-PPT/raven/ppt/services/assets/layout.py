"""The page grid, projected as a module an author's build script imports.

Three live runs wrote every rectangle by eye. What they produced was measurable:
labels needing 4.2in in a 3.0in box so they wrapped mid-phrase, copy painted over
copy, and body type pushed to 12.5pt to make invented boxes fit. And what a reader
saw was a page with no visible structure -- gaps of six different sizes, rows that
did not line up, nothing to separate one group from the next -- because nothing
shared a grid.

So the grid is code. An author asks a page for its regions and asks a region to
divide itself; the gutters are one number, the safe margin is one number, and every
box a program draws is as wide as the column it sits in. Zones become visible
because a region can paint itself in the theme's own surface tint rather than being
implied by whitespace the author eyeballed.

The type ramp is here for the same reason: a size chosen per page drifts, and the
floor the measurements enforce (14pt body) belongs next to the sizes rather than in
a document the author may not re-read.

`raven.ppt.services.assets.script_helpers` writes this as `ppt_layout.py` beside
the script. Coordinates are inches, because that is what python-pptx's `Inches`
wants and what the author reads in the reference.
"""

from __future__ import annotations

LAYOUT_MODULE_FILENAME = "ppt_layout.py"

_LAYOUT_MODULE = '''"""The page grid: regions, divisions and the type ramp.

    from ppt_layout import page, GUTTER, BODY_PT, plane, write

    T = THEMES["indigo-scholar"]
    FONT, HAN = T["font_family"], T["cjk_font_family"]
    frame = page()                        # kicker / title / body / footer
    left, right = frame.body.split_left(0.58)
    for card in right.rows(3):
        plane(slide, card, T)             # the zone, visible
        write(slide, card.inset(0.22), "…", size=BODY_PT, colour=INK, font=FONT, cjk_font=HAN)

Every box is inches: `box.pptx()` unpacks straight into python-pptx.
Never write a coordinate by eye -- ask a region to divide itself, and the gutters,
the alignment and the safe margin come out right by construction.
"""

from collections import namedtuple

from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.util import Inches, Pt

_A = "http://schemas.openxmlformats.org/drawingml/2006/main"

CANVAS_W, CANVAS_H = 13.333, 7.5

# The safe area. Type outside it reads as falling off the page, and a projector
# crops less predictably than a screen.
MARGIN = 0.72
# One gap, everywhere. Six different gaps on one page is what "no structure" looks
# like; a single number is what makes a three-card row read as three cards.
GUTTER = 0.28
# Space inside a plane before its copy starts.
PAD = 0.22

# The ramp. Sizes drift when they are chosen per page, so they are chosen once.
# BODY_PT sits above the 14pt floor the measurements enforce, with room to drop one
# step and still clear it.
KICKER_PT = 12
TITLE_PT = 30
LEAD_PT = 20
BODY_PT = 16
LABEL_PT = 14
BODY_FLOOR_PT = 14
NUMBER_PT = 40

# The gap between the heading's ground and the first thing on the page. Touching
# reads as welded -- the same thing that makes an accent strip on a card's edge look
# generated -- so the band stops short of the body.
_HEADING_AIR = 0.07

_KICKER_H = 0.30
_TITLE_H = 0.72
_RULE_GAP = 0.16
_FOOTER_H = 0.30


class Box(namedtuple("Box", "x0 y0 x1 y1")):
    """A rectangle in inches, which knows how to divide itself."""

    __slots__ = ()

    @property
    def w(self):
        return self.x1 - self.x0

    @property
    def h(self):
        return self.y1 - self.y0

    def pptx(self):
        """(left, top, width, height) as python-pptx lengths."""
        return Inches(self.x0), Inches(self.y0), Inches(self.w), Inches(self.h)

    def inset(self, dx=PAD, dy=None):
        dy = dx if dy is None else dy
        return Box(self.x0 + dx, self.y0 + dy, self.x1 - dx, self.y1 - dy)

    def columns(self, n, gutter=GUTTER, weights=None):
        """`n` side-by-side boxes filling this one, separated by `gutter`."""
        return self._divide(n, gutter, weights, vertical=False)

    def rows(self, n, gutter=GUTTER, weights=None):
        """`n` stacked boxes filling this one, separated by `gutter`."""
        return self._divide(n, gutter, weights, vertical=True)

    def grid(self, cols, rows, gutter=GUTTER):
        """Row-major cells, so `grid(3, 2)[4]` is the second row's middle cell."""
        return [cell for band in self.rows(rows, gutter) for cell in band.columns(cols, gutter)]

    def split_left(self, fraction, gutter=GUTTER):
        """This box as (left, right), the left one taking `fraction` of the width."""
        left, right = self.columns(2, gutter, weights=(fraction, 1 - fraction))
        return left, right

    def split_top(self, fraction, gutter=GUTTER):
        """This box as (top, bottom), the top one taking `fraction` of the height."""
        top, bottom = self.rows(2, gutter, weights=(fraction, 1 - fraction))
        return top, bottom

    def _divide(self, n, gutter, weights, vertical):
        if n < 1:
            raise ValueError("a region divides into at least one part")
        span = (self.h if vertical else self.w) - gutter * (n - 1)
        if span <= 0:
            raise ValueError(f"{n} parts and {gutter}in gutters do not fit in {self.w:.2f}x{self.h:.2f}in")
        shares = list(weights or [1] * n)
        if len(shares) != n:
            raise ValueError(f"{len(shares)} weights for {n} parts")
        total = float(sum(shares))
        parts, offset = [], self.y0 if vertical else self.x0
        for share in shares:
            size = span * share / total
            if vertical:
                parts.append(Box(self.x0, offset, self.x1, offset + size))
            else:
                parts.append(Box(offset, self.y0, offset + size, self.y1))
            offset += size + gutter
        return parts


Frame = namedtuple("Frame", "kicker title body footer")


def page(kicker=True, footer=True):
    """The regions of an ordinary page, inside the safe area.

    `kicker` is the small section label over the title; `footer` is the line a
    source citation goes on. Both take their space out of the body when asked for,
    so a page that skips them gets the room rather than leaving a hole.
    """
    top = MARGIN
    kicker_box = Box(MARGIN, top, CANVAS_W - MARGIN, top + _KICKER_H)
    if kicker:
        top += _KICKER_H + 0.04
    title_box = Box(MARGIN, top, CANVAS_W - MARGIN, top + _TITLE_H)
    top += _TITLE_H + _RULE_GAP
    bottom = CANVAS_H - MARGIN
    footer_box = Box(MARGIN, bottom - _FOOTER_H, CANVAS_W - MARGIN, bottom)
    if footer:
        bottom -= _FOOTER_H + GUTTER
    return Frame(kicker_box, title_box, Box(MARGIN, top, CANVAS_W - MARGIN, bottom), footer_box)


def rule(slide, box, theme, thickness=0.03, colour=None):
    """The kicker rule under a title: a hairline, in the accent unless told otherwise."""
    shape = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, Inches(box.x0), Inches(box.y1 + 0.06), Inches(min(1.05, box.w)), Inches(thickness)
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(colour or theme["accent"])
    shape.line.fill.background()
    shape.shadow.inherit = False
    return shape


def plane(slide, box, theme, tint="surface", radius=False):
    """Paint a region, so the page's divisions are visible rather than implied.

    `tint` names a theme colour -- "surface" for a grouped zone, "accent_soft" for
    the one zone that carries the page's answer. No outline: an edge and a tint
    together read as a form to fill in.
    """
    shape = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE if radius else MSO_SHAPE.RECTANGLE, *box.pptx()
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(theme[tint] if tint in theme else tint)
    shape.line.fill.background()
    shape.shadow.inherit = False
    if radius:
        shape.adjustments[0] = 0.06
    return shape


def write(
    slide,
    box,
    text,
    *,
    size=BODY_PT,
    colour="#000000",
    font=None,
    cjk_font=None,
    bold=False,
    align="left",
    anchor="top",
    spacing=1.15,
):
    """Copy in a box, wrapping inside it and never shrinking to fit.

    The box is the box: autofit is off, so a page that does not fit comes back as a
    measurement instead of as type quietly dropping below the floor.

    `font` is the Latin face and `cjk_font` its CJK companion -- pass both from the
    theme (`T["font_family"]`, `T["cjk_font_family"]`) and one line of mixed text
    comes out right: "TarViS" in the Latin face, 目标查询 in the CJK one. Naming only
    the Latin face leaves every Han character to the viewer's fallback.
    """
    left, top, width, height = box.pptx()
    frame = slide.shapes.add_textbox(left, top, width, height).text_frame
    frame.word_wrap = True
    frame.auto_size = MSO_AUTO_SIZE.NONE
    frame.margin_left = frame.margin_right = Inches(0.04)
    frame.margin_top = frame.margin_bottom = Inches(0.02)
    frame.vertical_anchor = _named(_ANCHORS, anchor, "anchor")
    lines = text if isinstance(text, (list, tuple)) else [text]
    for index, line in enumerate(lines):
        para = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        para.text = str(line)
        para.alignment = _named(_ALIGNS, align, "align")
        para.line_spacing = spacing
        for run in para.runs:
            run.font.size = Pt(size)
            run.font.bold = bold
            run.font.color.rgb = _rgb(colour)
            if font:
                run.font.name = font
            if cjk_font:
                _east_asian(run, cjk_font)
    return frame


def _east_asian(run, name):
    """Set the run's east-asian face, which python-pptx has no property for.

    `run.font.name` writes `a:latin` only, and a renderer picks the face for a Han
    character from `a:ea`. Without this the two are decided by different rules and a
    mixed line comes out in two unrelated designs.
    """
    properties = run.font._rPr  # noqa: SLF001 -- the only way to reach a:ea
    for existing in properties.findall(f"{{{_A}}}ea"):
        properties.remove(existing)
    element = properties.makeelement(f"{{{_A}}}ea", {"typeface": name})
    latin = properties.find(f"{{{_A}}}latin")
    if latin is not None:
        latin.addnext(element)
    else:
        properties.append(element)


def table(slide, box, rows, theme, *, weights=None, size=LABEL_PT, numeric_from=1):
    """A table that reads like a table in a book, not like a spreadsheet.

    python-pptx hands you the Office default, and the default is why tables come out
    looking cheap: a white hairline around every cell, banding on, and a header style
    that fights whatever palette the deck is in. On a dark page the grid is the loudest
    thing on the slide. Measured on a live deck: 25 cells, every one outlined, two
    columns filled in colours the theme does not contain, and 0.9in rows holding one
    line of text.

    So this draws the other kind: no vertical rules at all, no banding, no filled
    columns, one accent rule under the header, and a hairline between rows in the
    theme's own muted tone. Separation comes from alignment and weight, which is what
    a reader actually follows across a row.

    `rows` is a list of lists of strings, the first being the header. `weights` sizes
    the columns (defaults to equal). `numeric_from` is the first column index to
    right-align, because a column of numbers that is not right-aligned cannot be
    compared down its length -- pass len(header) to align everything left.
    """
    columns = len(rows[0])
    shape = slide.shapes.add_table(len(rows), columns, *box.pptx())
    tbl = shape.table
    _drop_gallery_style(tbl)
    tbl.first_row = True
    tbl.horz_banding = False
    tbl.vert_banding = False

    shares = list(weights or [1] * columns)
    total = float(sum(shares))
    for index, share in enumerate(shares):
        tbl.columns[index].width = Inches(box.w * share / total)

    head_h = (size + 12) / 72
    body_h = (size + 10) / 72
    tbl.rows[0].height = Inches(head_h)
    for row in list(tbl.rows)[1:]:
        row.height = Inches(body_h)

    for r, line in enumerate(rows):
        for c, value in enumerate(line):
            cell = tbl.cell(r, c)
            cell.fill.background()
            _strip_borders(cell)
            cell.margin_left = cell.margin_right = Inches(0.10)
            cell.margin_top = cell.margin_bottom = Inches(0.03)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            frame = cell.text_frame
            frame.word_wrap = True
            para = frame.paragraphs[0]
            para.text = str(value)
            # The header takes its column's alignment, not its own: a left-aligned
            # "差值" over a right-aligned column of numbers sits over nothing.
            para.alignment = PP_ALIGN.RIGHT if c >= numeric_from else PP_ALIGN.LEFT
            for run in para.runs:
                run.font.size = Pt(size)
                run.font.bold = r == 0
                run.font.color.rgb = _rgb(theme["foreground"] if r == 0 or c < numeric_from else theme["foreground"])
                if theme.get("font_family"):
                    run.font.name = theme["font_family"]
                if theme.get("cjk_font_family"):
                    _east_asian(run, theme["cjk_font_family"])

    # One rule under the header, and hairlines between rows. Drawn as shapes because
    # a table's own borders cannot be set per edge through python-pptx.
    _hairline(slide, box, box.y0 + head_h, theme["accent"], 0.022)
    _hairline(slide, box, box.y0 + head_h + body_h * (len(rows) - 1), theme["grid"], 0.012)
    return tbl


def _hairline(slide, box, y, colour, thickness):
    line = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(box.x0), Inches(y), Inches(box.w), Inches(thickness))
    line.fill.solid()
    line.fill.fore_color.rgb = _rgb(colour)
    line.line.fill.background()
    line.shadow.inherit = False
    return line


# What a renderer shrinks a raised or lowered run to, measured off LibreOffice:
# 13pt came back as 7.5pt. Used for the width estimate, since the size written into
# the file is the line's own.
_SUB_RENDERED = 0.58

# The order a:tcPr's children have to be written in (ECMA-376, CT_TableCellProperties).
# The four edges come first and in this order, then the fill, and an XML file that
# writes them in any other order is not the format -- see _strip_borders.
_EDGES = ("lnL", "lnR", "lnT", "lnB")

# The table style python-pptx stamps on every table it creates: "Medium Style 2 -
# Accent 1", a blue banded thing from the Office gallery. Recognised by value so a
# table cloned out of a template keeps the style its designer chose.
_PPTX_DEFAULT_STYLE = "{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}"


def _strip_borders(cell):
    """Remove the outline Office puts on every cell.

    python-pptx has no border API, so the four edge elements are written into the
    cell's properties as explicit noFill: absent would mean "inherit", and what it
    would inherit is the gallery style below.

    The order matters and getting it wrong is invisible in this renderer, which is
    how it shipped: `insert(0)` per edge left `lnB, lnT, lnR, lnL` -- exactly
    reversed -- in every cell of four delivered decks. LibreOffice does not check
    the sequence, so every render and every measurement said the tables were clean;
    PowerPoint does check it, and drops the cell properties it cannot parse, so the
    reader saw the blue gallery table underneath. Nothing in a pipeline that judges
    decks by rendering them can catch that, so the order is asserted in a test.
    """
    properties = cell._tc.get_or_add_tcPr()  # noqa: SLF001 -- no API for cell borders
    for index, edge in enumerate(_EDGES):
        for existing in properties.findall(f"{{{_A}}}{edge}"):
            properties.remove(existing)
        element = properties.makeelement(f"{{{_A}}}{edge}", {})
        element.append(element.makeelement(f"{{{_A}}}noFill", {}))
        properties.insert(index, element)


def _drop_gallery_style(tbl):
    """Take off the Office gallery style, keeping a template's own.

    Every cell here is styled explicitly, so the style underneath should be
    unreachable -- and it is not: a table style also carries the header's font, its
    borders and its band fills, and PowerPoint applies each of those wherever the
    cell itself is silent. Leaving it on means the deck's tables look one way here
    and another way in the room.
    """
    for element in tbl._tbl.iter(f"{{{_A}}}tableStyleId"):  # noqa: SLF001 -- no API for the table style
        if (element.text or "").strip().upper() == _PPTX_DEFAULT_STYLE:
            element.getparent().remove(element)
            break


def points(slide, box, theme, items, *, size=BODY_PT, numbered=False, mark="\u2022", colour=None,
           mark_colour=None, spacing=1.25):
    """Parallel points, each with a mark and a hanging indent.

    Two claims stacked as bare paragraphs read as one paragraph that happens to have
    a line break in it: a delivered page put two 40-character sentences under a rule
    with nothing in front of either, and a reader has to work out that they are two
    things. A mark in the margin is what says "these are a set" -- and the hanging
    indent is what keeps the second line of a point aligned with its first rather
    than with the mark.

    Written as a real bullet (`a:buChar` or `a:buAutoNum` with marL/indent) rather
    than by prefixing the string, so the wrap is the renderer's problem and the list
    stays a list when someone edits the deck. `numbered=True` counts them.
    """
    left, top, width, height = box.pptx()
    frame = slide.shapes.add_textbox(left, top, width, height).text_frame
    frame.word_wrap = True
    frame.auto_size = MSO_AUTO_SIZE.NONE
    frame.margin_left = frame.margin_right = Inches(0.04)
    frame.margin_top = frame.margin_bottom = Inches(0.02)
    lines = items if isinstance(items, (list, tuple)) else [items]
    for index, line in enumerate(lines):
        para = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        para.text = str(line)
        para.line_spacing = spacing
        if index:
            para.space_before = Pt(size * 0.45)
        _bullet(para, size, numbered=numbered, mark=mark, colour=mark_colour or theme["accent"])
        for run in para.runs:
            run.font.size = Pt(size)
            run.font.color.rgb = _rgb(colour or theme["foreground"])
            if theme.get("font_family"):
                run.font.name = theme["font_family"]
            if theme.get("cjk_font_family"):
                _east_asian(run, theme["cjk_font_family"])
    return frame


def _bullet(para, size, *, numbered=False, mark="\u2022", colour="#000000"):
    """The paragraph's mark and hanging indent, which python-pptx has no API for.

    a:pPr fixes the order of its children (ECMA-376): the bullet colour, then its
    size, then its font, then the bullet itself. Written in that order because
    PowerPoint drops what it cannot parse -- the same trap the table borders fell
    into.
    """
    properties = para._pPr if para._pPr is not None else para._p.get_or_add_pPr()  # noqa: SLF001
    hang = int(size * 1.5 / 72 * 914400)
    properties.set("marL", str(hang))
    properties.set("indent", str(-hang))
    for tag in ("buClrTx", "buClr", "buSzTx", "buSzPct", "buSzPts", "buFontTx", "buFont", "buNone",
                "buAutoNum", "buChar"):
        for existing in properties.findall(f"{{{_A}}}{tag}"):
            properties.remove(existing)
    fill = properties.makeelement(f"{{{_A}}}buClr", {})
    solid = fill.makeelement(f"{{{_A}}}solidFill", {})
    value = solid.makeelement(f"{{{_A}}}srgbClr", {"val": str(_rgb(colour))})
    solid.append(value)
    fill.append(solid)
    properties.append(fill)
    font = properties.makeelement(f"{{{_A}}}buFont", {"typeface": "Arial"})
    properties.append(font)
    if numbered:
        properties.append(properties.makeelement(f"{{{_A}}}buAutoNum", {"type": "arabicPeriod"}))
    else:
        properties.append(properties.makeelement(f"{{{_A}}}buChar", {"char": mark}))

def heading(slide, frame, theme, title, kicker=None, *, tint="surface", underline=True, bleed=True):
    """The title row with a ground under it, which is what gives a page a top edge.

    A title floating on the same white as the body leaves the page without one, and
    a deck of those reads as a document someone is reading out. A quiet band behind
    the title row gives every page the same anchor -- and because the band carries
    the title, it is grouping rather than decoration: the band gate refuses a filled
    bar only when nothing sits on it.

    `bleed` runs the band the full width of the canvas, which reads as design; off,
    it stops just outside the safe area, which reads as a box. Returns the band, so
    a page that wants something at its right edge knows where the row ends.
    """
    top = 0.0 if bleed else max(0.0, frame.kicker.y0 - 0.14)
    # Stops above the body rather than a fixed distance under the title: the first
    # thing on the page starts at frame.body.y0, and a band computed from the title
    # instead reached 0.04in past it -- so every card in the top row of a page came
    # out with its corner inside the heading's ground.
    bottom = frame.body.y0 - _HEADING_AIR
    x0, x1 = (0.0, CANVAS_W) if bleed else (MARGIN - 0.26, CANVAS_W - MARGIN + 0.26)
    band = Box(x0, top, x1, bottom)
    plane(slide, band, theme, tint=tint)
    face, han = theme.get("font_family"), theme.get("cjk_font_family")
    if kicker:
        write(
            slide,
            frame.kicker,
            kicker,
            size=KICKER_PT,
            colour=theme.get("muted", theme["foreground"]),
            font=face,
            cjk_font=han,
        )
    write(
        slide,
        frame.title,
        title,
        size=TITLE_PT,
        bold=True,
        colour=theme["foreground"],
        font=face,
        cjk_font=han,
        anchor="middle",
    )
    if underline:
        rule(slide, frame.title, theme)
    return band

def formula(slide, box, text, theme, *, size=BODY_PT, align="left", anchor="top"):
    """An expression, set as one unbreakable line with real subscripts.

    A formula written with `write` is prose to a text box: it wraps wherever the box
    runs out, and where it runs out is the middle of a symbol. Measured on a
    delivered page, in a 3.9in column: "分类 logits = (Q'inst, concat(Q'sem, Q'bg))"
    came out broken after "Q" with "'bg))" alone on the next line, and every
    subscript in it was flat -- F4 for F-sub-4, Qinst for Q-sub-inst -- so the one
    thing the notation was carrying was gone.

    So: wrapping off, because a line that overflows is a measurement the gates
    report and a line broken mid-symbol is a page nobody can read. The size steps
    down from `size` until the line fits the box, and if the floor is reached first
    the expression is split at its own top-level separators (semicolons) rather than
    anywhere. `_x` and `_{xyz}` subscript, `^x` and `^{xyz}` superscript, a lone
    latin letter is italic the way a variable is, and a word of two or more letters
    is upright the way `concat` and `softmax` are.
    """
    lines = list(text) if isinstance(text, (list, tuple)) else [text]
    chosen, lines = _formula_size(lines, box.w, size)
    left, top, width, height = box.pptx()
    frame = slide.shapes.add_textbox(left, top, width, height).text_frame
    frame.word_wrap = False
    frame.auto_size = MSO_AUTO_SIZE.NONE
    frame.margin_left = frame.margin_right = Inches(0.04)
    frame.margin_top = frame.margin_bottom = Inches(0.02)
    frame.vertical_anchor = _named(_ANCHORS, anchor, "anchor")
    for index, line in enumerate(lines):
        para = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        para.alignment = _named(_ALIGNS, align, "align")
        para.line_spacing = 1.25
        for body, level in _pieces(str(line)):
            for token, italic in _words(body):
                run = para.add_run()
                run.text = token
                # One size for every run, including the sub- and superscripts: the
                # renderer shrinks a raised or lowered run on its own, and shrinking
                # it here as well multiplies the two. Measured on a rendered deck: an
                # 18pt formula asked for 13pt subscripts and got 7.5pt, four tenths
                # of the line it sat on and unreadable at projector distance.
                run.font.size = Pt(chosen)
                run.font.italic = italic
                run.font.color.rgb = _rgb(theme["foreground"])
                if theme.get("font_family"):
                    run.font.name = theme["font_family"]
                if theme.get("cjk_font_family"):
                    _east_asian(run, theme["cjk_font_family"])
                if level:
                    _baseline(run, level)
    return frame


def card(slide, box, theme, *, icon=None, title="", body=(), tint="surface", size=LABEL_PT, title_size=BODY_PT):
    """A titled card: the surface, an icon, the title beside it, the copy under it.

    What a row of parallel points should be drawn as, and the reason this exists
    rather than being described: three live decks drew the surface and the copy by
    hand and not one of them put an icon on a card, so 180 icons shipped unused
    while the cards' titles sat in a column of identical bold lines. An argument
    that is easier to write with the icon than without it gets the icon.

    `icon` is a name from `ICON_NAMES` in ppt_icons -- `find_icons("compare")` finds
    one. Everything else is the geometry, which is this function's business: the
    icon's square, the gap after it, the title's line, and the copy filling what is
    left inside the padding.
    """
    plane(slide, box, theme, tint=tint, radius=True)
    x = box.x0 + PAD
    top = box.y0 + PAD
    head = 0.36
    if icon:
        side = 0.30
        try:
            from ppt_icons import add_icon
        except ImportError:
            add_icon = None
        if add_icon is not None:
            add_icon(slide, icon, Inches(x), Inches(top + (head - side) / 2), Inches(side), theme["accent"])
            x += side + 0.14
    if title:
        write(
            slide,
            Box(x, top, box.x1 - PAD, top + head),
            title,
            size=title_size,
            bold=True,
            colour=theme["foreground"],
            font=theme.get("font_family"),
            cjk_font=theme.get("cjk_font_family"),
            anchor="middle",
        )
        top += head + 0.10
    if body:
        write(
            slide,
            Box(box.x0 + PAD, top, box.x1 - PAD, box.y1 - PAD),
            body,
            size=size,
            colour=theme.get("muted", theme["foreground"]),
            font=theme.get("font_family"),
            cjk_font=theme.get("cjk_font_family"),
        )
    return box


# Character widths in ems, by class, for the faces these decks use. Not a font
# metric: a formula is one line and the only question is whether it fits, so the
# class average decides the size and the render-side measurement catches the rest.
_EMS = {"cjk": 1.02, "cap": 0.64, "low": 0.52, "digit": 0.56, "space": 0.28, "thin": 0.32, "other": 0.66}
_THIN = ".,;:!|'()[]{}ilt"


def _em_width(text, size):
    total = 0.0
    for character in text:
        if ord(character) > 0x2E80:
            total += _EMS["cjk"]
        elif character.isspace():
            total += _EMS["space"]
        elif character.isdigit():
            total += _EMS["digit"]
        elif character in _THIN:
            total += _EMS["thin"]
        elif character.isupper():
            total += _EMS["cap"]
        elif character.islower():
            total += _EMS["low"]
        else:
            total += _EMS["other"]
    return total * size / 72.0


def _formula_width(line, size):
    total = 0.0
    for body, level in _pieces(str(line)):
        total += _em_width(body, size if level == 0 else size * _SUB_RENDERED)
    return total


def _formula_size(lines, width, size):
    """The largest size in the ramp that fits, and the lines to set at it."""
    room = max(0.5, width - 0.12)
    chosen = size
    while chosen > BODY_FLOOR_PT and max(_formula_width(line, chosen) for line in lines) > room:
        chosen -= 1
    if max(_formula_width(line, chosen) for line in lines) <= room:
        return chosen, lines
    # Still over at the floor: break at the expression's own separators, which is
    # the one place a break does not land inside a symbol.
    split = []
    for line in lines:
        split.extend(_clauses(str(line)))
    if len(split) > len(lines):
        return _formula_size(split, width, size)
    return chosen, lines


def _clauses(text):
    parts = []
    current = ""
    for character in text:
        current += character
        if character in "；;":
            parts.append(current.strip())
            current = ""
    if current.strip():
        parts.append(current.strip())
    return parts or [text]


def _pieces(text):
    """(text, level) runs, where level is 0 for the baseline, -1 sub, +1 super."""
    out = []
    plain = []
    index = 0
    while index < len(text):
        character = text[index]
        if character in "_^" and index + 1 < len(text):
            level = -1 if character == "_" else 1
            index += 1
            if text[index] == "{":
                end = text.find("}", index)
                end = len(text) if end < 0 else end
                token = text[index + 1 : end]
                index = end + 1
            else:
                token = ""
                while index < len(text) and (text[index].isalnum() or text[index] == "'"):
                    token += text[index]
                    index += 1
            if plain:
                out.append(("".join(plain), 0))
                plain = []
            if token:
                out.append((token, level))
            continue
        plain.append(character)
        index += 1
    if plain:
        out.append(("".join(plain), 0))
    return out or [("", 0)]


def _words(text):
    """(token, italic) runs: a lone latin letter is a variable, a word is a name."""
    out = []
    current = ""
    for character in text:
        if character.isascii() and character.isalpha():
            current += character
            continue
        if current:
            out.append((current, len(current) == 1))
            current = ""
        out.append((character, False))
    if current:
        out.append((current, len(current) == 1))
    merged = []
    for token, italic in out:
        if merged and merged[-1][1] == italic:
            merged[-1] = (merged[-1][0] + token, italic)
        else:
            merged.append((token, italic))
    return merged or [(text, False)]


def _baseline(run, level):
    """Raise or lower the run, which python-pptx has no property for."""
    run.font._rPr.set("baseline", "30000" if level > 0 else "-25000")  # noqa: SLF001

def overlaps(boxes, tolerance=0.01):
    """Which pairs of these boxes overlap -- an assertion an author can make cheaply.

    Returns a list of (i, j). Regions that came out of the same division never
    overlap; this is for pages that mix a division with a box placed by hand.
    """
    hits = []
    for i, first in enumerate(boxes):
        for j in range(i + 1, len(boxes)):
            second = boxes[j]
            if (
                first.x0 < second.x1 - tolerance
                and second.x0 < first.x1 - tolerance
                and first.y0 < second.y1 - tolerance
                and second.y0 < first.y1 - tolerance
            ):
                hits.append((i, j))
    return hits


_ALIGNS = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER, "right": PP_ALIGN.RIGHT}
_ANCHORS = {"top": MSO_ANCHOR.TOP, "middle": MSO_ANCHOR.MIDDLE, "bottom": MSO_ANCHOR.BOTTOM}


def _named(table, value, what):
    """A name from this table, or the python-pptx enum a caller passed instead.

    `write(..., align="right")` is what these helpers take and `align=PP_ALIGN.RIGHT` is
    what somebody writing python-pptx reaches for. One design pass wrote the second,
    the lookup raised `KeyError: <PP_PARAGRAPH_ALIGNMENT.RIGHT: 3>`, the round was
    reverted and the deck lost the whole pass over a spelling.
    """
    if isinstance(value, str):
        try:
            return table[value.lower()]
        except KeyError:
            raise ValueError(f"{what} is one of {', '.join(table)}, not {value!r}") from None
    if value in set(table.values()):
        return value
    raise ValueError(f"{what} is one of {', '.join(table)}, not {value!r}")


def _rgb(value):
    if isinstance(value, RGBColor):
        return value
    text = str(value).lstrip("#")
    return RGBColor(int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
'''


def layout_module_source() -> str:
    """Source of the `ppt_layout` module a build script imports."""
    return _LAYOUT_MODULE
