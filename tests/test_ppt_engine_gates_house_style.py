"""Two things a deck built inside a template has to get right, and nothing did.

A user hands over their house style, `ppt_template` binds it, the build directory
gets `PPT_TEMPLATE`, the tool description explains how to open it -- and every link
in that chain was prose. An author that wrote `Presentation()` produced a white
deck on a default canvas and it published clean; an author that picked a reviewed
theme produced one in somebody else's colours inside the template and that
published clean too.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("pptx")

from raven_ppt.contracts import Project, Severity  # noqa: E402
from raven_ppt.services.gates.house_style import house_style_findings  # noqa: E402
from raven_ppt.services.measure.inherited import over_layout_art  # noqa: E402
from raven_ppt.services.template import bind, theme_name, theme_of  # noqa: E402
from tests._ppt_engine_fixtures import deck, image, noise_image, noise_png, product_page, template_file  # noqa: F401


def _recolour(source: Path, destination: Path, accent: str = "BADA55") -> Path:
    """The same deck with a different theme accent, rewritten in the package.

    python-pptx has no API for the theme, which is also why the check reads it off
    the XML rather than through a colour object.
    """
    import zipfile

    with zipfile.ZipFile(source) as reading, zipfile.ZipFile(destination, "w") as writing:
        for entry in reading.infolist():
            body = reading.read(entry.filename)
            if entry.filename == "ppt/theme/theme1.xml":
                body = body.replace(
                    b'<a:accent1><a:srgbClr val="4F81BD"/>', f'<a:accent1><a:srgbClr val="{accent}"/>'.encode()
                )
            writing.writestr(entry, body)
    return destination


# --- was it built in the template at all ----------------------------------


def test_a_deck_in_the_template_says_nothing(deck, tmp_path: Path):
    """The check has to be silent on the ordinary case or it is worthless: every
    build with a template bound runs it."""
    deck.text(deck.page(), ("A page", 24.0))
    built = deck.save()

    assert house_style_findings(built, built) == []


def test_a_deck_that_is_not_in_the_template_is_reported(deck, tmp_path: Path):
    """`Presentation()` instead of `Presentation(os.environ['PPT_TEMPLATE'])`: a
    white deck on a default canvas, which published clean before this.

    A warning since D52, and the negative control this file is missing is why: the
    ordinary case is asserted by comparing a deck against itself, which is true by
    construction, so nothing here could have found the false positive that matters."""
    deck.text(deck.page(), ("A page", 24.0))
    built = deck.save()
    template = _recolour(built, tmp_path / "house.pptx")

    findings = house_style_findings(built, template)

    assert len(findings) == 1
    assert findings[0].kind == "house_style"
    assert findings[0].severity.value == "warning"
    assert "PPT_TEMPLATE" in findings[0].message
    assert "house.pptx" in findings[0].message


def test_no_template_and_no_finding(deck, tmp_path: Path):
    deck.text(deck.page(), ("A page", 24.0))
    built = deck.save()

    assert house_style_findings(built, None) == []
    assert house_style_findings(built, tmp_path / "gone.pptx") == []


def test_a_template_that_will_not_open_is_not_grounds_for_refusal(deck, tmp_path: Path):
    """Refusing here would refuse a deck for a property of the user's own file."""
    deck.text(deck.page(), ("A page", 24.0))
    built = deck.save()
    broken = tmp_path / "broken.pptx"
    broken.write_bytes(b"PK not really")

    assert house_style_findings(built, broken) == []


# --- and is it in the template's colours -----------------------------------


def test_the_template_becomes_the_only_theme_a_script_can_pick(tmp_path: Path, template_file):
    """The author is told to take its palette from `ppt_theme`, and with a template
    bound that instruction was wrong -- the correction was a sentence in a tool
    description. Now there is one theme in there and it is the template's, so an
    author that picks `ink-graphite` cannot, because there is no such key."""
    from raven_ppt.backends.script import asset_helpers, provision, with_template_helpers
    from raven_ppt.services.assets import script_helpers

    project = Project(workspace=tmp_path, slug="talk")
    template = bind(template_file(), project)
    assert template is not None

    workdir = provision(project, with_template_helpers(asset_helpers(), template))

    themes = json.loads((workdir / script_helpers.THEME_DATA_FILENAME).read_text(encoding="utf-8"))
    assert list(themes) == [theme_name(template.inventory)]
    assert "ink-graphite" not in themes
    entry = themes[theme_name(template.inventory)]
    assert entry["font_family"]
    assert len(entry["chart_series"]) >= 3
    assert all(str(entry[field]).startswith("#") for field in ("background", "foreground", "accent"))


