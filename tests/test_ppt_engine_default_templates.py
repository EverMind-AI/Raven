import re
from pathlib import Path

import pytest

pytest.importorskip("pptx")

_TEMPLATES = Path(__file__).resolve().parents[1] / "plugins-dist" / "ppt-engine" / "raven_ppt" / "assets" / "templates"
_needs_templates = pytest.mark.skipif(
    not any(_TEMPLATES.glob("*.pptx")),
    reason="the template payload is fetched, not tracked; see plugins-dist/ppt-engine/templates.manifest.json",
)
pytestmark = _needs_templates

from raven_ppt.contracts import Project  # noqa: E402
from raven_ppt.services.template import (  # noqa: E402
    default_template_catalog,
    house_style,
)
from raven_ppt.services.template.bind import bind  # noqa: E402
from raven_ppt.services.template.defaults import DEFAULT_TEMPLATES  # noqa: E402
from raven_ppt.services.template.menu import menu, roles  # noqa: E402


def test_every_bundled_template_says_which_ground_it_has_and_is_named_once() -> None:
    """Counted off the declaration rather than written down here.

    A number in this test said ten while twelve shipped, because the swap that
    changed the set had no reason to come here. What the catalogue owes is that
    every entry resolves, none is a duplicate, and the promise the prompt makes
    about them -- each is tagged light or dark, never both, and the dark ones are the
    exception the prompt names -- is true of each.
    """
    catalog = default_template_catalog()
    declared = [template for template in DEFAULT_TEMPLATES if template.path.is_file()]

    assert catalog == tuple(declared), "the catalogue is what is declared and on disk"
    assert catalog, "a checkout with no bundled template leaves a task with none to offer"
    assert all(("light" in template.tags) != ("dark" in template.tags) for template in catalog)
    assert 1 <= sum("dark" in template.tags for template in catalog) < len(catalog) / 2
    assert len({template.filename for template in catalog}) == len(catalog)


@pytest.mark.parametrize("index", range(len(DEFAULT_TEMPLATES)))
def test_each_bundled_template_binds_and_exposes_house_style(tmp_path: Path, index: int) -> None:
    template = default_template_catalog()[index]
    project = Project(tmp_path, f"template_{index}")

    bound = bind(template.path, project)

    assert bound is not None
    assert bound.example_pages > 0
    entries = menu(bound.source)
    named = roles(entries)
    assert named.get("cover") == 1
    assert "closing" in named
    # All four, on every bundled template. Two of them used to be missing: gold_panel
    # reported page 14 as its closing and beige_geometric reported no agenda at all,
    # both because the words on a page were read ahead of the layout the template named
    # it with -- and the agenda word on beige page 2 is not on its first line at all.
    assert set(named) == {"cover", "agenda", "section", "closing"}, f"{template.filename} names {sorted(named)}"
    # The page the deck closes on is the page the file calls a closing page.
    assert entries[named["closing"] - 1].layout == "Closing"
    assert house_style(bound.source, entries) is not None


@pytest.mark.parametrize("index", range(len(DEFAULT_TEMPLATES)))
def test_every_content_example_says_what_arrangement_it_is(tmp_path: Path, index: int) -> None:
    """The one thing an author picking a prototype needs off the listing.

    A content page came back as its heading -- somebody else's quarterly report -- the
    layout name every content page in the file shares, and two counts, so nothing in
    the listing told one apart from another. A live run against an eighteen-page
    template then cloned the five pages the reply named by role and drew all twelve
    content pages from primitives, which is a deck with three tables and no timeline.

    Not every page: one page of one bundled template is too sparse to read a signature
    off, and the reply drops the clause rather than printing an empty one. What the
    listing must not do is go quiet across a whole template.
    """
    template = default_template_catalog()[index]
    project = Project(tmp_path, f"arrangement_{index}")

    bound = bind(template.path, project)

    assert bound is not None
    examples = [entry for entry in menu(bound.source) if not entry.role and not entry.hidden]
    assert examples, "a bundled template with no content example is not a template"
    named = [entry for entry in examples if entry.arrangement]
    assert len(named) >= len(examples) - 1, f"{len(examples) - len(named)} of {len(examples)} say nothing"
    # And they have to separate the pages, not label them all alike: the listing is
    # what an author matches its content against.
    assert len({entry.arrangement for entry in named}) >= len(named) / 2
    assert all(entry.arrangement in entry.line() for entry in named)


