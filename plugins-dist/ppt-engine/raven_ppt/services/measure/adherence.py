"""Whether a deck built inside a template used the template's own pages.

The gap this closes: `house_style` compares theme colour schemes, and a program
that opens the template with `Presentation(os.environ['PPT_TEMPLATE'])` passes it
whatever it then draws. So a deck could ignore every page the template ships,
build all twelve of its own on the emptiest layout, and come back with every check
green -- which is exactly what a live run did, and what the user saw was "this is
not the template I gave you".

Measured by geometry, because geometry is what survives. A cloned page keeps its
prototype's shape positions exactly: the XML is deep-copied, so a rectangle that
sat at 0.72in stays at 0.72in even after its words are replaced. A page drawn by
hand lands nowhere near, because nobody types the template's coordinates.

Calibrated on three cases rather than on a guess, and the middle one is the point:
adapting a page is *meant* to change it, so the question is how much change the
measurement tolerates.

* A page adapted straight from the template's own page 2, 4 or 5: 100%.
* The same page changed hard -- a third of its shapes deleted, two of the rest
  moved by a third of an inch, a figure box added: 86%. Deleting costs nothing,
  because a shape that is gone is not counted against you, and adding costs only
  its own share. So a page can lose most of what it cloned and still be its own.
* The eight pages of a live deck that used the same template as a background
  colour: 2% to 6%, the one exception being its cover at 50%, which was built on
  the template's Title Slide layout and did inherit that page's furniture.

Counted per shape rather than weighted by area, which was measured too and is
worse at telling the cases apart: a full-bleed background rectangle matches the
template's, so the deck that ignored its template scores 12-23% by area.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from raven_ppt.contracts.findings import Finding, Severity
from raven_ppt.services.measure.geometry import (
    EMU_PER_INCH,
    is_icon_sized,
    iter_shapes,
    open_deck,
    page_box,
    picture_blob,
)

# A page counts as adapted when this share of its shapes sit where the template puts
# one. Adapting is *meant* to change things, so the number was measured against a
# page changed hard: cloned, a third of its shapes deleted, two of the rest moved by
# a third of an inch, a figure box added. That page still scores 0.86, because
# deleting costs nothing -- a shape that is gone is not counted against you -- and
# adding costs only its own share. A page may lose most of what it cloned and still
# read as adapted.
FROM_PROTOTYPE = 0.5
# And below this it shares essentially nothing, which is the only case worth naming.
# Between the two, a page kept the template's frame and rebuilt the body inside it:
# a legitimate way to work, so it is counted and not named. The live deck that used
# its template as a background colour scored 0.02 to 0.06 on seven of its eight pages.
SHARES_LITTLE = 0.2
# Below this many shapes a page is a section divider or a quote, and coincidence
# is cheap: two boxes on a title layout can both match by accident.
MIN_SHAPES = 4
# How far a shape may have been nudged and still count as the template's. Cloning is
# exact, so this is for the author who moved a box to make room, and it is small
# enough that two shapes a third of an inch apart do not collapse into one.
TOLERANCE_IN = 0.05
_PLACES = 2


def template_adherence(pptx_path: Path, template: Path | None) -> list[Finding]:
    """One deck-wide finding when a template is bound and its pages went unused."""
    if template is None or not Path(template).is_file():
        return []
    prototypes = _pages(Path(template))
    if not prototypes:
        return []
    everywhere = {box for boxes in prototypes.values() for box in boxes}
    left_over = _placeholders_by_page(pptx_path, Path(template))
    adapted: list[int] = []
    partly: list[int] = []
    unrelated: list[int] = []
    underlay: list[int] = []
    for number, shapes in _pages(pptx_path).items():
        if len(shapes) < MIN_SHAPES:
            continue
        share = sum(1 for box in shapes if _matches(box, everywhere)) / len(shapes)
        if left_over.get(number):
            # Position says this page is the template's; its words say the template's
            # own text boxes were never touched. Both are true at once in exactly one
            # situation, and it is the one a live deck was in: `clone_page` for the
            # background, `add_textbox` for the copy, nothing replaced. The shapes
            # match because they are still the template's, sitting under a second
            # layer -- which is why position alone cannot be the measurement.
            underlay.append(number)
            continue
        bucket = adapted if share >= FROM_PROTOTYPE else partly if share >= SHARES_LITTLE else unrelated
        bucket.append(number)
    findings: list[Finding] = []
    if underlay:
        findings.append(
            Finding(
                kind="template_underlay",
                severity=Severity.BLOCKING,
                message=(
                    f"{'Page' if len(underlay) == 1 else 'Pages'} {', '.join(str(n) for n in underlay)} "
                    f"{'has' if len(underlay) == 1 else 'have'} the template's page cloned underneath and new "
                    f"text boxes laid on top of it: the template's own text is still there, unreplaced, and "
                    f"the copy sits over it. That is using the template as a background. Replace the text in "
                    f"the shapes the template drew -- `adapt(prs, prototype, texts={{...}})` matches a shape by "
                    f"the text it currently holds or by its index, empties the ones you do not name, and keeps "
                    f"every position, size, colour and font the template chose. Adding a box over a page you "
                    f"cloned pays for the clone twice and reads as two designs at once"
                ),
                detail={"underlay": underlay, "adapted": adapted, "partly_adapted": partly, "unrelated": unrelated},
            )
        )
    named = _structural_pages(Path(template))
    if not named:
        return findings
    built = _pages(pptx_path)
    # A page with two boxes on it can match a prototype by coincidence and can miss one
    # for no reason, so a deck of nothing but sparse pages -- a divider, a quote -- is not
    # evidence either way.
    if not any(len(shapes) >= MIN_SHAPES for shapes in built.values()):
        return findings
    from_house = [number for number, shapes in sorted(built.items()) if _from_one_of(shapes, prototypes, named)]
    if from_house:
        return findings
    findings.append(
        Finding(
            kind="template_adherence",
            severity=Severity.WARNING,
            message=(
                "this deck is built in the user's template and none of its pages came from the template's own "
                + ", ".join(f"{role} (page {number})" for role, number in named.items())
                + ". Those are the pages a reader recognises whose deck this is by -- clone them with "
                "`adapt(prs, prototype(tpl, N), texts={...})`. The content pages in between are yours to compose "
                "and nothing here asks them to match the template's own"
            ),
            detail={"structural": named, "adapted": adapted, "partly_adapted": partly, "unrelated": unrelated},
        )
    )
    return findings


def _structural_pages(template: Path) -> dict[str, int]:
    """role -> template page, for the pages a deck is expected to clone.

    This check used to count how many of the deck's pages sat on *any* of the
    template's, and that number is the wrong question now: a content page is composed
    rather than filled, so a deck of eleven pages with four cloned ones is exactly
    right and the old wording called it 4 of 11 and asked for more. What still has to
    hold is the frame -- the cover, the contents, the divider, the closing.
    """
    from raven_ppt.services.template.menu import menu, roles

    return roles(menu(template))


def _from_one_of(shapes: set, prototypes: dict[int, set], named: dict[str, int]) -> bool:
    """Does this page's geometry come from one of the named template pages?"""
    if len(shapes) < MIN_SHAPES:
        return False
    for number in named.values():
        boxes = prototypes.get(number) or set()
        if not boxes:
            continue
        if sum(1 for box in shapes if _matches(box, boxes)) / len(shapes) >= SHARES_LITTLE:
            return True
    return False


