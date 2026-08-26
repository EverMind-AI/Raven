"""Which two faces a bound template hands the author, and which of its colours.

The face defects both showed on the same Chinese template, whose scheme names
`('Arial', '微软雅黑')`: the Latin face came back as the Han one, and the CJK key
the authoring documents all tell an author to read was not in the theme at all.

The colour half is about the scheme's "second pair". `bg2` and `tx2` are where a
template states a second plane and a second ink, and `theme_of` used to mix both
from the first pair instead, so the plane the author was handed was not the one the
template draws with -- across five 20-page decks built inside the bundled
`5407617`, the `#F0F0F0` its `bg2` states was the dominant plane on two of them,
45% and 78% of all fill area. The stated colour is preferred now, and what
decides is a comparison against the palette rather than a threshold: these tests
are the four ways a stated colour loses that comparison, plus the ten bundled
templates measured end to end.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.ppt.services.template.inventory import TemplateInventory
from raven.ppt.services.template.theme import (
    _GRID_MIX,
    _LEGIBLE,
    _SOFT_MIX,
    _contrast,
    _faces,
    _mixed,
    _receded,
    theme_of,
)


def _inventory(fonts: tuple[str, ...]) -> TemplateInventory:
    return TemplateInventory(path=Path("t.pptx"), width_in=13.333, height_in=7.5, fonts=fonts)


def test_a_chinese_template_gives_both_faces_and_keeps_its_own_han_one() -> None:
    """Preferred over the reviewed default: it is the face its designer chose."""
    assert _faces(("Arial", "微软雅黑")) == ("Arial", "微软雅黑")


def test_a_template_naming_no_han_face_gets_the_reviewed_one_for_its_class() -> None:
    assert _faces(("Arial",)) == ("Arial", "Noto Sans CJK SC")
    assert _faces(("Cambria",)) == ("Cambria", "Noto Serif CJK SC")


def test_a_han_family_spelled_in_ascii_is_still_a_han_family() -> None:
    """ "Not ASCII" alone reads PingFang SC and SimSun as Latin faces."""
    assert _faces(("Helvetica", "PingFang SC")) == ("Helvetica", "PingFang SC")
    assert _faces(("Times New Roman", "SimSun")) == ("Times New Roman", "SimSun")


def test_a_template_that_names_only_a_han_face_still_has_a_latin_one() -> None:
    assert _faces(("微软雅黑",)) == ("Arial", "微软雅黑")


def test_a_template_naming_nothing_still_answers() -> None:
    assert _faces(()) == ("Arial", "Noto Sans CJK SC")


def test_the_theme_a_bound_template_hands_the_author_carries_the_cjk_key() -> None:
    """`ppt_theme`, `ppt_layout.write` and the authoring skill each name it."""
    entry = theme_of(_inventory(("Arial", "微软雅黑")))

    assert entry["cjk_font_family"] == "微软雅黑"
    assert entry["font_family"] == "Arial"


# A light template's map, which is also what PowerPoint defaults to: the second
# pair resolves to `lt2`/`dk2`, so a scheme states its second plane there.
_LIGHT_MAP = (("accent1", "accent1"), ("bg1", "lt1"), ("bg2", "lt2"), ("tx1", "dk1"), ("tx2", "dk2"))
# A dark template's, which is how a template is dark at all: the same four scheme
# names, crossed over, so `bg2` is `dk2` and `tx2` is `lt2`.
_DARK_MAP = (("accent1", "accent1"), ("bg1", "dk1"), ("bg2", "dk2"), ("tx1", "lt1"), ("tx2", "lt2"))


def _scheme(mapping: tuple[tuple[str, str], ...], **slots: str) -> TemplateInventory:
    """An inventory whose colour scheme is exactly these slots, read through `mapping`."""
    return TemplateInventory(
        path=Path("t.pptx"),
        width_in=13.333,
        height_in=7.5,
        layouts=(),
        theme_colours=tuple(slots.items()),
        colour_map=mapping,
        fonts=("Arial",),
    )


def test_the_plane_a_template_states_is_not_the_plane_any_of_its_pages_paint() -> None:
    """`bg2` is not read: #F0F0F0 is stated by every bundled template and painted by none.

    Sixteen templates, every solid shape fill in each of them: the declared second
    background appears on 0 of the 16, and the accent is the largest non-ground fill
    in 8 -- 79% of the painted area in 5407011, 57% in 5407013. So the plane comes
    off the accent, and a deck built in a blue template is not grey inside it.
    """
    entry = theme_of(_scheme(_LIGHT_MAP, lt1="#FFFFFF", dk1="#000000", lt2="#F0F0F0", accent1="#155FFD"))

    assert entry["surface"] != "#F0F0F0"
    assert entry["surface"] == _mixed("#155FFD", "#FFFFFF", 1 - (1 - _SOFT_MIX) / 2)
    assert entry["background"] == "#FFFFFF"


def test_the_plane_is_visible_on_its_ground_and_carries_the_ink_that_is_written_on_it() -> None:
    """A plane nobody can see is not one, and neither is one the copy cannot sit on."""
    entry = theme_of(_scheme(_LIGHT_MAP, lt1="#FFFFFF", dk1="#000000", lt2="#FFFFFF", accent1="#155FFD"))
    surface, ground = str(entry["surface"]), str(entry["background"])

    assert surface != ground
    assert _contrast(surface, ground) > 1.0
    assert _contrast(str(entry["muted"]), surface) >= _LEGIBLE


@pytest.mark.parametrize(
    "accent",
    ["#155FFD", "#1A3397", "#B1EC52", "#B38D76", "#05A3F1", "#11B582", "#000000", "#FFFF00"],
)
def test_the_quiet_plane_stays_quieter_than_the_tile_that_carries_the_answer(accent: str) -> None:
    """Both off the same tint, because two planes on two scales trade places.

    Measured when they were on two: the plane reached for the contrast a tint of the
    ink would have had, 1.25, and a fixed mix of the accent landed anywhere from
    1.06 for a pale lime to 1.39 for a navy -- so the plane came out louder than the
    tile in 8 of the 16 bundled templates and level with it in a ninth, and the one
    zone meant to carry the page's answer was the quietest thing on the page.
    """
    entry = theme_of(_scheme(_LIGHT_MAP, lt1="#FFFFFF", dk1="#000000", accent1=accent))
    ground = str(entry["background"])

    assert _contrast(str(entry["surface"]), ground) < _contrast(str(entry["accent_soft"]), ground)


def test_the_second_ink_has_to_clear_the_bar_the_mix_is_walked_back_to_hold() -> None:
    """`#778495` is the `tx2` of all ten bundled templates and reads on none of them.

    3.81:1 on their white page, 3.34:1 on the `#F0F0F0` card, where `_LEGIBLE` asks
    4.5 on both -- so the model that used it 41 times in one deck was setting
    secondary copy nobody can read, and this is the one place that answer is no.
    """
    entry = theme_of(_scheme(_LIGHT_MAP, lt1="#FFFFFF", dk1="#000000", lt2="#F0F0F0", dk2="#778495", accent1="#155FFD"))

    assert _contrast("#778495", "#FFFFFF") < _LEGIBLE
    assert entry["muted"] == _receded("#000000", "#FFFFFF", str(entry["surface"]))


def test_a_second_ink_that_is_the_ink_recedes_from_nothing() -> None:
    """`tx2` within a percent of its own white ink: legible, and not a recession.

    Contrast alone passes it -- it is nearly the ink, so of course it reads -- which
    is why the second half of the comparison is against the recession the mix
    reaches rather than against a floor.
    """
    entry = theme_of(_scheme(_DARK_MAP, dk1="#000000", lt1="#FFFFFF", lt2="#FDFDFD", accent1="#5E31FF"))

    assert _contrast("#FDFDFD", "#000000") >= _LEGIBLE
    assert entry["muted"] != "#FDFDFD"
    assert entry["muted"] == _receded("#FFFFFF", "#000000", str(entry["surface"]))


def test_a_second_ink_that_is_quiet_and_legible_is_taken() -> None:
    """The window is not empty: a legible slate `tx2` is the author's muted.

    5.48:1 on the page and 4.81:1 on the card, both over 4.5, and quieter than the
    `#616161` the mix reaches at 6.19:1. A template that states a usable second ink
    gets to keep it.
    """
    entry = theme_of(_scheme(_LIGHT_MAP, lt1="#FFFFFF", dk1="#000000", lt2="#F0F0F0", dk2="#5A6B7C", accent1="#155FFD"))

    assert entry["muted"] == "#5A6B7C"


def test_the_second_accent_is_not_the_soft_form_of_the_first() -> None:
    """`accent_soft` stays mixed, because no slot states a quiet tile.

    `accent2` is the next series colour: measured across the bundled ten it runs up
    to 176 degrees off `accent1`'s hue (a blue accent whose "soft" form would be
    orange), it is the louder of the two against the ground on two of them, and it
    is `chart_series[1]` on all ten -- so a tile painted in it reads as data. The
    tile also has to be a ground `accent_ink` can be written on, which assumes the
    two share a hue.
    """
    entry = theme_of(_scheme(_LIGHT_MAP, lt1="#FFFFFF", dk1="#000000", accent1="#155FFD", accent2="#FDA211"))

    assert entry["accent_soft"] == _mixed("#155FFD", "#FFFFFF", _SOFT_MIX)
    assert entry["accent_soft"] != "#FDA211"


def test_no_scheme_slot_is_a_hairline_so_the_grid_stays_mixed() -> None:
    """A scheme has twelve slots and none of them is a rule.

    `hlink` and `folHlink` are the nearest thing and they are type colours: a
    visited link has to read as copy, which is the opposite contract to a hairline
    that must be findable and not a border. `folHlink` being grey on all ten bundled
    templates is one vendor's habit -- they share their `dk2` and `lt2` too -- and it
    tracks nothing: 1.84:1 off the ground on eight of them, 2.92:1 on two, where the
    mixed hairline sits at 2.46-2.68:1.
    """
    entry = theme_of(
        _scheme(_LIGHT_MAP, lt1="#FFFFFF", dk1="#000000", accent1="#155FFD", hlink="#564EF8", folHlink="#BFBFBF")
    )

    assert entry["grid"] == _mixed("#000000", "#FFFFFF", _GRID_MIX)
    assert entry["grid"] not in {"#BFBFBF", "#564EF8"}


@pytest.mark.parametrize("index", range(10))
def test_every_bundled_template_keeps_a_visible_plane_and_a_legible_muted(index: int) -> None:
    """The guarantees the derived palette carries, over every template that ships.

    The plane is the template's own colour and not a grey: `bg2` is stated as
    #F0F0F0 or #E7E6E6 by all sixteen and painted by none of them, so what the
    author gets is a tint of the accent, quieter than the tile that carries the
    page's answer. What the palette still owes is the pair it can honestly promise
    -- a plane you can see, and a second ink legible on the ground and on that plane
    -- and no more than that: which ink a card actually writes with is chosen against
    the card's own fill when it is drawn, so a palette naming a saturated plane is
    the drawing's problem to solve and not a palette this has to refuse.
    """
    pytest.importorskip("pptx", reason="python-pptx reads the bundled templates")
    from raven.ppt.services.template.defaults import default_template_catalog
    from raven.ppt.services.template.inventory import inspect_template

    inventory = inspect_template(default_template_catalog()[index].path)
    assert inventory is not None
    entry = theme_of(inventory)
    ground, surface, muted = (str(entry[key]) for key in ("background", "surface", "muted"))

    assert surface != ground
    assert _contrast(muted, ground) >= _LEGIBLE
    assert _contrast(muted, surface) >= _LEGIBLE
    assert surface not in {"#F0F0F0", "#E7E6E6", "#E6E6E6"}
    assert _contrast(surface, ground) < _contrast(str(entry["accent_soft"]), ground)