@pytest.mark.parametrize(
    "stem,number",
    [
        (s, n)
        for s, pages in __import__(
            "raven_ppt.services.template.defaults", fromlist=["REFERENCE_PAGES"]
        ).REFERENCE_PAGES.items()
        for n in pages
    ],
)
def test_every_reference_page_ships_and_is_a_content_page(stem: str, number: int) -> None:
    """The pages a deck may borrow: on disk, not the file's cover or closing, not hidden,
    and readable as an arrangement -- a borrowed page is offered by what it is."""
    from raven_ppt.services.template.defaults import bundled_path

    path = bundled_path(stem)
    assert path is not None, f"{stem} is a reference template and does not ship"
    entries = {entry.number: entry for entry in menu(path)}
    entry = entries.get(number)
    assert entry is not None, f"{stem} has no page {number}"
    assert not entry.role and not entry.hidden, f"{stem} page {number} is the template's {entry.role or 'hidden'} page"
    assert entry.arrangement, f"{stem} page {number} reads as no arrangement"


def test_every_page_the_borrow_offer_lists_is_a_page_the_sheet_can_show() -> None:
    """How many pages the borrow offer is, and that not one of them is withheld.

    The count matters because the offer is now a picture as well as a list, and the two
    are built from one walk over `reference_pages()`: a page the sentences name and the
    sheet has no cell for, or the other way round, is a key that does not fit its lock.

    The number itself is not the claim -- another branch adding a template adds rows to
    the table, and a test that pins the total makes that a failure rather than a fact.
    What is pinned is that the table and the offer are the same length. 36 on the eight
    templates this branch ships. An earlier reading of that set put the number at 35, on
    the grounds that `teal_illustrated_work_analysis` page 13 is one of
    the vendor's own advertising pages and marked not-for-show. That is true of a
    different copy of that template -- the fork tree ships a 13-slide file whose pages
    12 and 13 carry `show="0"` and the vendor's channels -- and not of the file this
    engine ships, whose sha256 is the one in `templates.manifest.json`: 15 slides, none
    marked hidden, and page 13 a five-item numbered list beside an illustration. The
    filter in `_borrowable` still drops a hidden page if a refresh ever brings one back.
    """
    from raven_ppt.services.template.defaults import REFERENCE_PAGES, bundled_path, reference_pages

    offered = reference_pages(except_stem="")

    assert len(offered) == sum(len(pages) for pages in REFERENCE_PAGES.values()), (
        "every reference page ships, so the offer is the whole table"
    )
    withheld = [
        (stem, number)
        for stem, number in offered
        if (entry := {page.number: page for page in menu(bundled_path(stem))}.get(number)) is None
        or entry.role
        or entry.hidden
    ]
    assert withheld == [], f"the sentences offer pages the sheet cannot show: {withheld}"
    teal = {entry.number: entry for entry in menu(bundled_path("teal_illustrated_work_analysis"))}
    assert len(teal) == 15 and not teal[13].hidden and not teal[13].role
    assert teal[13].arrangement, "page 13 reads as an arrangement, which an advertisement would not"


def test_the_bound_template_is_not_offered_to_itself() -> None:
    from raven_ppt.services.template.defaults import REFERENCE_PAGES, reference_pages

    stem = next(iter(REFERENCE_PAGES))
    offered = reference_pages(except_stem=stem)

    assert offered and all(s != stem for s, _ in offered)
    assert len(offered) == sum(len(pages) for s, pages in REFERENCE_PAGES.items() if s != stem)