# Below this a matching string is a page number, a bullet glyph or a unit, and two
# pages sharing it means nothing. Above it, a text block that is word-for-word the
# template's is the template's -- nobody retypes a placeholder.
PLACEHOLDER_MIN_CHARS = 5


def placeholder_copy(pptx_path: Path, template: Path | None, borrowed: Sequence[Path] = ()) -> list[Finding]:
    """Text blocks a page cloned from the template and never replaced.

    From a live deck that otherwise used its template well: page 4 carried
    "单击此处添加长一点的副标题" at subtitle size, and page 7 carried the same block
    with a figure over most of it, one character showing. `adapt` replaces the texts
    it is given and leaves the rest of the cloned page alone, which is right -- a
    prototype's furniture is why it was cloned -- but a placeholder is not furniture.

    Refused rather than reported. A finished page saying "click here to add a title"
    is not a matter of degree, and no reading of the brief wants it.

    URLs and pure digits are skipped: a template's own watermark and its page numbers
    come through cloning too, and neither is an unfilled slot.

    `borrowed` is every other bundled template the plan took a page from: a page cloned
    out of one of those carries that file's example copy, which the bound template's
    text set does not hold.
    """
    sources = [Path(template)] if template is not None and Path(template).is_file() else []
    sources += [Path(other) for other in borrowed if Path(other).is_file()]
    if not sources:
        return []
    placeholders: set[str] = set().union(*(_texts(source) for source in sources))
    if not placeholders:
        return []
    findings: list[Finding] = []
    for number, texts in _texts_by_page(pptx_path).items():
        for text in sorted(texts & placeholders):
            findings.append(
                Finding(
                    kind="placeholder_copy",
                    severity=Severity.BLOCKING,
                    page=number,
                    message=(
                        f"page {number} still says \u201c{text[:40]}\u201d, which is the template's own "
                        f"placeholder text. Replace it with what this page says, or drop the shape: pass it in "
                        f"`texts=` to adapt(), or name it in `drop=`"
                    ),
                    detail={"text": text[:120]},
                )
            )
    return findings