def test_a_deck_without_a_template_keeps_all_ten(tmp_path: Path):
    from raven_ppt.backends.script import asset_helpers, provision
    from raven_ppt.services.assets import script_helpers

    project = Project(workspace=tmp_path, slug="talk")

    workdir = provision(project, asset_helpers())

    themes = json.loads((workdir / script_helpers.THEME_DATA_FILENAME).read_text(encoding="utf-8"))
    assert len(themes) >= 10
    assert not (workdir / "ppt_template.py").exists()


def test_the_theme_falls_back_rather_than_inventing_an_accent(tmp_path: Path):
    """A missing slot means the template did not state one, and inventing a colour
    here would put something in the deck that is in nobody's house style."""
    from raven_ppt.services.template.inventory import TemplateInventory

    bare = TemplateInventory(path=tmp_path / "bare.pptx", width_in=13.333, height_in=7.5)

    entry = theme_of(bare)

    assert entry["accent"] == "#111111"
    assert entry["background"] == "#FFFFFF"
    assert entry["font_family"] == "Arial"


def test_what_the_master_paints_beats_what_the_colour_map_says(tmp_path: Path):
    """A template may map `bg1` to a colour it then never paints.

    One real template maps it to a #2F2F2F it does not use anywhere and covers
    every page with `<p:bg>` in accent1 instead. Read off the map alone it came
    back as a grey deck, and every plane derived from that grey -- `surface`
    especially -- landed on the purple pages looking dirty.
    """
    from raven_ppt.services.template.inventory import TemplateInventory

    declared_grey = TemplateInventory(
        path=tmp_path / "purple.pptx",
        width_in=13.333,
        height_in=7.5,
        theme_colours=(("dk1", "#2F2F2F"), ("lt1", "#FFFFFF"), ("accent1", "#5E31FF"), ("accent2", "#F98DD6")),
        colour_map=(("bg1", "dk1"), ("tx1", "lt1")),
        painted_ground="accent1",
    )

    entry = theme_of(declared_grey)

    assert entry["background"] == "#5E31FF"
    # A ground that is the accent leaves nothing to accent with, and the template
    # answers that itself: it alternates accent1 grounds with accent2 ones.
    assert entry["accent"] == "#F98DD6"


def test_a_master_that_paints_nothing_keeps_the_mapped_ground(tmp_path: Path):
    """The map is right whenever the master does not override it, which is most
    templates -- nothing here may change what those already resolve to."""
    from raven_ppt.services.template.inventory import TemplateInventory

    plain = TemplateInventory(
        path=tmp_path / "plain.pptx",
        width_in=13.333,
        height_in=7.5,
        theme_colours=(("dk1", "#2F2F2F"), ("lt1", "#FFFFFF"), ("accent1", "#5E31FF")),
        colour_map=(("bg1", "dk1"), ("tx1", "lt1")),
    )

    entry = theme_of(plain)

    assert entry["background"] == "#2F2F2F"
    assert entry["accent"] == "#5E31FF"


def test_the_ground_the_master_paints_is_read_off_the_file(tmp_path: Path, template_file):
    """And it comes from the XML, not from a caller passing it in."""
    import zipfile

    from raven_ppt.services.template.inventory import inspect_template

    source = template_file("plain.pptx")
    painted = tmp_path / "painted.pptx"
    with zipfile.ZipFile(source) as reading, zipfile.ZipFile(painted, "w") as writing:
        for entry in reading.infolist():
            body = reading.read(entry.filename)
            if entry.filename == "ppt/slideMasters/slideMaster1.xml":
                body = body.replace(
                    b"<p:cSld>",
                    b'<p:cSld><p:bg><p:bgPr><a:solidFill><a:schemeClr val="accent1"/>'
                    b"</a:solidFill><a:effectLst/></p:bgPr></p:bg>",
                    1,
                )
            writing.writestr(entry, body)

    assert inspect_template(source).painted_ground is None
    assert inspect_template(painted).painted_ground == "accent1"


def test_the_ground_measured_off_a_render_beats_everything_the_file_says(tmp_path: Path):
    """Measured across 119 templates, what a file declares matched what it renders
    73 times; eleven declared the inverse. So a reading taken off pixels wins."""
    from raven_ppt.services.template.inventory import TemplateInventory

    lying = TemplateInventory(
        path=tmp_path / "lies.pptx",
        width_in=13.333,
        height_in=7.5,
        theme_colours=(("dk1", "#2F2F2F"), ("lt1", "#FFFFFF"), ("accent1", "#5E31FF")),
        colour_map=(("bg1", "lt1"), ("tx1", "dk1")),
        painted_ground="dk1",
        rendered_ground="#2484E4",
    )

    assert theme_of(lying)["background"] == "#2484E4"