def test_the_reference_pages_that_carry_house_coloured_drawings_are_named() -> None:
    """The four pages a bitmap makes an exception of, checked against the payload.

    A borrowed page arrives in the deck's own palette because every fill on it is a
    theme colour -- the whole reason these pages are the ones offered. A picture is not
    a fill: a drawing painted in its own template's accents arrives in those accents,
    and it is the only thing on these pages that does. The list is data, so what this
    holds is that each entry is a reference page that really does carry that many
    images, and that no page outside the list carries a drawing nobody was warned about.
    """
    import re

    from pptx import Presentation

    from raven_ppt.services.template.defaults import (
        REFERENCE_ARTWORK,
        REFERENCE_PAGES,
        bundled_path,
        reference_artwork,
    )

    for stem, pages in REFERENCE_ARTWORK.items():
        assert stem in REFERENCE_PAGES, f"{stem} is not a template anything borrows from"
        path = bundled_path(stem)
        if path is None:
            continue
        slides = list(Presentation(str(path)).slides)
        for number, drawings in pages.items():
            assert number in REFERENCE_PAGES[stem], f"{stem} page {number} is not offered for borrowing"
            carried = len(re.findall(r"<a:blip ", slides[number - 1]._element.xml))
            assert carried >= drawings, f"{stem} page {number} carries {carried} image(s), not {drawings}"
            assert reference_artwork(stem, number) == drawings

    assert reference_artwork("gold_panel_year_end_summary", 6) == 0, "a photograph is a placeholder, not a caveat"
    assert reference_artwork("no_such_template", 1) == 0


def test_the_ratio_at_which_a_borrowed_label_stops_reading_is_the_measurement_packages_own() -> None:
    """Two modules state it and only one may decide it.

    `defaults` cannot import the measurement package -- it is read where python-pptx is
    not wanted -- so the number is written twice. Written twice it can drift, and the
    drift would be silent: the gate would refuse a page the offer had just called safe.
    """
    from raven_ppt.services.measure.contrast import UNREADABLE_RATIO
    from raven_ppt.services.template.defaults import BORROWED_INK_READS

    assert BORROWED_INK_READS == UNREADABLE_RATIO


def test_every_bundled_template_is_told_which_of_its_colours_wash_a_borrowed_label_out() -> None:
    """Measured over 486 clones of the 54 reference pages into the ten bundled templates.

    89 of those pages came back with copy under the ratio, in every one of the ten hosts.
    The question this replaces asked whether white reads on `accent1`, which is true of
    nine of the ten, so nine hosts were told nothing -- the dark one 17 of the failures
    are in among them. What each host owes now is the pairs its own palette cannot show,
    and the two the measurement weighs most are the pale-accent host, whose `accent1`
    renders white at 1.88:1, and the dark one, whose `accent2` renders it at 1.91:1 and
    whose 60/40 tint of it renders it at 1.5:1.
    """
    from raven_ppt.services.template.defaults import templates_dir
    from raven_ppt.services.template.inventory import inspect_template
    from raven_ppt.services.template.theme import borrow_ink_note, unreadable_grounds

    said = {}
    for path in sorted(templates_dir().glob("*.pptx")):
        inventory = inspect_template(path)
        assert inventory is not None
        said[path.stem] = unreadable_grounds(inventory)

    silent = [stem for stem, found in said.items() if not found]
    assert not silent, f"these hosts are told nothing and 89 measured failures say otherwise: {silent}"

    pale = said["warm_bauhaus_quarterly_review"]
    assert any(one.fill == "accent1" and 1.8 < one.ratio < 1.95 for one in pale), pale

    dark = said["black_circuit_tech_launch"]
    assert any(one.fill == "accent2" for one in dark), dark
    # The tint and not only the swatch: 22 of the 89 landed on a tint of a stated colour.
    assert any(one.tint != (1.0, 0.0) for one in dark), dark

    path = templates_dir() / "warm_bauhaus_quarterly_review.pptx"
    note = borrow_ink_note(inspect_template(path), path)
    assert "1.9:1" in note and "this deck's ink" in note
    # And it names a page of this template that already does it, because "set it in the
    # ink" without one leaves the author to invent the alternative.
    assert re.search(r"own page \d+ does", note), note

    other = templates_dir() / "black_circuit_tech_launch.pptx"
    assert borrow_ink_note(inspect_template(other), other), "the dark host carries 17 of the 89"