# Below this share of the page a template's image is decoration -- an icon, a corner
# flourish, a rule -- and deleting it damages the page. Measured over 40 templates'
# 564 embedded images: 74% are under a tenth of the page, 21% are the middle band where
# a stock photograph lives, and 5% are full-bleed backgrounds. Format tells you nothing
# (512 PNG against 52 JPG), so size is the signal.
PICTURE_IS_DECORATION = 0.10
# And above this it is the page's background -- full-bleed artwork the design is partly
# made of, 5% of the 564 images measured. Removing one leaves a white page, so the band
# that gets reported is the middle: big enough to be content, small enough not to be
# the page itself. One live deck repeated a 74%-of-the-page background on two slides,
# which the earlier threshold counted as two placeholders to replace.
PICTURE_IS_BACKGROUND = 0.55


def template_pictures(pptx_path: Path, template: Path | None, borrowed: Sequence[Path] = ()) -> list[Finding]:
    """Pages still showing a photograph the template shipped.

    Measured on two live decks: `replace_picture` was called zero times in both, and
    every picture on one of them was the template's own. A stock photograph of a
    meeting table is not evidence for anything the deck says, and it reads as the
    slide nobody finished.

    Reported rather than refused, and only for images big enough to be content. There
    is no way to tell a template's decorative graphic from its placeholder photograph
    -- both arrive as a PNG -- so the author decides: replace it with a figure, drop
    it, or keep it if it is part of the design.

    `borrowed` is every other bundled template the plan took a page from; a stock
    photograph cloned out of one of those is as much a placeholder as the bound one's.
    """
    sources = [Path(template)] if template is not None and Path(template).is_file() else []
    sources += [Path(other) for other in borrowed if Path(other).is_file()]
    if not sources:
        return []
    known: set[str] = set().union(*(_picture_hashes(source) for source in sources))
    if not known:
        return []
    pages: dict[int, int] = {}
    distinct: set[str] = set()
    for number, digests in _pictures_by_page(pptx_path).items():
        shared = [digest for digest in digests if digest in known]
        if shared:
            pages[number] = len(shared)
            distinct.update(shared)
    if not pages:
        return []
    total = sum(pages.values())
    where = ", ".join(str(number) for number in sorted(pages))
    # Places and images, separately: one of them was the same photograph on three
    # pages, and "10 images" for six of them reads as an error in the count.
    count = f"{total} place(s)" if total == len(distinct) else f"{total} place(s) ({len(distinct)} different image(s))"
    return [
        Finding(
            kind="template_picture",
            severity=Severity.WARNING,
            message=(
                f"the template's own images are still showing in {count} on page(s) {where}. A template's photograph "
                f"is a placeholder: it illustrates nothing this deck says. Replace it with a figure "
                f"(`pictures={{n: FIGURES/'x.png'}}` with `FIGURES = Path(os.environ['PPT_FIGURES_DIR'])`, "
                f"which crops to the frame rather than stretching), drop "
                f"the frame (`drop=[n]`), or keep it if it is part of the design rather than a photograph"
            ),
            detail={"pages": {str(k): v for k, v in sorted(pages.items())}, "distinct": len(distinct)},
        )
    ]


