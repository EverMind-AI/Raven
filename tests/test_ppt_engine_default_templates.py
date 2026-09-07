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


def test_the_bound_template_is_not_offered_to_itself() -> None:
    from raven_ppt.services.template.defaults import REFERENCE_PAGES, reference_pages

    stem = next(iter(REFERENCE_PAGES))
    offered = reference_pages(except_stem=stem)

    assert offered and all(s != stem for s, _ in offered)
    assert len(offered) == sum(len(pages) for s, pages in REFERENCE_PAGES.items() if s != stem)
