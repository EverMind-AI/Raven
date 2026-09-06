"""What a picture on the page actually shows, against what it was drawn from.

The one family of defect no gate in this repo could see. Forty-three checks measure
the built deck and not one of them compares a picture frame's proportions with the
proportions of the image inside it, or asks whether a figure is large enough to read.
The single ratio test that exists -- `template.compose.FIT_RATIO_LIMIT` -- guards one
route, the template's own `replace_picture`, and raises there; an author who writes
`slide.shapes.add_picture(path, left, top, width=w, height=h)` states both extents
and nothing looks at the result. `replace_picture`'s own docstring concedes that
`fit="stretch"` "distorts to fill, which is visible in any screenshot with type in
it", and no measurement backs that sentence up.

The evidence that the hole is real is that a model closed it by hand: in one live run
it shelled out to python-pptx mid-build to print `region aspect: 1.253 / box aspect:
1.253` for itself. A check the author has to write is a check that is missing.

**A crop is not a distortion.** A .pptx picture carries a source rectangle
(`a:srcRect`) naming the part of the image that is shown, and a stretch fill carries
its own inset (`a:stretch/a:fillRect`) naming the part of the frame the source is
mapped onto -- negative insets are how a designer expresses "cover". Reading only
`shape.image.size` reports both as distortion. On the calibration set that is not a
corner case, it is a third of the reading: across 250 pictures in the twelve bundled
templates and eight built decks, comparing raw pixels against the frame reports 81
distortions, the worst of them 4.26x, and every one is false. Reading `a:srcRect` and
stopping there still reports 37. Reading both reports none. One delivered deck is the
whole argument in miniature: five photographs, each cropped 8% to 37% off two sides,
and the naive reading calls all five distorted.

Everything here is a WARNING. The picture families the predecessor refused were
refused on aesthetic judgement and had to be walked back three times (design doc D17);
a frame's proportions are the author's decision to make, and a check that can be
answered by deleting the figure is worse than one that is read and ignored.
"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from raven_ppt.contracts.findings import Finding, Severity
from raven_ppt.services.measure.bands import band_of
from raven_ppt.services.measure.geometry import Rect, iter_shapes, open_deck, page_box, picture_blob

if TYPE_CHECKING:
    from raven_ppt.contracts.masters import Bands

_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_P = "http://schemas.openxmlformats.org/presentationml/2006/main"

# How far a frame's proportions may sit from the proportions of what it shows before
# the stretch is the thing a reader notices. Measured over 250 pictures in the twelve
# bundled templates plus eight built decks: once `a:srcRect` and `a:stretch/a:fillRect`
# are read, 245 land within 1% of correct and the five that do not are 1.02, 1.03,
# 1.08, 1.15 and 1.16 -- mild stretches a designer shipped on purpose. 1.20 clears every
# one of them, while the failure this is for is far past it: a 16:9 figure given a 4:3
# box is 1.33x, given a square box 1.78x.
DISTORTION_LIMIT = 1.20
# The least of an image a frame may show before the crop is the picture: a 16:9
# generation cover-fitted into an 11.9x1.9in band kept 28% of itself, and what the
# reader saw was a strip of sky. Half is generous -- a 3:2 photograph in a 16:9 frame
# keeps 84% -- so a frame under this is one asking for a picture of another shape.
CROP_LIMIT = 0.5

# The three roles a picture can take, graded by the share of the content band it
# occupies. A hero carries the page's argument; a mark is a badge, an icon, an avatar
# chip; a support figure is everything between.
HERO_BODY_SHARE = 0.40

# The mark ceiling is 5% of the content band and not the 64x64px the design rubric
# names, because the rubric's number was never measured against a real template. The
# bundled twelve put their icon chips at 1.15x1.15in, 1.32x1.30in and 1.38x1.38in --
# every one unmistakably a badge, every one six to ten times a 64px square. Grading
# those as support figures reported 93 of the calibration set's 250 as undersized,
# which is the D20 failure exactly: a check that fires on the artifact rather than on
# the defect. At 5% the set reports none, and what still trips the floor below is a
# picture with a figure's area squeezed into a sliver -- 8.0x0.5in, 1.5x3.0in.
MARK_BODY_SHARE = 0.05

# What a support figure needs to be read at all, stated in the pixels the rubric states
# it in: 280x180 on the 1920x1080 grid a 13.33in canvas renders to, so 144px to the
# inch. The pixel grid rather than 96dpi print because a deck is projected -- at 96 the
# floor is 2.92x1.88in, which is over an eighth of the content band and reports a
# delivered deck's full-width strip by a hundredth of an inch.
SCREEN_DPI = 144
SUPPORT_MIN_IN = (280 / SCREEN_DPI, 180 / SCREEN_DPI)
SUPPORT_MIN_AREA_IN = SUPPORT_MIN_IN[0] * SUPPORT_MIN_IN[1]

# An image on this many pages is the deck's own furniture -- a logo, a masthead --
# rather than one page's decoration.
FIXTURE_PAGES = 3
# How far the same mark may move between pages before the drift is visible. Cloned
# pages carry identical EMU, so a mark that is placed once does not move at all; this
# is not calibrated against a real deck because no deck in the calibration set repeats
# a mark on three pages, and it is set at the smallest offset a reader could see rather
# than at a measured line.
FIXTURE_SLACK_IN = 0.1

# How near the trim a picture has to sit to read as a page device rather than as
# something placed beside the heading. Exactly one picture of the calibration set's 250
# reaches the title-band check, and rendering that page settles what it is: the
# lattice-window ornament on `red_chinese_traditional_culture` page 6, drawn flush to
# the top and right trim (x1 == the canvas width to the EMU, y0 == 0) with its pair at
# the opposite corner. Bleeding off the page is what makes it a corner device; the
# decoration this check is for -- a small graphic dropped to the right of the title
# block -- sits inside the margins. A fiftieth of an inch is under any inset a layout
# would state on purpose.
TRIM_SLACK_IN = 0.02


@dataclass(frozen=True)
class Figure:
    """One picture on one page: its frame, its source, and the part of it shown.

    Three rectangles, because no two of them are the same thing. `box` is the frame on
    the page. `crop` is the part of the source image the frame shows, as fractions
    trimmed off each side. `inset` is the part of the frame the source is mapped into
    -- negative fractions mean the image runs outside the frame and is clipped, which
    is how a stretch fill spells "cover".
    """

    page: int
    name: str
    box: Rect
    digest: str
    pixels: tuple[int, int] | None
    crop: tuple[float, float, float, float]
    inset: tuple[float, float, float, float]
    tiled: bool

    @property
    def source_aspect(self) -> float | None:
        """Wide-to-tall of the part of the image that is displayed, or None.

        None when the pixels could not be read -- an EMF or WMF, which python-pptx
        cannot size and which this refuses to guess at.
        """
        if self.pixels is None:
            return None
        width = self.pixels[0] * (1 - self.crop[0] - self.crop[1])
        height = self.pixels[1] * (1 - self.crop[2] - self.crop[3])
        return width / height if width > 0 and height > 0 else None

    @property
    def display_box(self) -> Rect:
        """Where the image lands, which is the frame only when the fill states no inset."""
        left, right, top, bottom = self.inset
        width = self.box.width * (1 - left - right)
        height = self.box.height * (1 - top - bottom)
        x0 = self.box.x0 + self.box.width * left
        y0 = self.box.y0 + self.box.height * top
        return Rect(x0, y0, x0 + width, y0 + height)

    @property
    def shown_share(self) -> float:
        """How much of the source image the frame shows, 1.0 being all of it.

        Two spellings of a crop, read the way `fitted_box` reads them. `srcRect`
        trims the source; a negative `fillRect` maps the source onto a box larger
        than the frame, of which the frame is the window -- so the frame's area over
        the display box's is the share of the (trimmed) source that is seen. When
        the file states both, `fitted_box` has already decided which one the page
        shows, and only that statement's share is counted, since counting both
        multiplies a fit stated twice into a loss that is not on the page.
        """
        left, right, top, bottom = self.crop
        trimmed = max(0.0, 1 - left - right) * max(0.0, 1 - top - bottom)
        display = self.display_box
        if display.width <= 0 or display.height <= 0:
            return trimmed
        window = min(1.0, (self.box.width * self.box.height) / (display.width * display.height))
        if any(self.crop) and any(self.inset):
            return trimmed if self.fitted_box == self.box else window
        return trimmed if any(self.crop) else window

    @property
    def display_aspect(self) -> float | None:
        box = self.display_box
        return box.width / box.height if box.width > 0 and box.height > 0 else None

    @property
    def fitted_box(self) -> Rect:
        """The rectangle to judge the source against, of the readings the fill allows.

        Both statements, or there is nothing to choose between: a fill that states its
        fit once has one reading and is judged on it. A `fillRect` alone is the whole
        correction the renderer applies, and letting the frame stand in for it hid a
        picture stretched 1.5x across every visible pixel.

        A fill states its fit once, but a file can carry two statements of it. A
        template photograph is fitted by its designer with a negative `fillRect`;
        `compose._fill_crop` then replaces the image and states the same fit again as
        an `srcRect` against the frame. Read as one composition -- crop the source,
        then cover-fit the crop into the inset box -- the two corrections multiply into
        a stretch that is not on the page: measured on three template photographs, the
        rendered pixels match the crop at 1.00x and get monotonically worse out to the
        1.64x the pair reported. So the kindest reading wins. A picture the file fits
        correctly under any statement it makes is a picture the page shows correctly,
        and a fill with one statement still has only one reading to be judged on.
        """
        shown = self.source_aspect
        if shown is None or not (any(self.crop) and any(self.inset)):
            return self.display_box
        return min((self.box, self.display_box), key=lambda box: _off_by(box, shown))

    @property
    def distortion(self) -> float | None:
        """How many times off the displayed proportions are, 1.0 being exact.

        None for a tiled fill, which repeats the image at its own size rather than
        stretching one copy, and for anything whose pixels could not be read.
        """
        shown = self.source_aspect
        if self.tiled or shown is None:
            return None
        return _off_by(self.fitted_box, shown)

    def role(self, bands: Bands) -> str:
        """Which of the three sizes this is, by the share of the content band it takes."""
        share = self.body_share(bands)
        if share >= HERO_BODY_SHARE:
            return "hero"
        return "mark" if share < MARK_BODY_SHARE else "support"

    def body_share(self, bands: Bands) -> float:
        area = bands.body_area
        return self.box.area / area if area > 0 else 0.0


def figure_findings(pptx_path: Path, bands: Bands | None = None) -> list[Finding]:
    """Every figure defect in one deck.

    `bands` is the deck's own title/content/footer grid. Without it the two families
    that need a denominator -- what share of the content band a picture takes, and
    which band it sits in -- return nothing rather than falling back on a hardcoded
    canvas, per the masters contract. Distortion has no such dependency: it compares a
    frame with its own image and is reported either way.
    """
    figures = read_figures(pptx_path)
    return [
        *distorted_figures(figures),
        *cropped_figures(figures),
        *figure_roles(figures, bands),
        *title_band_figures(figures, bands),
    ]


def read_figures(pptx_path: Path) -> list[Figure]:
    """Every picture in a built deck, however the file spells it.

    Through `picture_blob` rather than `shape.image`, because a template's photograph
    is as often a rounded rectangle with a `blipFill` as it is a picture frame, and
    `shape.image` raises on the first kind.
    """
    try:
        deck = open_deck(pptx_path)
    except Exception:  # noqa: BLE001 -- an unreadable deck is not a measurement
        return []
    found: list[Figure] = []
    for number, slide in enumerate(deck.slides, start=1):
        for shape in iter_shapes(slide.shapes):
            blob = picture_blob(shape)
            if blob is None:
                continue
            box = page_box(shape)
            if box is None or box.width <= 0 or box.height <= 0:
                continue
            fill = _blip_fill(shape)
            found.append(
                Figure(
                    page=number,
                    name=str(getattr(shape, "name", "") or "picture"),
                    box=box,
                    digest=hashlib.sha1(blob, usedforsecurity=False).hexdigest(),
                    pixels=_pixels(blob),
                    crop=_source_rect(fill),
                    inset=_fill_rect(fill),
                    tiled=fill is not None and fill.find(f"{{{_A}}}tile") is not None,
                )
            )
    return found


def cropped_figures(figures: list[Figure]) -> list[Finding]:
    """One finding per picture whose frame shows less than CROP_LIMIT of its image."""
    found = []
    for figure in figures:
        if figure.pixels is None or figure.tiled:
            continue
        shown = figure.shown_share
        if shown >= CROP_LIMIT or shown <= 0:
            continue
        frame = figure.box
        found.append(
            Finding(
                kind="figure_crop",
                severity=Severity.WARNING,
                page=figure.page,
                message=(
                    f"'{figure.name}' shows {shown:.0%} of its {figure.pixels[0]}x{figure.pixels[1]}px image: "
                    f"a {frame.width:.2f}x{frame.height:.2f}in frame ({frame.width / frame.height:.2f} wide-to-tall) "
                    f"cover-fitted a {figure.pixels[0] / figure.pixels[1]:.2f} wide-to-tall source and cropped the "
                    f"rest away, so what is left may not be the subject. Generate or choose a picture at the "
                    f"frame's own shape (aspect_ratio nearest {frame.width / frame.height:.2f}), or give the picture "
                    f"a frame nearer its shape"
                ),
                detail={
                    "shown_share": round(shown, 3),
                    "frame_aspect": round(frame.width / frame.height, 3),
                    "source_aspect": round(figure.pixels[0] / figure.pixels[1], 3),
                    "pixels": list(figure.pixels),
                },
            )
        )
    return found


def distorted_figures(figures: list[Figure]) -> list[Finding]:
    """One finding per picture whose frame does not hold the proportions of its image."""
    found = []
    for figure in figures:
        off = figure.distortion
        if off is None or off <= DISTORTION_LIMIT:
            continue
        shown = figure.source_aspect or 1.0
        drawn = figure.fitted_box
        fitted = drawn.width / shown
        pixels = f"{figure.pixels[0]}x{figure.pixels[1]}px" if figure.pixels else "its source"
        cropped = " after its crop" if any(figure.crop) else ""
        found.append(
            Finding(
                kind="figure_distortion",
                severity=Severity.WARNING,
                page=figure.page,
                message=(
                    f"'{figure.name}' is drawn {drawn.width:.2f}x{drawn.height:.2f}in "
                    f"({drawn.width / drawn.height:.2f} wide-to-tall) from {pixels} that is "
                    f"{shown:.2f} wide-to-tall{cropped} -- {off:.2f}x apart, so everything in it is "
                    f"stretched, type in a screenshot most visibly. At {drawn.width:.2f}in wide the "
                    f"frame it needs is {fitted:.2f}in tall: give add_picture the width and let the "
                    f"height follow, or keep the box and crop the source to it "
                    f"(pictures={{n: path}} on a template frame crops rather than stretches)"
                ),
                detail={
                    "distortion": round(off, 3),
                    "frame_aspect": round(drawn.width / drawn.height, 3),
                    "source_aspect": round(shown, 3),
                    "pixels": list(figure.pixels) if figure.pixels else None,
                    "fitted_height_in": round(fitted, 2),
                },
            )
        )
    return found


def figure_roles(figures: list[Figure], bands: Bands | None) -> list[Finding]:
    """Pictures sized between the roles, and marks that will not stay put.

    Two readings of one grading. A picture with a support figure's area but a sliver's
    width or height is too small to read and too big to be a badge -- there is no size
    that is neither, so the reader is left with an illustration they cannot see. And a
    mark the deck repeats is furniture: a logo that lands somewhere different on each
    page is the tell that the pages were built one at a time.
    """
    if bands is None:
        return []
    found = []
    for figure in figures:
        if figure.role(bands) != "support":
            continue
        width, height = figure.box.width, figure.box.height
        if width >= SUPPORT_MIN_IN[0] and height >= SUPPORT_MIN_IN[1]:
            continue
        short = "wide" if width < SUPPORT_MIN_IN[0] else "tall"
        found.append(
            Finding(
                kind="figure_undersized",
                severity=Severity.WARNING,
                page=figure.page,
                message=(
                    f"'{figure.name}' takes {figure.body_share(bands) * 100:.0f}% of the content band but "
                    f"is only {width:.2f}x{height:.2f}in -- {SUPPORT_MIN_IN[0]:.2f}x{SUPPORT_MIN_IN[1]:.2f}in "
                    f"is what a figure needs to be read, and this one is not {short} enough. It is a sliver: "
                    f"large enough to occupy the page, too thin to show anything. Either give it that box, or "
                    f"take it under {MARK_BODY_SHARE * 100:.0f}% of the band and let it be a badge"
                ),
                detail={
                    "box_in": [round(width, 2), round(height, 2)],
                    "body_share": round(figure.body_share(bands), 3),
                    "minimum_in": list(SUPPORT_MIN_IN),
                },
            )
        )
    return found + _drifting_marks(figures, bands)


def title_band_figures(figures: list[Figure], bands: Bands | None) -> list[Finding]:
    """Pictures small enough to be decoration, sitting in the deck's heading row.

    The title band is where the page says what it is about. A picture in it that is too
    small to carry a figure is filling space beside the heading, and a reader takes that
    as the page having nothing to show.

    Three exemptions. A picture drawn to the trim is a corner device rather than filler
    -- that one came from rendering the page and looking at it, and it is the only
    picture of the calibration set's 250 the rest of this check reaches. Anything the
    deck repeats across three or more pages is furniture: a masthead is not one page's
    filler, and one that wanders is already reported once, by `figure_mark_drift` --
    without the exemption a drifting logo comes back as four findings for one problem.
    And a picture whose bottom edge is past the band is not sitting in it; that one
    excludes nothing on the calibration set, because `Bands.where` already wants most of
    a shape inside a band before it says so, and it is stated here because "in the
    heading row" is this check's own claim and should not change meaning quietly when
    that threshold moves.
    """
    if bands is None:
        return []
    fixed = _repeated(figures)
    found = []
    for figure in figures:
        if figure.digest in fixed or figure.box.area >= SUPPORT_MIN_AREA_IN:
            continue
        if band_of(bands, figure.box.y0, figure.box.y1) != "title":
            continue
        if figure.box.y1 > bands.title_bottom or _bleeds(figure.box, bands):
            continue
        found.append(
            Finding(
                kind="title_band_figure",
                severity=Severity.WARNING,
                page=figure.page,
                message=(
                    f"'{figure.name}' is a {figure.box.width:.2f}x{figure.box.height:.2f}in picture inside the "
                    f"title band ({bands.title_top:.2f}-{bands.title_bottom:.2f}in), which is the page's "
                    f"heading row. A picture that small there is decoration beside the title rather than "
                    f"evidence under it. Move it into the content band "
                    f"({bands.title_bottom:.2f}-{bands.body_bottom:.2f}in) and give it at least "
                    f"{SUPPORT_MIN_IN[0]:.2f}x{SUPPORT_MIN_IN[1]:.2f}in, or drop it and let the heading have "
                    f"the row"
                ),
                detail={
                    "box_in": [round(figure.box.x0, 2), round(figure.box.y0, 2)],
                    "size_in": [round(figure.box.width, 2), round(figure.box.height, 2)],
                    "title_band": [round(bands.title_top, 2), round(bands.title_bottom, 2)],
                },
            )
        )
    return found


def _drifting_marks(figures: list[Figure], bands: Bands) -> list[Finding]:
    """One finding per repeated mark that lands somewhere different from page to page."""
    places: dict[str, list[Figure]] = defaultdict(list)
    for figure in figures:
        if figure.role(bands) == "mark":
            places[figure.digest].append(figure)
    found = []
    for digest, marks in sorted(places.items()):
        pages = sorted({mark.page for mark in marks})
        if len(pages) < FIXTURE_PAGES:
            continue
        spread_x = max(mark.box.x0 for mark in marks) - min(mark.box.x0 for mark in marks)
        spread_y = max(mark.box.y0 for mark in marks) - min(mark.box.y0 for mark in marks)
        if max(spread_x, spread_y) <= FIXTURE_SLACK_IN:
            continue
        corners = sorted({(round(mark.box.x0, 2), round(mark.box.y0, 2)) for mark in marks})
        found.append(
            Finding(
                kind="figure_mark_drift",
                severity=Severity.WARNING,
                message=(
                    f"the same {marks[0].box.width:.2f}x{marks[0].box.height:.2f}in mark sits at "
                    f"{len(corners)} different places across pages "
                    f"{', '.join(str(page) for page in pages)} -- it moves {spread_x:.2f}in across and "
                    f"{spread_y:.2f}in down. A mark the deck repeats is furniture, and a reader paging "
                    f"through sees it jump. Put it at one (left, top) on every page that carries it"
                ),
                detail={
                    "digest": digest[:16],
                    "pages": pages,
                    "corners": [list(corner) for corner in corners],
                    "spread_in": [round(spread_x, 2), round(spread_y, 2)],
                },
            )
        )
    return found


def _bleeds(box: Rect, bands: Bands) -> bool:
    """Whether this picture runs to the trim, which makes it a page device."""
    return (
        box.x0 <= TRIM_SLACK_IN or box.x1 >= bands.canvas_w - TRIM_SLACK_IN or box.y0 <= bands.title_top + TRIM_SLACK_IN
    )


def _repeated(figures: list[Figure]) -> set[str]:
    """Images the deck carries on several pages -- a logo, a masthead, a page-number badge."""
    places: dict[str, set[int]] = defaultdict(set)
    for figure in figures:
        places[figure.digest].add(figure.page)
    return {digest for digest, pages in places.items() if len(pages) >= FIXTURE_PAGES}


def _off_by(box: Rect, aspect: float) -> float:
    """How many times `box` is off `aspect`, 1.0 being exact and inf for a degenerate box."""
    if box.width <= 0 or box.height <= 0:
        return math.inf
    drawn = box.width / box.height
    return max(drawn / aspect, aspect / drawn)


def _blip_fill(shape: Any) -> Any | None:
    """The `blipFill` this shape shows its image through, whichever spelling it uses.

    `p:pic/p:blipFill` for a picture frame, `p:sp/p:spPr/a:blipFill` for a shape filled
    with one. Only this shape's own, never a descendant's: `.//` finds the picture
    inside a group and reports the group as showing it too.
    """
    element = getattr(shape, "_element", None)
    if element is None:
        return None
    own = getattr(element, "blipFill", None)
    if own is not None:
        return own
    properties = element.find(f"{{{_P}}}spPr")
    return None if properties is None else properties.find(f"{{{_A}}}blipFill")


def _source_rect(fill: Any | None) -> tuple[float, float, float, float]:
    """`a:srcRect` as fractions trimmed off left, right, top, bottom of the image."""
    return _sides(None if fill is None else fill.find(f"{{{_A}}}srcRect"))


def _fill_rect(fill: Any | None) -> tuple[float, float, float, float]:
    """`a:stretch/a:fillRect` as fractions inset from each side of the frame.

    Negative fractions are the ordinary case, not an error: they are how a stretch fill
    says "scale the image past the frame and clip", which is `cover`. Sixteen of the
    calibration set's 218 pictures crop this way and no other, and reading only
    `srcRect` calls every one of them distorted -- up to 2.70x.
    """
    return _sides(None if fill is None else fill.find(f"{{{_A}}}stretch/{{{_A}}}fillRect"))


def _sides(rect: Any | None) -> tuple[float, float, float, float]:
    if rect is None:
        return (0.0, 0.0, 0.0, 0.0)
    values = []
    for side in ("l", "r", "t", "b"):
        try:
            values.append(float(rect.get(side) or 0) / 100000)
        except (TypeError, ValueError):
            values.append(0.0)
    return (values[0], values[1], values[2], values[3])


def _pixels(blob: bytes) -> tuple[int, int] | None:
    """The image's own size, or None for a format python-pptx cannot measure.

    EMF and WMF arrive as vector metafiles with no pixel grid to read, and a check that
    guessed at one would report the guess as a defect.
    """
    from pptx.parts.image import Image

    try:
        size = Image.from_blob(blob).size
    except Exception:  # noqa: BLE001 -- an image whose header will not parse has no size
        return None
    try:
        width, height = int(size[0]), int(size[1])
    except (TypeError, ValueError):
        return None
    return (width, height) if width > 0 and height > 0 else None