# How far above or below a text block a mark may sit and still be the mark on it. The
# seals on the live page sat 0.3in over their headings; a mark an inch away is in another
# row.
MARK_GAP_IN = 1.0
# A text block at least this share of the page wide is a title or a subtitle band: it runs
# over every unit and is the mark on none of them. Without it every seal on the live page
# read as the mark on the page's subtitle, and four marks on four things counted as one.
BAND_SHARE = 0.5
# How many things on one page have to wear a mark before the marks are read together: two
# is the first number at which "the same mark" or "the template's mark" can mean anything.
MARKED_THINGS = 2


def unit_marks(pptx_path: Path, template: Path | None = None, borrowed: Sequence[Path] = ()) -> list[Finding]:
    """Pages whose marks beside the units do not tell the units apart.

    A mark is a picture no longer than `ICON_MAX_IN` a side that sits within MARK_GAP_IN
    of one text block narrower than a band, over the block's own span. The marks on a
    page are read together: each thing wearing one wants a mark of its own, so the marks
    tell the things apart only when there are as many distinct marks of the author's as
    there are things -- a mark still the template's is about nothing on this page, and one
    mark on two things says nothing about either.

    Calibrated over the eight bundled templates, ten built decks and three delivered ones
    (297 pages): it fires on two pages, both of one delivered deck -- page 4 wore the
    template's three seals over four things, one seal twice; page 18 the same three seals
    over three phases -- and on nothing else, the templates' own pages included, because
    their marks are each their own. Asked as "one image three times" it fired nowhere:
    the seals are three images, and the fourth unit borrowed one.

    `template_pictures` does not see these: a mark is a fortieth of the page, below the
    band that check reads as content, and lowering the band would sweep in the corner
    ornaments the same template puts on every page. Those mark nothing, so they are not
    here either.
    """
    import hashlib

    from raven_ppt.services.template.menu import ICON_SLOT_NOTE

    sources = [Path(template)] if template is not None and Path(template).is_file() else []
    sources += [Path(other) for other in borrowed if Path(other).is_file()]
    known: set[str] = set().union(*(_picture_hashes(source, every=True) for source in sources)) if sources else set()
    try:
        presentation = open_deck(pptx_path)
    except Exception:  # noqa: BLE001 -- an unreadable deck is not a measurement
        return []
    band = (presentation.slide_width or 0) / EMU_PER_INCH * BAND_SHARE
    findings: list[Finding] = []
    for number, slide in enumerate(presentation.slides, start=1):
        marked = _marked_things(slide, band)
        if len(marked) < MARKED_THINGS:
            continue
        digests = [hashlib.sha1(blob, usedforsecurity=False).hexdigest() for _, blob in marked.values()]
        theirs = sum(1 for digest in digests if digest in known)
        repeats = len(digests) - len(set(digests))
        if len({digest for digest in digests if digest not in known}) >= len(marked):
            continue
        things = ", ".join(f"\u201c{words}\u201d" for words in marked)
        said = []
        if theirs:
            said.append(f"{theirs} of the {len(marked)} marks are the template's own")
        if repeats:
            said.append(f"{repeats} of them repeat{'s' if repeats == 1 else ''} a mark already beside another thing")
        findings.append(
            Finding(
                kind="same_mark",
                severity=Severity.WARNING,
                page=number,
                message=(
                    f"page {number} puts a mark beside each of {len(marked)} things ({things}) and the marks do not "
                    f"tell them apart: {'; '.join(said)}. A" + ICON_SLOT_NOTE[1:]
                ),
                detail={"things": list(marked), "template_marks": theirs, "repeated_marks": repeats},
            )
        )
    return findings


