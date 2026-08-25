"""Which two faces a bound template hands the author, and where each comes from.

Both defects here showed on the same Chinese template, whose scheme names
`('Arial', '微软雅黑')`: the Latin face came back as the Han one, and the CJK key
the authoring documents all tell an author to read was not in the theme at all.
"""

from __future__ import annotations

from pathlib import Path

from raven.ppt.services.template.inventory import TemplateInventory
from raven.ppt.services.template.theme import _faces, theme_of


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