def test_a_page_of_one_colour_reports_it_and_a_page_of_many_reports_none(tmp_path: Path):
    from PIL import Image

    from raven_ppt.services.template.theme import ground_of

    flat = tmp_path / "flat.png"
    Image.new("RGB", (400, 225), "#2484E4").save(flat)
    assert ground_of([flat]) == "#2484E4"

    # Noise: no colour is more than a sliver of it, so there is no ground to name.
    busy = tmp_path / "busy.png"
    noise = Image.new("RGB", (400, 225))
    noise.putdata([((x * 7) % 256, (y * 11) % 256, (x + y) % 256) for y in range(225) for x in range(400)])
    noise.save(busy)
    assert ground_of([busy]) is None

    assert ground_of([]) is None
    assert ground_of([tmp_path / "missing.png"]) is None


def test_the_ground_is_the_one_most_pages_share(tmp_path: Path):
    """One template runs a single white page between black ones. A sample that
    caught it read the whole deck as white, so the count is over all the pixels
    sampled rather than over pages."""
    from PIL import Image

    from raven_ppt.services.template.theme import ground_of

    pages = []
    for index, colour in enumerate(("#111111", "#FFFFFF", "#111111", "#111111")):
        page = tmp_path / f"page{index}.png"
        Image.new("RGB", (400, 225), colour).save(page)
        pages.append(page)

    assert ground_of(pages) == "#0C0C0C"


# --- what the layout draws -------------------------------------------------


def test_copy_laid_across_the_layouts_artwork_is_reported(deck):
    """The measurement that did not exist. Three real templates, three pages each,
    built through the route: the numbers landed unreadable over the artwork and the
    deck published clean, because a layout's own shapes never reach
    `slide.shapes`."""
    deck.layout_art(left=7.0, width=6.3)
    deck.text(deck.page(), ("44 FPS on one A100", 40.0), left=8.0, top=3.0, width=4.0, height=1.0)

    findings = over_layout_art(deck.save())

    assert len(findings) == 1
    assert findings[0].kind == "over_layout_art"
    assert findings[0].page == 1
    assert findings[0].severity is Severity.WARNING
    assert "the template kept that part of the page clear" in findings[0].message


def test_a_clone_of_the_templates_own_page_is_not_reported(deck):
    """A cloned page's copy is the template's copy, and it is not in a placeholder.

    `clone` copies the prototype's own text boxes, so the layout has nothing to offer
    and every block reads as laid over the art. Measured on the designers' own files:
    four of the twelve bundled templates reported on themselves, 36 findings over 217
    pages, and `gold_panel_year_end_summary`'s 21 became 17 in the deck built in it.
    """
    deck.layout_art(left=7.0, width=6.3)
    deck.text(deck.page(), ("44 FPS on one A100", 40.0), left=8.0, top=3.0, width=4.0, height=1.0)
    built = deck.save()

    assert len(over_layout_art(built)) == 1, "the template it was built in is what excuses it"
    assert over_layout_art(built, built) == []


def test_copy_somewhere_no_page_of_the_template_puts_any_is_still_reported(deck):
    """The licence is per box, not per template: a page of its own making gets no cover."""
    deck.layout_art(left=7.0, width=6.3)
    deck.text(deck.page(), ("the template's own line", 40.0), left=8.0, top=0.4, width=4.0, height=1.0)
    prototype = deck.save("prototype.pptx")

    deck.text(deck.page(), ("a line of its own", 40.0), left=8.0, top=5.2, width=4.0, height=1.0)
    built = deck.save("built.pptx")

    found = over_layout_art(built, prototype)

    assert [one.detail["text"] for one in found] == ["a line of its own"]


def test_copy_in_the_clear_half_is_not(deck):
    deck.layout_art(left=7.0, width=6.3)
    deck.text(deck.page(), ("44 FPS on one A100", 40.0), left=0.8, top=3.0, width=4.0, height=1.0)

    assert over_layout_art(deck.save()) == []


def test_a_layout_with_nothing_on_it_reports_nothing(deck):
    deck.text(deck.page(), ("A page", 24.0))

    assert over_layout_art(deck.save()) == []


def test_a_corner_clipping_the_artwork_is_not_a_paragraph_laid_across_it(deck):
    deck.layout_art(left=7.0, width=6.3)
    # 0.2in of a 1.2in box, which is 17% and under the threshold.
    deck.text(deck.page(), ("A caption", 12.0), left=6.0, top=3.0, width=1.2, height=0.4)

    assert over_layout_art(deck.save()) == [], "a clipped corner is not the failure this looks for"