def _marked_things(slide: Any, band: float) -> dict[str, tuple[Any, bytes]]:
    """words of the text block -> (the mark's shape, its image), for each block wearing a mark.

    A block wearing two marks keeps the nearer; the far one is decoration between rows.
    """
    texts = []
    marks = []
    for shape in iter_shapes(slide.shapes):
        box = page_box(shape)
        if box is None:
            continue
        blob = picture_blob(shape)
        if blob is not None:
            if is_icon_sized(shape):
                marks.append((box, shape, blob))
            continue
        if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip() and box.width < band:
            texts.append((box, " ".join(shape.text_frame.text.split())[:30]))
    marked: dict[str, tuple[float, Any, bytes]] = {}
    for box, shape, blob in marks:
        centre = (box.x0 + box.x1) / 2
        nearest = None
        for text, words in texts:
            if not text.x0 <= centre <= text.x1:
                continue
            gap = max(0.0, max(box.y0, text.y0) - min(box.y1, text.y1))
            if gap <= MARK_GAP_IN and (nearest is None or gap < nearest[0]):
                nearest = (gap, words)
        if nearest is None:
            continue
        gap, words = nearest
        if words not in marked or gap < marked[words][0]:
            marked[words] = (gap, shape, blob)
    return {words: (shape, blob) for words, (_, shape, blob) in marked.items()}


def layouts_with_photographs(pptx_path: Path) -> dict[str, tuple[list[int], list[str]]]:
    """Each layout carrying a picture big enough to be content, with the pages built on it.

    Keyed by layout name; the value is (page numbers, picture sizes as "WxHin"). Pictures
    on a layout are inherited by every page on it and are not on the page, so the
    per-page reading above never sees them -- which is how a deck shipped with the
    template's photograph on every section page and nothing reported.
    """
    try:
        presentation = open_deck(pptx_path)
    except Exception:  # noqa: BLE001 -- an unreadable deck is not a measurement
        return {}
    canvas = (presentation.slide_width or 1) * (presentation.slide_height or 1)
    carried: dict[str, tuple[list[int], list[str]]] = {}
    for number, slide in enumerate(presentation.slides, start=1):
        layout = slide.slide_layout
        sizes = [
            f"{shape.width / EMU_PER_INCH:.1f}x{shape.height / EMU_PER_INCH:.1f}in"
            for shape in iter_shapes(layout.shapes)
            if picture_blob(shape) is not None
            and shape.width
            and shape.height
            and (shape.width * shape.height) / canvas >= PICTURE_IS_DECORATION
        ]
        if not sizes:
            continue
        pages, _ = carried.setdefault(layout.name or "unnamed layout", ([], sizes))
        pages.append(number)
    return carried


def layout_photographs(pptx_path: Path, template: Path | None) -> list[Finding]:
    """Pages sitting on a layout that carries the template's own photograph.

    The other half of `template_pictures`: that one reads the pictures on the page, and
    a template's cover, section and closing photographs are as often on the *layout*,
    where `pictures={...}` on a cloned page never reaches them. Reported, not refused,
    and once per layout rather than once per page, because the fix is one call for the
    whole layout -- and because an illustration the designer drew there is the design,
    which the author keeps.
    """
    if template is None or not Path(template).is_file():
        return []
    carried = layouts_with_photographs(pptx_path)
    if not carried:
        return []
    named = "; ".join(
        f"'{layout}' ({', '.join(sizes)}) under page(s) {', '.join(str(page) for page in pages)}"
        for layout, (pages, sizes) in carried.items()
    )
    return [
        Finding(
            kind="layout_picture",
            severity=Severity.WARNING,
            message=(
                f"the template's own picture is on the layout, not the page, so every page on it shows it: {named}. "
                "`pictures={...}` on the cloned page cannot reach a layout's picture. `layout_pictures(slide)` "
                "returns them, largest first, and `replace_picture(layout_pictures(slide)[0], FIGURES/'x.png', "
                "'cover')` changes the picture for every page on that layout at once -- a picture generated in "
                "the deck's own style is the usual replacement. One the size of the page is the page's background "
                "with type over it: `alpha=0.1` keeps it a texture, and a photograph meant to be seen goes in at "
                "full strength under a plane of ink with light type, as `backdrop` lays them; a wash between is "
                "the fog `washed_backdrop` reports. Keep it if it is the design (an illustration) rather than a "
                "stock photograph"
            ),
            detail={layout: {"pages": pages, "sizes": sizes} for layout, (pages, sizes) in carried.items()},
        )
    ]


