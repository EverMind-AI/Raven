"""The two modules a build script imports, exercised as the script would.

These are generated source, so the only test worth writing runs them: the files
are written into a directory, imported, and used to draw. A test that only
compared strings would pass for a module with a NameError in it, and the author
would find that out in a failed build with the error attributed to its own
program.

Writing the files is deliberately done here rather than by the assets service --
the service returns filename and text, the script backend owns the directory --
so this test is also the check on that contract: everything needed to make a
working build directory has to come out of `script_helper_files()`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.ppt.services.assets import icons
from raven.ppt.services.assets.script_helpers import (
    ICON_DATA_FILENAME,
    ICON_MODULE_FILENAME,
    THEME_DATA_FILENAME,
    THEME_MODULE_FILENAME,
    icon_catalog_json,
    icon_module_source,
    icon_summary,
    script_helper_files,
    theme_catalog,
    theme_catalog_json,
    theme_module_source,
    theme_summary,
)
from raven.ppt.services.assets.themes import THEMES

pytest.importorskip("pptx", reason="ppt extra not installed")


def _install(directory: Path) -> None:
    """What the script backend will do; done here to prove that is all it takes."""
    for filename, text in script_helper_files().items():
        (directory / filename).write_text(text, encoding="utf-8")


def _load_module(directory: Path, filename: str) -> dict:
    """Run one generated module the way an import in the build directory would."""
    path = directory / filename
    namespace: dict = {"__file__": str(path), "__name__": path.stem}
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)  # noqa: S102
    return namespace


def test_the_set_of_files_is_exactly_what_a_build_directory_needs() -> None:
    from raven.ppt.services.assets.layout import LAYOUT_MODULE_FILENAME

    files = script_helper_files()
    assert set(files) == {
        THEME_MODULE_FILENAME,
        THEME_DATA_FILENAME,
        ICON_MODULE_FILENAME,
        ICON_DATA_FILENAME,
        LAYOUT_MODULE_FILENAME,
    }
    assert all(text for text in files.values())
    # The module names are the contract: the script writes `import ppt_theme`.
    assert THEME_MODULE_FILENAME == "ppt_theme.py"
    assert ICON_MODULE_FILENAME == "ppt_icons.py"
    assert LAYOUT_MODULE_FILENAME == "ppt_layout.py"


def test_nothing_generated_reaches_back_into_raven() -> None:
    """The build directory has to run as a plain python-pptx project.

    A helper that imported raven would bind the author's script to internals it
    cannot see, and would break the moment those move -- which is the whole
    reason this refactor exists.
    """
    for source in (theme_module_source(), icon_module_source()):
        assert "raven" not in source
        assert "import raven" not in source


def test_the_theme_module_exposes_the_themes_and_a_colour_converter(tmp_path: Path) -> None:
    _install(tmp_path)
    module = _load_module(tmp_path, THEME_MODULE_FILENAME)

    assert set(module["THEMES"]) == set(THEMES)
    assert module["THEME_NAMES"] == sorted(THEMES)

    from pptx.dml.color import RGBColor

    rgb = module["rgb"]
    assert rgb("#0B5FA5") == RGBColor(0x0B, 0x5F, 0xA5)
    # Both spellings, because a theme value arrives with the hash on it.
    assert rgb("0B5FA5") == rgb("#0B5FA5")
    # And an RGBColor passes through, so wrapping twice is not an error.
    assert rgb(rgb("#0B5FA5")) == rgb("#0B5FA5")

    accent = module["THEMES"]["ink-graphite"]["accent"]
    assert accent == THEMES["ink-graphite"].accent
    assert rgb(accent) == RGBColor(0x0B, 0x5F, 0xA5)


def test_the_exported_catalog_carries_paints_and_no_type_scale() -> None:
    catalog = theme_catalog()
    assert set(catalog) == set(THEMES)
    for theme_id, entry in catalog.items():
        assert set(entry) == {
            "cjk_font_family",
            "background",
            "surface",
            "foreground",
            "muted",
            "accent",
            "accent_soft",
            "grid",
            "chart_series",
            "font_family",
        }
        assert entry["background"] == "#FFFFFF"
        assert entry["chart_series"] == list(THEMES[theme_id].chart_series)
        assert len(entry["chart_series"]) >= 6
        # JSON-shaped: lists rather than tuples, or the dump changes shape.
        assert isinstance(entry["chart_series"], list)
    # The on-dark tokens and the engine's decoration dimensions stay behind.
    assert all("background_dark" not in entry for entry in catalog.values())
    assert all("decor_language" not in entry for entry in catalog.values())
    assert json.loads(theme_catalog_json()) == catalog


def test_the_icon_module_draws_a_real_icon_onto_a_real_slide(tmp_path: Path) -> None:
    _install(tmp_path)
    module = _load_module(tmp_path, ICON_MODULE_FILENAME)

    from pptx import Presentation
    from pptx.util import Inches

    assert module["ICON_NAMES"] == list(icons.icon_names())

    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    before = len(slide.shapes)
    shapes = module["add_icon"](slide, "target", Inches(1), Inches(1), Inches(0.5), "#0B5FA5")

    assert shapes
    assert len(slide.shapes) == before + len(shapes)
    for shape in shapes:
        assert shape.line.color.rgb == module["_line_color"]("#0B5FA5")


def test_add_icon_takes_a_theme_colour_exactly_as_ppt_theme_hands_it_over(tmp_path: Path) -> None:
    """The two helpers sit side by side, so a hex string has to be accepted.

    `themes.json` stores '#RRGGBB'; requiring `rgb()` in between is a step an
    author gets wrong once and then avoids the icon call entirely.
    """
    _install(tmp_path)
    theme_module = _load_module(tmp_path, THEME_MODULE_FILENAME)
    icon_module = _load_module(tmp_path, ICON_MODULE_FILENAME)

    from pptx import Presentation
    from pptx.util import Inches

    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    accent = theme_module["THEMES"]["warm-paper"]["accent"]

    as_string = icon_module["add_icon"](slide, "shield", Inches(1), Inches(1), Inches(0.4), accent)
    as_color = icon_module["add_icon"](slide, "shield", Inches(3), Inches(1), Inches(0.4), theme_module["rgb"](accent))

    assert as_string and as_color
    assert {shape.line.color.rgb for shape in as_string} == {shape.line.color.rgb for shape in as_color}


def test_the_icon_module_answers_a_miss_the_same_way_the_service_does(tmp_path: Path) -> None:
    """The generated copy cannot import the service, so a test pins the agreement.

    Duplicated on purpose -- the build directory has to stand alone -- which
    means the only thing holding the two in step is this table.
    """
    _install(tmp_path)
    module = _load_module(tmp_path, ICON_MODULE_FILENAME)
    find_icons, add_icon = module["find_icons"], module["add_icon"]

    from pptx import Presentation
    from pptx.util import Inches

    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])

    for attempt in ("arrows-move", "click", "task-check", "chart_bars", "category", "switch-horizontal", "wand"):
        assert find_icons(attempt) == icons.icon_candidates(attempt), attempt
        with pytest.raises(LookupError) as caught:
            add_icon(slide, attempt, Inches(1), Inches(1), Inches(0.4), "#000000")
        assert repr(attempt) in str(caught.value)
        for candidate in icons.icon_candidates(attempt):
            assert candidate in str(caught.value)

    # And the forgiving spellings resolve rather than raise.
    assert add_icon(slide, "map-pin", Inches(1), Inches(2), Inches(0.4), "#000000")
    assert add_icon(slide, "TREND UP", Inches(2), Inches(2), Inches(0.4), "#000000")


def test_the_icon_data_is_what_the_module_reads(tmp_path: Path) -> None:
    _install(tmp_path)
    written = json.loads((tmp_path / ICON_DATA_FILENAME).read_text(encoding="utf-8"))
    assert written == json.loads(icon_catalog_json())
    assert set(written) == set(icons.icon_names())


def test_the_summaries_name_everything_available() -> None:
    """A caller that has to describe the assets in a prompt cannot omit one: an
    asset the model is never told about is one it never uses."""
    summary = theme_summary()
    for theme_id in THEMES:
        assert theme_id in summary

    catalog = icon_summary()
    for name in icons.icon_names():
        assert name in catalog
    assert str(len(icons.icon_names())) in catalog
    assert "find_icons" in catalog


def test_align_takes_the_enum_a_python_pptx_author_reaches_for(tmp_path) -> None:
    """`align="right"` is what these helpers take; `align=PP_ALIGN.RIGHT` is what
    somebody writing python-pptx writes. One design pass wrote the second, the lookup
    raised `KeyError: <PP_PARAGRAPH_ALIGNMENT.RIGHT: 3>` in the block that drew page 3,
    the round was reverted, and a deck lost the whole pass over a spelling.
    """
    import sys

    from pptx import Presentation
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN

    from raven.ppt.services.assets.layout import layout_module_source

    (tmp_path / "ppt_layout.py").write_text(layout_module_source(), encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    try:
        module = __import__("ppt_layout")
        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        module.write(slide, module.Box(1, 1, 4, 2), "enum", align=PP_ALIGN.RIGHT, anchor=MSO_ANCHOR.BOTTOM)
        module.write(slide, module.Box(1, 3, 4, 4), "string", align="center", anchor="middle")
        with pytest.raises(ValueError, match="left, center, right"):
            module.write(slide, module.Box(1, 5, 4, 6), "neither", align="diagonal")
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("ppt_layout", None)


def _layout(tmp_path: Path):
    """The generated module, imported the way a build script imports it."""
    import sys

    from raven.ppt.services.assets.layout import layout_module_source
    from raven.ppt.services.assets.script_helpers import script_helper_files

    for name, text in script_helper_files().items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    (tmp_path / "ppt_layout.py").write_text(layout_module_source(), encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    return sys.path, __import__("ppt_layout")


def _drop(path, module_names=("ppt_layout", "ppt_icons", "ppt_theme")) -> None:
    import sys

    sys.path.remove(str(path))
    for name in module_names:
        sys.modules.pop(name, None)


def test_a_formula_keeps_its_symbols_whole_and_its_subscripts_down(tmp_path) -> None:
    """What "掩码 logits = (F4, Q'inst)" was missing on a delivered page.

    Written with `write` in a 3.9in column it wrapped after "分类", splitting a clause
    across two lines, and every subscript in it was flat -- F4 for F-sub-4. The three
    things this asserts are the three things the render showed wrong.
    """
    from pptx import Presentation

    _, module = _layout(tmp_path)
    try:
        from ppt_theme import THEMES

        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        frame = module.formula(
            slide,
            module.Box(0.7, 2.0, 4.6, 2.6),
            "掩码 logits = (F_4, Q'_{inst})",
            THEMES["ink-graphite"],
            size=16,
        )

        assert frame.word_wrap is False, "a formula that wraps breaks inside a symbol"
        runs = [run for para in frame.paragraphs for run in para.runs]
        subscripts = [run for run in runs if run.font._rPr.get("baseline") == "-25000"]
        assert [run.text for run in subscripts] == ["4", "inst"]
        # The same size as the line, deliberately: a renderer shrinks a lowered run
        # on its own, and shrinking it here too multiplies the two. An 18pt formula
        # that asked for 13pt subscripts rendered them at 7.5pt -- four tenths of the
        # line, unreadable in a room.
        assert all(run.font.size == module.Pt(16) for run in subscripts)
        italic = {run.text for run in runs if run.font.italic}
        assert italic == {"F", "Q"}, f"a variable is italic and a function name is not: {italic}"
        assert not any(run.font.italic for run in runs if run.text.startswith("logits"))
    finally:
        _drop(tmp_path)


def test_a_formula_too_wide_for_the_floor_breaks_at_its_own_separators(tmp_path) -> None:
    """The one place a break does not land inside a symbol."""
    from pptx import Presentation

    _, module = _layout(tmp_path)
    try:
        from ppt_theme import THEMES

        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        long = "掩码 logits = (F_4, Q'_{inst})；分类 logits = (Q'_{inst}, concat(Q'_{sem}, Q'_{bg}))"
        wide = module.formula(slide, module.Box(0.7, 2.0, 12.6, 2.6), long, THEMES["ink-graphite"], size=16)
        narrow = module.formula(slide, module.Box(0.7, 3.0, 4.6, 3.6), long, THEMES["ink-graphite"], size=16)

        assert len(wide.paragraphs) == 1, "a formula that fits is one line"
        assert len(narrow.paragraphs) == 2, "and one that cannot is split at its semicolon"
        assert narrow.paragraphs[1].runs[0].text.startswith("分类")
    finally:
        _drop(tmp_path)


def test_a_card_puts_an_icon_beside_its_title(tmp_path) -> None:
    """Three live decks drew cards by hand and not one of them used an icon, so 180
    icons shipped unused. The helper that makes a card easy makes the icon the
    default -- and the geometry is its business, not the author's.
    """
    from pptx import Presentation

    _, module = _layout(tmp_path)
    try:
        from ppt_theme import THEMES

        presentation = Presentation()
        presentation.slide_width, presentation.slide_height = module.Inches(13.333), module.Inches(7.5)
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        box = module.Box(0.7, 1.6, 4.6, 3.4)
        module.card(slide, box, THEMES["ink-graphite"], icon="target", title="语义查询是必要的", body="去掉后全线下降")

        boxes = [
            (shape.left / 914400, shape.top / 914400, shape.width / 914400, shape.height / 914400)
            for shape in slide.shapes
        ]
        assert all(
            left >= box.x0 - 0.01 and left + width <= box.x1 + 0.01 and top >= box.y0 - 0.01
            for left, top, width, height in boxes
        ), f"a card drew outside its own box: {boxes}"
        from pptx.enum.shapes import MSO_SHAPE_TYPE

        drawn = [shape for shape in slide.shapes if shape.shape_type == MSO_SHAPE_TYPE.FREEFORM]
        assert drawn, "the icon did not reach the card"
        # And the title sits to the right of it, not on top of it.
        icon_right = max(shape.left + shape.width for shape in drawn) / 914400
        titles = [s for s in slide.shapes if getattr(s, "has_text_frame", False) and "语义" in s.text_frame.text]
        assert titles and titles[0].left / 914400 >= icon_right - 0.02
    finally:
        _drop(tmp_path)


def test_a_stack_hands_out_bands_without_arithmetic(tmp_path) -> None:
    """What it replaces: a live run wrote `Box(x0, 5.34, x1, 6.72)`, looked, tried
    5.50, looked, tried 4.86, and did the same on two later pages -- five requests at
    about half a dollar each, spent finding where the previous band ended.
    """
    _, module = _layout(tmp_path)
    try:
        region = module.Box(0.72, 1.66, 12.61, 6.80)
        down = module.stack(region, gutter=0.20)

        first = down.take(1.0)
        second = down.take(2.0)
        rest = down.rest()

        assert (first.y0, first.y1) == pytest.approx((1.66, 2.66))
        assert second.y0 == pytest.approx(2.86), "the gutter sits between bands, not inside them"
        assert second.y1 == pytest.approx(4.86)
        assert (rest.y0, rest.y1) == pytest.approx((5.06, 6.80))
        assert first.x0 == second.x0 == rest.x0 == region.x0
        assert down.left == pytest.approx(0)
    finally:
        _drop(tmp_path)


def test_a_stack_says_so_rather_than_overlapping(tmp_path) -> None:
    _, module = _layout(tmp_path)
    try:
        down = module.stack(module.Box(0.72, 1.66, 12.61, 3.66))
        down.take(1.5)

        with pytest.raises(ValueError, match="0.22in is left"):
            down.take(2.0)
        with pytest.raises(ValueError):
            module.stack(module.Box(0, 0, 1, 1)).take(0)
    finally:
        _drop(tmp_path)


@pytest.mark.parametrize(("pixels", "wide"), [((1600, 400), True), ((400, 1600), False)])
def test_a_picture_fits_its_box_whole_and_centred(tmp_path, pixels, wide) -> None:
    """Whichever dimension runs out first. `add_picture` scales the other from the
    one it is given, so which to give depends on the image -- a live run wrote that
    itself and reached into `slide.shapes._spTree` to do it.
    """
    from PIL import Image
    from pptx import Presentation

    photo = tmp_path / "figure.png"
    Image.new("RGB", pixels, (120, 140, 160)).save(photo)

    _, module = _layout(tmp_path)
    try:
        from ppt_theme import THEMES

        presentation = Presentation()
        presentation.slide_width, presentation.slide_height = module.Inches(13.333), module.Inches(7.5)
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        box = module.Box(1.0, 1.5, 7.0, 5.0)
        shape = module.picture_fit(slide, photo, box, THEMES["ink-graphite"], caption="Figure 2：架构")

        unit = module.Inches(1)
        left, top = shape.left / unit, shape.top / unit
        width, height = shape.width / unit, shape.height / unit
        assert left >= box.x0 - 0.01 and left + width <= box.x1 + 0.01
        assert top >= box.y0 - 0.01 and top + height <= box.y1 + 0.01
        # The aspect is the image's, not the box's.
        assert abs(width / height - pixels[0] / pixels[1]) < 0.02
        # Centred in the room left over once the caption has its line.
        assert abs((left - box.x0) - (box.x1 - left - width)) < 0.02
        assert (width >= box.w - 0.02) if wide else (height > box.h * 0.5)
        said = [s.text_frame.text for s in slide.shapes if getattr(s, "has_text_frame", False)]
        assert "Figure 2：架构" in said
    finally:
        _drop(tmp_path)


def test_a_table_sizes_its_columns_from_what_they_hold(tmp_path) -> None:
    """Equal columns are why a live run spent thirteen requests on one page: it
    shortened "DAVIS J&F" to "DAVIS", tried five hand-written weights, rebuilt,
    shortened another header, and rebuilt again.
    """
    from pptx import Presentation

    _, module = _layout(tmp_path)
    try:
        from ppt_theme import THEMES

        presentation = Presentation()
        presentation.slide_width, presentation.slide_height = module.Inches(13.333), module.Inches(7.5)
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        box = module.Box(0.72, 1.6, 12.61, 3.6)
        rows = [["方法", "AP"], ["TarViS (Swin-L) 联合训练", "60.2"], ["VITA", "57.5"]]

        table = module.table(slide, box, rows, THEMES["ink-graphite"], numeric_from=1)
        unit = module.Inches(1)
        widths = [column.width / unit for column in table.columns]

        assert widths[0] > widths[1] * 2, f"the名字 column holds four times the text: {widths}"
        # A table narrower than its box stays narrow rather than being stretched.
        assert sum(widths) < box.w - 1.0

        given = module.table(slide, box, rows, THEMES["ink-graphite"], weights=(1, 1), numeric_from=1)
        filled = [column.width / unit for column in given.columns]
        assert abs(sum(filled) - box.w) < 0.02, "given weights are proportions and fill the box"
        assert abs(filled[0] - filled[1]) < 0.02
    finally:
        _drop(tmp_path)


def test_a_box_can_be_given_as_a_corner_and_a_size(tmp_path) -> None:
    """`Box` takes two corners and every python-pptx call beside it takes a size, so
    a script holds both conventions at once. A live run wrote `Box(x, y, w, h)`
    throughout, read the render, and then "corrected" its own
    `box_shape(slide, x, y, w, h)` helper into corners as well -- turning a 3.75in
    box into a 12.4in one that left the page. `Box.at` is the spelling that does not
    need the author to switch conventions mid-line.
    """
    import sys

    from raven.ppt.services.assets.layout import layout_module_source

    (tmp_path / "ppt_layout.py").write_text(layout_module_source(), encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    try:
        module = __import__("ppt_layout")
        sized = module.Box.at(0.82, 1.5, 3.75, 5.25)
        assert sized == module.Box(0.82, 1.5, 4.57, 6.75)
        assert (round(sized.w, 2), round(sized.h, 2)) == (3.75, 5.25)
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("ppt_layout", None)


def test_given_weights_still_leave_room_for_the_header_words(tmp_path) -> None:
    """Weights say which column deserves the room; they cannot know what row one
    measures. A live deck passed weights that fit its numbers and starved its
    headings, and shipped a table reading "VIPSe / g", "DAVI / S", "BURS / T".
    """
    import sys

    from raven.ppt.services.assets.layout import layout_module_source

    (tmp_path / "ppt_layout.py").write_text(layout_module_source(), encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    try:
        module = __import__("ppt_layout")
        header = ["设置", "YTVIS", "OVIS", "KITTI", "VIPSeg", "DAVIS", "BURST"]
        weights = [1.65, 0.95, 0.95, 0.95, 1.0, 0.95, 0.95]
        size, table_width = 13.5, 7.2

        floors = module._header_floors(header, size)
        naive = [table_width * weight / sum(weights) for weight in weights]
        widths = module._column_widths(weights, table_width, floors)

        assert any(width < floor for width, floor in zip(naive, floors)), "the case must still bite"
        for name, width, floor in zip(header, widths, floors):
            assert width >= floor - 1e-9, f"{name} is narrower than its own heading"
        assert round(sum(widths), 6) == round(table_width, 6), "and the table keeps its width"
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("ppt_layout", None)


def test_a_cjk_header_sets_no_floor_of_its_own(tmp_path) -> None:
    """A Chinese heading wraps between any two characters, so it never needs a column
    held open for it -- only unbreakable Latin runs do.
    """
    import sys

    from raven.ppt.services.assets.layout import layout_module_source

    (tmp_path / "ppt_layout.py").write_text(layout_module_source(), encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    try:
        module = __import__("ppt_layout")
        assert module._unbroken("任务定义不同") == ""
        assert module._unbroken("VIPSeg 数据集") == "VIPSeg"
        assert module._unbroken("DAVIS J&F") == "DAVIS"
        cjk, latin = module._header_floors(["任务定义不同", "VIPSeg"], 13.5)
        assert cjk < latin
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("ppt_layout", None)