def test_a_hosts_own_pages_are_not_what_the_borrow_note_speaks_about() -> None:
    """The other half of the question, and the half a ratio alone cannot answer.

    Two of the 54 reference pages carry copy under the ratio in their own template's
    colours -- `green_aurora` page 4 at 1.80:1 and `mint_memphis` page 6 at 1.78:1 -- and
    `measure.contrast` reports both as warnings rather than refusals, because the shape
    sits where that template's own page puts one and so the design is the designer's. A
    note that spoke about a host's native pages would be arguing with that, so it cannot
    reach them: what it speaks about is the borrow offer, and the offer is every reference
    page except the bound template's own.
    """
    from raven_ppt.services.template.defaults import REFERENCE_PAGES, reference_pages, templates_dir

    for path in sorted(templates_dir().glob("*.pptx")):
        offered = reference_pages(except_stem=path.stem)
        assert offered, f"{path.stem} is offered nothing to borrow"
        assert all(stem != path.stem for stem, _ in offered), path.stem
        assert len(offered) == sum(len(pages) for stem, pages in REFERENCE_PAGES.items() if stem != path.stem)


# What share of a label's box a card has to cover to be the card it sits on. Whole
# containment rather than a share of it: a label reaching past its card is the case
# `measure.contrast` reads a render for.
_CARD_HOLDS = 0.9


def test_every_pinned_label_pair_is_one_the_reference_pages_really_write() -> None:
    """The evidence half, run rather than asserted: open the pages and find the pair.

    The pinned pairs are a measurement, and a measurement nobody re-runs is a number
    someone will edit. Each pair says a run of one colour sits on a card of another, so
    each is looked for where it was read: a shape's own fill, or the card whose box
    contains it.
    """
    from pptx import Presentation

    from raven_ppt.services.measure.geometry import ink_box, iter_shapes, page_box
    from raven_ppt.services.template.defaults import (
        BORROWED_LABEL_PAIRS,
        REFERENCE_PAGES,
        bundled_path,
    )
    from raven_ppt.services.template.theme import _card_fills, _label_inks

    found: set[tuple[str, str, float, float, float]] = set()
    for stem, pages in REFERENCE_PAGES.items():
        source = bundled_path(stem)
        if source is None:
            continue
        presentation = Presentation(str(source))
        for number in pages:
            shapes = list(iter_shapes(presentation.slides[number - 1].shapes))
            cards = [(page_box(shape), _card_fills(shape)) for shape in shapes]
            cards = [(box, fills) for box, fills in cards if box is not None and box.area > 0 and fills]
            for shape in shapes:
                inks = _label_inks(shape)
                if not inks:
                    continue
                own = _card_fills(shape)
                if own:
                    painted = own
                else:
                    where = ink_box(shape)
                    if where is None or where.area <= 0:
                        continue
                    inside = [
                        (box.area, fills) for box, fills in cards if box.overlap(where) / where.area >= _CARD_HOLDS
                    ]
                    if not inside:
                        continue
                    painted = min(inside)[1]
                found.update((ink,) + card for ink in inks for card in painted if ink != card[0])

    missing = [pair[:5] for pair in BORROWED_LABEL_PAIRS if pair[:5] not in found]
    assert not missing, f"pinned pairs no reference page writes: {missing}"


def test_the_page_named_as_the_example_really_sets_dark_ink_on_that_accent() -> None:
    """The evidence half, run rather than asserted: open the page and measure it."""
    from pptx import Presentation

    from raven_ppt.services.assets.color import contrast_ratio
    from raven_ppt.services.template.defaults import templates_dir
    from raven_ppt.services.template.theme import (
        _fill_scheme,
        _palette_of,
        _run_inks,
        _walk,
        labels_accent_in_ink,
    )

    path = templates_dir() / "warm_bauhaus_quarterly_review.pptx"
    number = labels_accent_in_ink(path)
    assert number is not None

    presentation = Presentation(str(path))
    palette = _palette_of(presentation)
    slide = list(presentation.slides)[number - 1]
    measured = [
        contrast_ratio(ink, palette["accent1"])
        for shape in _walk(slide.shapes)
        if _fill_scheme(shape) == "accent1"
        for ink in _run_inks(shape, palette)
    ]
    assert measured, f"page {number} was named and sets no ink on accent1"
    assert max(measured) >= 3.0, measured