def _picture_hashes(path: Path, every: bool = False) -> set[str]:
    return {digest for digests in _pictures_by_page(path, every).values() for digest in digests}


def _pictures_by_page(path: Path, every: bool = False) -> dict[int, list[str]]:
    """sha1 of every embedded image big enough to be content, keyed by page.

    `every` lifts the size band: `unit_marks` asks whether a mark is the template's, and
    a mark is by definition under the band.
    """
    import hashlib

    try:
        presentation = open_deck(path)
    except Exception:  # noqa: BLE001 -- an unreadable deck is not a measurement
        return {}
    canvas = (presentation.slide_width or 1) * (presentation.slide_height or 1)
    pages: dict[int, list[str]] = {}
    for number, slide in enumerate(presentation.slides, start=1):
        found: list[str] = []
        for shape in iter_shapes(slide.shapes):
            # `picture_blob` rather than `shape.image`: a template's photograph is as
            # often a rounded rectangle filled with one, and this check missed every one
            # of those -- including the stock photograph of a meeting table that a
            # delivered deck kept on its contents page at 27% of the canvas.
            blob = picture_blob(shape)
            if blob is None or not shape.width or not shape.height:
                continue
            area = (shape.width * shape.height) / canvas
            if every or PICTURE_IS_DECORATION <= area < PICTURE_IS_BACKGROUND:
                found.append(hashlib.sha1(blob, usedforsecurity=False).hexdigest())
        pages[number] = found
    return pages


def _placeholders_by_page(pptx_path: Path, template: Path) -> dict[int, list[str]]:
    """Which pages still carry text the template wrote, keyed by page."""
    placeholders = _texts(template)
    if not placeholders:
        return {}
    return {number: sorted(texts & placeholders) for number, texts in _texts_by_page(pptx_path).items()}


def _texts(path: Path) -> set[str]:
    """Every replaceable text block in a deck, normalised, from every page."""
    return {text for texts in _texts_by_page(path).values() for text in texts}


def _texts_by_page(path: Path) -> dict[int, set[str]]:
    try:
        presentation = open_deck(path)
    except Exception:  # noqa: BLE001 -- an unreadable deck is not a measurement
        return {}
    pages: dict[int, set[str]] = {}
    for number, slide in enumerate(presentation.slides, start=1):
        found: set[str] = set()
        for shape in iter_shapes(slide.shapes):
            if not getattr(shape, "has_text_frame", False):
                continue
            text = " ".join(shape.text_frame.text.split())
            if len(text) < PLACEHOLDER_MIN_CHARS or text.replace(".", "").isdigit():
                continue
            if text.lower().startswith(("http://", "https://", "www.")):
                continue
            found.add(text)
        pages[number] = found
    return pages


# A page counts as built on the prototype it named when this share of its shapes sit
# where that prototype puts one. Lower than FROM_PROTOTYPE because the question is
# narrower: not "did this come from the template" but "did it come from page 7", and a
# page that kept a third of page 7's furniture answered yes to a promise about page 7.
KEPT_ITS_PROTOTYPE = 0.35


