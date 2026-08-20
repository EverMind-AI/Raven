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

from raven.ppt.contracts import Project  # noqa: E402
from raven.ppt.services.gates.house_style import house_style_findings  # noqa: E402
from raven.ppt.services.measure.inherited import over_layout_art  # noqa: E402
from raven.ppt.services.template import bind, theme_name, theme_of  # noqa: E402


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


def test_a_deck_that_is_not_in_the_template_is_refused(deck, tmp_path: Path):
    """`Presentation()` instead of `Presentation(os.environ['PPT_TEMPLATE'])`: a
    white deck on a default canvas, which published clean before this."""
    deck.text(deck.page(), ("A page", 24.0))
    built = deck.save()
    template = _recolour(built, tmp_path / "house.pptx")

    findings = house_style_findings(built, template)

    assert len(findings) == 1
    assert findings[0].kind == "house_style"
    assert findings[0].severity.value == "blocking"
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
    from raven.ppt.backends.script import asset_helpers, provision, with_template_helpers
    from raven.ppt.services.assets import script_helpers

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
    from raven.ppt.backends.script import asset_helpers, provision
    from raven.ppt.services.assets import script_helpers

    project = Project(workspace=tmp_path, slug="talk")

    workdir = provision(project, asset_helpers())

    themes = json.loads((workdir / script_helpers.THEME_DATA_FILENAME).read_text(encoding="utf-8"))
    assert len(themes) >= 10
    assert not (workdir / "ppt_template.py").exists()


def test_the_theme_falls_back_rather_than_inventing_an_accent(tmp_path: Path):
    """A missing slot means the template did not state one, and inventing a colour
    here would put something in the deck that is in nobody's house style."""
    from raven.ppt.services.template.inventory import TemplateInventory

    bare = TemplateInventory(path=tmp_path / "bare.pptx", width_in=13.333, height_in=7.5)

    entry = theme_of(bare)

    assert entry["accent"] == "#111111"
    assert entry["background"] == "#FFFFFF"
    assert entry["font_family"] == "Arial"


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
    assert findings[0].audience.value == "designer"
    assert "the template kept that part of the page clear" in findings[0].message


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
