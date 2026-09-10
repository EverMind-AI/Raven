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


def test_every_bundled_template_is_light_present_and_named_once() -> None:
    """Counted off the declaration rather than written down here.

    A number in this test said ten while twelve shipped, because the swap that
    changed the set had no reason to come here. What the catalogue owes is that
    every entry resolves, none is a duplicate, and the promise the prompt makes
    about them -- these are the light ones -- is true of each.
    """
    catalog = default_template_catalog()
    declared = [template for template in DEFAULT_TEMPLATES if template.path.is_file()]

    assert catalog == tuple(declared), "the catalogue is what is declared and on disk"
    assert catalog, "a checkout with no bundled template leaves a task with none to offer"
    assert all("light" in template.tags for template in catalog)
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


def test_the_ratio_at_which_white_stops_reading_is_the_measurement_packages_own() -> None:
    """Two modules state it and only one may decide it.

    `defaults` cannot import the measurement package -- it is read where python-pptx is
    not wanted -- so the number is written twice. Written twice it can drift, and the
    drift would be silent: the gate would refuse a page the offer had just called safe.
    """
    from raven_ppt.services.measure.contrast import UNREADABLE_RATIO
    from raven_ppt.services.template.defaults import ACCENT_READS_WHITE

    assert ACCENT_READS_WHITE == UNREADABLE_RATIO


def test_only_the_template_whose_accent_is_pale_asks_for_dark_labels() -> None:
    """Measured over the 252 cross-template clones, and the one exception it found.

    Seven of the eight bundled templates have an accent1 dark enough to label in white,
    which is why every reference page labels its cards that way. The eighth does not,
    and 19 of the 31 pages borrowed into it came back with copy under the ratio. So the
    note has to fire for that one and stay silent for the other seven -- a note on a
    template that carries white fine is what teaches an author to read past the notes.
    """
    from raven_ppt.services.template.defaults import templates_dir
    from raven_ppt.services.template.inventory import inspect_template
    from raven_ppt.services.template.theme import borrow_ink_note, white_on_accent

    said = {}
    for path in sorted(templates_dir().glob("*.pptx")):
        inventory = inspect_template(path)
        assert inventory is not None
        said[path.stem] = white_on_accent(inventory)

    pale = {stem: found for stem, found in said.items() if found is not None}
    assert list(pale) == ["warm_bauhaus_quarterly_review"], f"expected one pale template, got {list(pale)}"
    slot, ratio = pale["warm_bauhaus_quarterly_review"]
    assert slot == "accent1"
    assert 1.8 < ratio < 1.95

    path = templates_dir() / "warm_bauhaus_quarterly_review.pptx"
    note = borrow_ink_note(inspect_template(path), path)
    assert "1.9:1" in note and "this deck's ink" in note
    # And it names a page of this template that already does it, because "set it in the
    # ink" without one leaves the author to invent the alternative.
    assert re.search(r"own page \d+ does", note), note

    other = templates_dir() / "beige_geometric_general_report.pptx"
    assert borrow_ink_note(inspect_template(other), other) == ""


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