def prototype_kept(pptx_path: Path, template: Path | None, outline: Any | None) -> list[Finding]:
    """Pages that did not come from the prototype their own outline named.

    The last link of the chain and the one that was missing. `ppt_outline` takes a
    `prototype` per page and refuses a deck whose cover, index and closing page do not
    name the template's own -- and then nothing ever looked at the file to see whether
    the promise was kept. Measured on two live decks afterwards: one honoured 9 of its
    12 pages and quietly built three others somewhere else; the other named a prototype
    for all twelve, cloned `slides[1]` for every one of them, and matched its own plan
    on exactly one page.

    Neither was refusing to work. Both wrote a plan, and nobody read it back.

    Reported rather than refused (D3b): the fix is either the program (build the page
    on the page it promised)
    or the outline (promise the page it was actually built on), and both are the
    author's to choose.
    """
    from raven_ppt.services.template.defaults import bundled_path

    if outline is None:
        return []
    bound = Path(template) if template is not None and Path(template).is_file() else None
    read: dict[Path, dict[int, set[tuple[float, float, float, float]]]] = {}

    def pages_of(source: Path) -> dict[int, set[tuple[float, float, float, float]]]:
        if source not in read:
            read[source] = _pages(source)
        return read[source]

    built = _pages(pptx_path)
    findings: list[Finding] = []
    for page in getattr(outline, "pages", ()):
        wanted = getattr(page, "prototype", None)
        if wanted is None:
            continue  # the page said it would be drawn, and the geometry gates own it
        # A borrowed page promised a page of another bundled template, and is held to
        # that file's geometry rather than the bound template's.
        stem = getattr(page, "borrowed", "")
        source = bundled_path(stem) if stem else bound
        if source is None:
            continue
        prototypes = pages_of(source)
        boxes = prototypes.get(wanted)
        shapes = built.get(page.page)
        if not boxes or not shapes or len(shapes) < MIN_SHAPES:
            continue
        share = sum(1 for box in shapes if _matches(box, boxes)) / len(shapes)
        if share >= KEPT_ITS_PROTOTYPE:
            continue
        # Which page it *did* come from, because that is the sentence that makes this
        # fixable. Two pages of a live deck named prototype 8 and cloned `slides[6]`,
        # which is page 7: off by one, in the one direction every 1-based menu invites,
        # and the finding said only "it came from somewhere else" -- true, and no help.
        actual = _nearest(shapes, prototypes, exclude=wanted)
        instead = ""
        whose = f"the bundled template {stem}'s" if stem else "the template's"
        if actual is not None:
            number, matched = actual
            instead = f" It matches {whose} page {number} ({matched:.0%} of its shapes)."
        call = f"prototype(bundled({stem!r}), {wanted})" if stem else f"prototype(tpl, {wanted})"
        findings.append(
            Finding(
                kind="prototype_kept",
                severity=Severity.WARNING,
                page=page.page,
                message=(
                    f"page {page.page} says in the outline that it is built on {whose} page {wanted}, and "
                    f"{share:.0%} of its shapes sit where that page puts one.{instead} Build it on the page it "
                    f"promised -- `adapt(prs, {call}, ...)`, which counts from 1 -- or change the "
                    f"outline to name the page it is really built on. A plan nobody follows is worse than no "
                    f"plan, because the checks that trust it stop meaning anything"
                ),
                detail={"promised": wanted, "share": round(share, 2), "matches": actual[0] if actual else None},
            )
        )
    return findings


def _nearest(shapes: set, prototypes: dict[int, set], exclude: int | None = None) -> tuple[int, float] | None:
    """The template page this page's geometry actually came from, and how much of it."""
    best: tuple[int, float] | None = None
    for number, boxes in prototypes.items():
        if number == exclude or not boxes:
            continue
        share = sum(1 for box in shapes if _matches(box, boxes)) / len(shapes)
        if share >= KEPT_ITS_PROTOTYPE and (best is None or share > best[1]):
            best = (number, share)
    return best


def _matches(box: tuple[float, float, float, float], known: set[tuple[float, float, float, float]]) -> bool:
    """Whether this shape sits where the template puts one, within the tolerance."""
    if box in known:
        return True
    return any(max(abs(box[index] - other[index]) for index in range(4)) <= TOLERANCE_IN for other in known)


def _pages(path: Path) -> dict[int, set[tuple[float, float, float, float]]]:
    """Each page's shape positions in inches, keyed by page number."""
    try:
        presentation = open_deck(path)
    except Exception:  # noqa: BLE001 -- an unreadable deck is not a measurement
        return {}
    pages: dict[int, set[tuple[float, float, float, float]]] = {}
    for number, slide in enumerate(presentation.slides, start=1):
        boxes: set[tuple[float, float, float, float]] = set()
        for shape in iter_shapes(slide.shapes):
            try:
                boxes.add(
                    (
                        round(shape.left / EMU_PER_INCH, _PLACES),
                        round(shape.top / EMU_PER_INCH, _PLACES),
                        round(shape.width / EMU_PER_INCH, _PLACES),
                        round(shape.height / EMU_PER_INCH, _PLACES),
                    )
                )
            except TypeError:  # a shape with no geometry of its own
                continue
        pages[number] = boxes
    return pages
