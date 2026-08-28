"""The fifteen charts, drawn and then read back off the slide.

`ppt_charts` is generated source: it reaches the author as a file in the build
directory, so the only test worth writing installs it, imports it and draws with
it. A test that compared strings would pass for a module with a NameError in it
and the author would meet the error in a failed build, attributed to its own
program.

What is asserted is what a chart can be wrong about and a render cannot show. A
bar out of proportion to its value looks like a bar; a scale that quietly starts
above zero looks like a chart; two labels a tenth of an inch apart look fine at
review size and are unreadable in the room. Those are measured here. So is the
one class of defect this route has shipped before -- geometry escaping the region
it was given -- because a chart that overflows its box lands on whatever the page
put beside it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pptx", reason="ppt extra not installed")

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE
from pptx.util import Inches

from raven.ppt.services.assets.script_helpers import script_helper_files
from raven.ppt.services.gates.bands import data_mark_ids
from raven.ppt.services.measure.type_size import MIN_FLOOR_PT

_GENERATED = ("ppt_charts", "ppt_layout", "ppt_icons", "ppt_theme")
_THEME_ID = "indigo-scholar"
# Thinner than this is a rule the charts draw -- an axis, a leader -- and not a part.
MIN_PART_IN = 0.05


@pytest.fixture
def charts(tmp_path: Path):
    """The generated module, installed and imported the way a build script does."""
    for name, text in script_helper_files().items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    for name in _GENERATED:
        sys.modules.pop(name, None)
    try:
        yield __import__("ppt_charts")
    finally:
        sys.path.remove(str(tmp_path))
        for name in _GENERATED:
            sys.modules.pop(name, None)


def _drawn_charts() -> set[str]:
    """Every chart `ppt_charts` defines, off the source, so a new one fails a test."""
    from raven.ppt.services.assets.charts import chart_names

    return set(chart_names())


@pytest.fixture
def theme() -> dict:
    from raven.ppt.services.assets.script_helpers import theme_catalog

    return theme_catalog()[_THEME_ID]


@pytest.fixture
def slide():
    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    return presentation.slides.add_slide(presentation.slide_layouts[6])


def _box(charts, x0=0.72, y0=1.0, x1=12.61, y1=6.5):
    return charts.Box(x0, y0, x1, y1)


def _rect(shape) -> tuple[float, float, float, float] | None:
    """(x0, y0, x1, y1) in inches for a filled rectangle, or None for anything else."""
    if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
        return None
    try:
        if shape.auto_shape_type != MSO_SHAPE.RECTANGLE:
            return None
    except (AttributeError, ValueError):
        return None
    unit = 914400
    return (shape.left / unit, shape.top / unit, (shape.left + shape.width) / unit, (shape.top + shape.height) / unit)


def _bars(slide) -> list[tuple[float, float, float, float]]:
    return [box for box in (_rect(shape) for shape in slide.shapes) if box is not None]


def _labels(slide) -> list[tuple[str, tuple[float, float, float, float], float, str]]:
    """(text, box, size in points, colour) for every run the chart wrote."""
    unit = 914400
    found = []
    for shape in slide.shapes:
        if not getattr(shape, "has_text_frame", False) or not shape.text_frame.text.strip():
            continue
        box = (
            shape.left / unit,
            shape.top / unit,
            (shape.left + shape.width) / unit,
            (shape.top + shape.height) / unit,
        )
        for para in shape.text_frame.paragraphs:
            for run in para.runs:
                colour = str(run.font.color.rgb) if run.font.color and run.font.color.type is not None else ""
                found.append((run.text, box, run.font.size.pt, f"#{colour}"))
    return found


def _oval(shape) -> bool:
    try:
        return shape.auto_shape_type == MSO_SHAPE.OVAL
    except (AttributeError, ValueError):
        return False


def _marks(slide) -> list[tuple[float, bool]]:
    """(diameter in inches, whether it is filled) for every circular mark drawn."""
    from pptx.enum.dml import MSO_FILL

    found = []
    for shape in slide.shapes:
        try:
            if shape.auto_shape_type != MSO_SHAPE.OVAL:
                continue
        except (AttributeError, ValueError):
            continue
        found.append((shape.width / 914400, shape.fill.type != MSO_FILL.BACKGROUND))
    return found


def _apart(one, other, tolerance=0.02) -> bool:
    """Whether two boxes miss each other.

    Measured with a couple of hundredths of slack because what has to stay apart
    is the words rather than the boxes: `write` keeps about 0.03in of margin on
    every side of the line it sets, so two boxes that graze still leave the type
    in them clearly separated.
    """
    return (
        one[2] <= other[0] + tolerance
        or other[2] <= one[0] + tolerance
        or one[3] <= other[1] + tolerance
        or other[3] <= one[1] + tolerance
    )


def _discs(slide) -> list[tuple[float, float, float, float]]:
    """(x0, y0, x1, y1) in inches for every circular mark drawn."""
    unit = 914400
    found = []
    for shape in slide.shapes:
        if not _oval(shape):
            continue
        found.append(
            (
                shape.left / unit,
                shape.top / unit,
                (shape.left + shape.width) / unit,
                (shape.top + shape.height) / unit,
            )
        )
    return found


def _type_in(charts, shape, theme) -> tuple[float, float, float, float]:
    """The part of a drawn text shape the line in it actually covers.

    Every other label measured here is written into a box its own size, so the
    shape is the measurement. A quadrant name is not: it is written into a whole
    quarter of the plot and set in one corner of it, so its shape says nothing
    about where the type landed, and `_apart` on that shape passes whatever the
    render shows. `_ink` is the module's own answer to which part of a box the word
    covers -- asked here with the alignment read back off the drawn shape rather
    than assumed, so what is asserted is the placement and not the arithmetic.
    """
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN

    across = {PP_ALIGN.LEFT: "left", PP_ALIGN.CENTER: "center", PP_ALIGN.RIGHT: "right", None: "left"}
    down = {MSO_ANCHOR.TOP: "top", MSO_ANCHOR.MIDDLE: "middle", MSO_ANCHOR.BOTTOM: "bottom", None: "top"}
    unit = 914400
    box = charts.Box(
        shape.left / unit, shape.top / unit, (shape.left + shape.width) / unit, (shape.top + shape.height) / unit
    )
    para = shape.text_frame.paragraphs[0]
    run = para.runs[0]
    ink = charts._ink(
        box,
        run.text,
        run.font.size.pt,
        across[para.alignment],
        down[shape.text_frame.vertical_anchor],
        charts.type_face(theme),
    )
    return tuple(ink)


def _between(paint: str, one: str, other: str, tolerance: int = 1) -> bool:
    """Whether `paint` sits on the straight line between two colours."""
    read = [
        tuple(int(value.lstrip("#")[index : index + 2], 16) for value in (paint, one, other)) for index in (0, 2, 4)
    ]
    # Solve for the position on the line using whichever channel moves furthest,
    # then check the other two agree: one channel alone matches far too much.
    found, start, end = max(read, key=lambda channel: abs(channel[2] - channel[1]))
    if start == end:
        return all(channel[0] == channel[1] for channel in read)
    where = (found - start) / (end - start)
    if not -0.001 <= where <= 1.001:
        return False
    return all(abs(value - (low + (high - low) * where)) <= tolerance for value, low, high in read)


def _every_shape(slide) -> list[tuple[float, float, float, float]]:
    unit = 914400
    return [
        (
            shape.left / unit,
            shape.top / unit,
            (shape.left + shape.width) / unit,
            (shape.top + shape.height) / unit,
        )
        for shape in slide.shapes
        if shape.width and shape.height
    ]


def _escapes(slide, box, tolerance=0.01) -> list[tuple[float, float, float, float]]:
    """Every shape on the slide that is not inside `box`.

    The one class of defect a render cannot show you, because what a chart lands on
    when it overflows is whatever the page put beside it -- and the page put that
    there on the strength of the box it handed over. So this is the assertion every
    chart is held to, in one place: `assert not _escapes(slide, box)`.

    The hundredth of an inch is the same slack `_apart` allows, and for the same
    reason: a rule drawn on the boundary is on the boundary, and a hairline whose
    thickness rounds a thousandth past it has not escaped anything.
    """
    return [
        where
        for where in _every_shape(slide)
        if where[0] < box.x0 - tolerance
        or where[1] < box.y0 - tolerance
        or where[2] > box.x1 + tolerance
        or where[3] > box.y1 + tolerance
    ]


def _draw_one_of_each(charts, slide, theme, box) -> None:
    """Every chart in the module, into the same region, one after another.

    Deliberately all in one box: what this is for is the assertion that a chart
    fills what it was given and nothing else, and one box per chart would be one
    more chance each for the test to be measuring the wrong one.
    """
    charts.column(slide, box, theme, [("East", 185), ("South", 142), ("North", 128)], accent="East", unit="M")
    charts.horizontal_bar(slide, box, theme, {"TarViS": 48.3, "VITA": 45.7, "IDOL": 43.9}, accent=0)
    charts.grouped_bar(slide, box, theme, ["Q1", "Q2"], [("A", [120, 145]), ("B", [85, 98])], accent="A")
    charts.stacked_bar(slide, box, theme, ["FY24", "FY25"], {"Licence": [140, 160], "Service": [90, 100]})
    charts.stacked_bar(slide, box, theme, ["FY25"], {"Licence": [160], "Service": [100]}, share=True, direction="bar")
    charts.progress_bar(slide, box, theme, [("Single sign-on", "88%"), ("Audit trail", 0.62)], accent=0)
    charts.bullet(slide, box, theme, [("Revenue", 182, 200), ("Retention", 92, 90)], accent="Retention")
    charts.butterfly(slide, box, theme, [("18-24", 320, 280), ("25-34", 460, 510)], sides=("Cost", "Revenue"))
    charts.dumbbell(slide, box, theme, [("Checkout", 61, 78), ("Search", 44, 71)], sides=("Before", "After"), unit="%")
    charts.waterfall(slide, box, theme, [("Open", 280), ("Growth", 120), ("Cost", -45), ("Close", 355)], totals=(0, -1))
    charts.pareto(slide, box, theme, [("Function", 350), ("Finish", 220), ("Missing", 150)], accent=0)
    charts.histogram(slide, box, theme, [12, 15, 18, 22, 25, 31, 38, 44, 52, 61, 72, 80])
    charts.gantt(slide, box, theme, [("Discovery", "2026-01-06", "2026-02-14"), ("Build", "2026-02-01", "2026-04-20")])
    charts.heatmap(slide, box, theme, ["North", "South"], ["Q1", "Q2", "Q3"], [[12, 18, 24], [9, 11, 14]])
    charts.matrix_2x2(
        slide,
        box,
        theme,
        [("SSO", 8, 3), ("Audit", 6, 7)],
        axes=("Effort", "Impact"),
        quadrants=("Quick wins", "Big bets", "Fill-ins", "Money pits"),
        accent="Audit",
        limits=(0, 10, 0, 10),
    )
    charts.scatter(
        slide,
        box,
        theme,
        [("Online", 48, 420, 28), ("Offline", 52, 410, 12)],
        axes=("Spend", "Revenue"),
        accent="Online",
    )
    charts.dot_plot(slide, box, theme, ["LoCoMo", "LongMemEval"], {"Platform": [92.5, 94.4], "Open": [88.1, 83.0]})
    charts.dot_plot(slide, box, theme, ["Homepage", "Blog", "Repo"], {"LoCoMo": [93.05, 92.3, 92.5]}, unit="%")
    charts.line(slide, box, theme, ["Q1", "Q2", "Q3"], {"Calls": [88, 124, 186]}, unit="M")
    charts.line(
        slide,
        box,
        theme,
        ["1M", "5M", "10M"],
        {"Platform": [64.1, 55.2, 48.6], "Local": [66.3, 61.0, 57.4]},
        accent="Local",
    )
    charts.combo(
        slide,
        box,
        theme,
        [("Q1", 88), ("Q2", 124), ("Q3", 186)],
        [44, 41, 27],
        sides=("Calls", "Growth"),
        curve_unit="%",
    )
    charts.funnel(slide, box, theme, [("Visited", 120000), ("Installed", 42000), ("Paid", 1450)], accent="Paid")
    charts.milestone(slide, box, theme, [("Seed", "2024-05-14"), ("Series A", "2025-09-09")], accent="Series A")
    charts.box_plot(
        slide, box, theme, [("Platform", [180, 210, 248, 275, 340, 520]), ("Local", [210, 240, 268, 295, 330])]
    )
    charts.treemap(slide, box, theme, [("Downloads", 1400), ("Stars", 410), ("Calls", 186), ("Frameworks", 24)])
    charts.marimekko(
        slide,
        box,
        theme,
        ["Personal", "Enterprise"],
        {"Mem0": [640, 310], "EverOS": [160, 90]},
        accent="EverOS",
    )


def test_a_column_is_exactly_as_long_as_the_value_it_stands_for(charts, slide, theme) -> None:
    """The one thing a render cannot show you.

    A bar 8% short of what its number says looks like a bar, reads as a bar, and
    passes every visual review the deck gets -- and it is the failure the whole
    module exists to make impossible, because it is what an author computing an
    axis per page produces. So it is measured off the drawn geometry.
    """
    box = _box(charts, y1=5.0)
    charts.column(slide, box, theme, [("A", 200), ("B", 150), ("C", 50)], unit="")

    bars = sorted((b for b in _bars(slide) if b[3] - b[1] > 0.2), key=lambda b: b[0])
    assert len(bars) == 3
    heights = [bar[3] - bar[1] for bar in bars]
    assert heights[1] / heights[0] == pytest.approx(150 / 200, rel=0.01)
    assert heights[2] / heights[0] == pytest.approx(50 / 200, rel=0.01)
    # And they stand on one baseline, which is what makes the lengths comparable.
    assert len({round(bar[3], 4) for bar in bars}) == 1


def test_a_scale_contains_zero_even_when_every_value_is_far_from_it(charts, slide, theme) -> None:
    """96, 98 and 100 drawn against a scale starting at 96 is a chart that says one
    of them is nothing. Three near-identical readings have to look near-identical."""
    box = _box(charts, y1=5.0)
    charts.column(slide, box, theme, [("A", 100), ("B", 98), ("C", 96)])

    bars = sorted((b for b in _bars(slide) if b[3] - b[1] > 0.2), key=lambda b: b[0])
    heights = [bar[3] - bar[1] for bar in bars]
    assert heights[2] / heights[0] == pytest.approx(0.96, rel=0.01)


def test_axis_max_raises_the_top_of_a_scale_and_never_cuts_a_bar_short(charts, slide, theme) -> None:
    """Two charts on one page have to be read against each other, which is what
    `axis_max` is for -- and a maximum under the data would truncate rather than
    scale, so it is clamped instead of trusted."""
    tall = Presentation()
    tall.slide_width, tall.slide_height = Inches(13.333), Inches(7.5)
    second = tall.slides.add_slide(tall.slide_layouts[6])
    box = _box(charts, y1=5.0)

    charts.column(slide, box, theme, [("A", 100), ("B", 50)], axis_max=200)
    charts.column(second, box, theme, [("A", 100), ("B", 50)], axis_max=10)

    raised = max(bar[3] - bar[1] for bar in _bars(slide))
    ignored = max(bar[3] - bar[1] for bar in _bars(second))
    assert raised < ignored, "axis_max=200 should halve the tallest bar"
    assert ignored == pytest.approx(max(bar[3] - bar[1] for bar in _bars(second)))


def test_the_one_item_that_carries_the_claim_is_the_only_one_in_the_accent(charts, slide, theme) -> None:
    """ "Only emphasise one" is a rule prose could not hold: three delivered decks
    coloured every bar. Naming the item is the whole interface, and everything else
    goes quiet by construction rather than by the author's restraint."""
    box = _box(charts, y1=5.0)
    charts.column(slide, box, theme, [("A", 200), ("B", 150), ("C", 50)], accent="A")

    fills = [
        str(shape.fill.fore_color.rgb)
        for shape in slide.shapes
        if _rect(shape) and (_rect(shape)[3] - _rect(shape)[1]) > 0.2
    ]
    accent = theme["accent"].lstrip("#").upper()
    assert fills.count(accent) == 1
    assert set(fills) == {accent, theme["muted"].lstrip("#").upper()}


def test_an_accent_that_names_nothing_on_the_chart_is_refused(charts, slide, theme) -> None:
    """A typo in a label silently painted every bar the quiet colour, which reads as
    a page with no point rather than as a mistake."""
    box = _box(charts, y1=5.0)
    with pytest.raises(ValueError, match="Souht"):
        charts.column(slide, box, theme, [("East", 1), ("South", 2)], accent="Souht")
    with pytest.raises(ValueError, match="item 7"):
        charts.column(slide, box, theme, [("East", 1), ("South", 2)], accent=7)


def test_a_bar_series_reads_to_the_band_gate_as_data_and_not_as_accent_strips(charts, slide, theme) -> None:
    """The gate that reports filled bars carrying nothing cannot tell a chart from a
    decoration by looking at one rectangle; it tells them apart by company -- one
    thickness, one baseline, lengths that differ, one row each. Drawing a bar series
    that falls outside that leaves a finding on every chart page in the deck."""
    box = _box(charts, y1=5.0)
    charts.horizontal_bar(slide, box, theme, [("A", 92), ("B", 78), ("C", 65), ("D", 52)])

    bars = [shape for shape in slide.shapes if _rect(shape) is not None]
    exempt = data_mark_ids(bars)
    lengths = [shape for shape in bars if shape.width / 914400 > 1.0]
    assert lengths, "no bars were drawn"
    assert all(shape.shape_id in exempt for shape in lengths)


def test_a_progress_track_and_the_share_drawn_over_it_are_read_as_one_series(charts, slide, theme) -> None:
    """Two shapes to a row -- a full-length track and the value on it -- is the
    commonest way to draw this and was once refused as decoration on every row."""
    box = _box(charts, y1=4.0)
    charts.progress_bar(slide, box, theme, [("A", "88%"), ("B", "62%"), ("C", "41%")])

    bars = [shape for shape in slide.shapes if _rect(shape) is not None]
    assert bars
    assert set(shape.shape_id for shape in bars) <= data_mark_ids(bars)


def test_the_parts_of_a_stack_read_to_the_band_gate_as_data_and_not_as_strips(charts, slide, theme) -> None:
    """The one chart shape the series test cannot see: parts stand on each other
    rather than on one baseline, and their thickness the way the values run is the
    value. Three columns of three left a finding on each thin top part -- 2.46 x
    0.09in, "an accent strip" -- on the chart whose whole subject is a part too
    small to be a bar of its own."""
    charts.stacked_bar(
        slide,
        _box(charts, y1=6.5),
        theme,
        ["FY23", "FY24", "FY25"],
        {"Licence": [140, 160, 180], "Service": [90, 100, 110], "Other": [6, 8, 5]},
    )

    bars = [shape for shape in slide.shapes if _rect(shape) is not None]
    parts = [shape for shape in bars if shape.width / 914400 > 1.0 and shape.height / 914400 > MIN_PART_IN]
    assert len(parts) == 9, "the nine parts of three three-part stacks"
    assert set(shape.shape_id for shape in parts) <= data_mark_ids(bars)


def test_a_value_too_wide_for_its_bar_becomes_the_scale_written_on_the_plot(charts, slide, theme) -> None:
    """A number that will not fit is dropped, and something has to replace it: a
    plot of lengths with no number anywhere on it cannot be read at all."""
    box = charts.Box(0.72, 1.0, 4.2, 5.0)
    charts.grouped_bar(
        slide,
        box,
        theme,
        ["Q1", "Q2", "Q3", "Q4"],
        [("A", [1200000, 1450000, 1680000, 1850000]), ("B", [850000, 980000, 1120000, 1250000])],
    )

    written = {text for text, _, _, _ in _labels(slide)}
    assert "1850000" in written, written
    assert "1200000" not in written, "the per-bar values should not have fit"


def test_a_key_wraps_rather_than_running_its_last_name_off_the_page(charts, slide, theme) -> None:
    """The first version laid the key out left to right and stopped measuring, so a
    four-series key on a narrow region put its last name past the edge as a column of
    single letters -- the same failure it exists to prevent, in the furniture."""
    box = charts.Box(0.72, 1.0, 4.6, 5.0)
    charts.stacked_bar(
        slide,
        box,
        theme,
        ["FY24", "FY25"],
        {
            "Product Sales": [140, 160],
            "Professional Services": [90, 100],
            "Subscriptions": [50, 60],
            "Other Revenue": [20, 30],
        },
    )

    for text, where, _, _ in _labels(slide):
        assert where[2] <= box.x1 + 0.01, f"{text!r} runs to {where[2]:.2f}in past {box.x1:.2f}in"


def test_several_quiet_series_stay_different_colours_from_each_other(charts, slide, theme) -> None:
    """Accenting one series painted the other two the same grey, and a key with two
    identical swatches on it names nothing."""
    box = _box(charts, y1=5.0)
    charts.grouped_bar(
        slide,
        box,
        theme,
        ["Q1", "Q2"],
        [("A", [120, 145]), ("B", [85, 98]), ("C", [65, 78])],
        accent="A",
    )

    fills = {str(shape.fill.fore_color.rgb) for shape in slide.shapes if _rect(shape) and shape.height / 914400 > 0.4}
    assert len(fills) == 3, fills
    assert theme["accent"].lstrip("#").upper() in fills


def test_a_number_inside_a_segment_is_set_in_an_ink_that_reads_on_it(charts, slide, theme) -> None:
    """One ink for every segment puts white on the light end of a series. The deck's
    own contrast measurement reads the render at 3:1, so this is computed."""
    from raven.ppt.services.assets.color import contrast_ratio

    box = _box(charts, y1=5.5)
    charts.stacked_bar(
        slide,
        box,
        theme,
        ["FY24", "FY25"],
        {"Dark": [140, 160], "Mid": [90, 100], "Light": [50, 60], "Grey": [20, 30]},
    )

    grounds = {}
    for shape in slide.shapes:
        where = _rect(shape)
        if where and where[3] - where[1] > 0.1:
            grounds[(round(where[0], 3), round(where[1], 3), round(where[2], 3), round(where[3], 3))] = (
                f"#{shape.fill.fore_color.rgb}"
            )
    checked = 0
    for text, where, _, ink in _labels(slide):
        key = (round(where[0], 3), round(where[1], 3), round(where[2], 3), round(where[3], 3))
        if key not in grounds:
            continue
        checked += 1
        assert contrast_ratio(ink, grounds[key]) >= 3.0, f"{text!r} at {contrast_ratio(ink, grounds[key]):.1f}:1"
    assert checked >= 4, "no segment labels were found to check"


def test_the_parts_of_a_stack_step_from_deep_to_pale_instead_of_striping(charts, slide, theme) -> None:
    """`chart_series` is ordered by prominence, not by lightness: deep, mid, very
    pale, and round again. A three-part stack taking it in order came out deep
    navy, indigo, then a lavender at 2.3:1 -- three unrelated bands rather than one
    quantity divided, with the third one barely on the page. The parts of one whole
    are told apart by depth, and every depth has to survive a projector."""
    from raven.ppt.services.assets.color import contrast_ratio, relative_luminance

    box = _box(charts, y1=5.5)
    charts.stacked_bar(slide, box, theme, ["FY24"], {"Licence": [140], "Service": [90], "Training": [12]})

    fills = [
        (where[1], f"#{shape.fill.fore_color.rgb}")
        for shape, where in ((shape, _rect(shape)) for shape in slide.shapes)
        if where is not None and where[2] - where[0] > 1.0 and where[3] - where[1] > 0.05
    ]
    parts = [paint for _, paint in sorted(fills, reverse=True)]
    assert len(parts) == 3, parts
    lightness = [relative_luminance(paint) for paint in parts]
    assert lightness == sorted(lightness), f"the stack is not monotone in lightness: {parts}"
    for paint in parts:
        ratio = contrast_ratio(paint, theme["background"])
        assert ratio >= 3.0 - 1e-9, f"{paint} is {ratio:.1f}:1 on the page and reads as nothing"


def test_every_palette_cuts_a_stack_that_is_monotone_and_survives_a_projector(charts) -> None:
    """One theme proves the arithmetic; ten prove it was the right arithmetic.

    The palettes start from ten different depths -- one accent is 5.4:1 on the page
    and another 9.2:1 -- so a ramp capped at a fixed share would leave some of them
    invisible at the pale end and crowded at the deep one. The line is searched per
    theme instead, and what has to hold for all of them is what is checked: every
    part darker than the one above it, and none of them lost against the page.
    """
    from pptx import Presentation

    from raven.ppt.services.assets.color import contrast_ratio, relative_luminance
    from raven.ppt.services.assets.script_helpers import theme_catalog

    for theme_id, theme in theme_catalog().items():
        presentation = Presentation()
        presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        charts.stacked_bar(
            slide,
            _box(charts, y1=5.5),
            theme,
            ["FY25"],
            {"A": [40], "B": [30], "C": [20], "D": [10]},
        )

        parts = [
            (where[1], f"#{shape.fill.fore_color.rgb}")
            for shape, where in ((shape, _rect(shape)) for shape in slide.shapes)
            if where is not None and where[2] - where[0] > 1.0 and where[3] - where[1] > 0.05
        ]
        paints = [paint for _, paint in sorted(parts, reverse=True)]
        assert len(paints) == 4, (theme_id, paints)
        lightness = [relative_luminance(paint) for paint in paints]
        assert lightness == sorted(lightness), f"{theme_id} is not monotone: {paints}"
        for paint in paints:
            ratio = contrast_ratio(paint, theme["background"])
            assert ratio >= 3.0 - 1e-9, f"{theme_id} paints a part at {ratio:.1f}:1 on its own page"


def test_a_column_is_a_reading_and_not_a_slab_when_there_are_only_two(charts, slide, theme) -> None:
    """Measured on a delivered page: two categories beside a table, in a box 4.6in
    wide, drew a pair of bars 1.43in across and 2.1in tall. Nothing about the numbers
    is in that width -- the length is the reading -- and the paint is what made the
    page look both crowded and empty at once.

    The cap only bites where a slot is wider than a bar has any reason to be, so the
    five-and-more-category charts the corpus is mostly made of do not move.
    """
    def widths(count, box):
        drawn_on = slide.shapes
        before = len(drawn_on._spTree)
        charts.column(slide, box, theme, [(f"c{i}", 10 - i) for i in range(count)])
        found = []
        for shape in list(drawn_on)[max(0, before - 1):]:
            where = _rect(shape)
            if where is not None and where[3] - where[1] > 0.2:
                found.append(round(where[2] - where[0], 3))
        return found

    two = widths(2, charts.Box(7.4, 1.3, 12.0, 4.0))
    assert two, "the columns were not drawn"
    assert max(two) == pytest.approx(charts.BAR_MAX), f"two columns came out {max(two)}in wide"

    six = widths(6, charts.Box(0.7, 4.4, 6.7, 6.9))
    slot = 6.0 / 6
    assert max(six) == pytest.approx(slot * charts.BAR_SHARE), "the cap moved a chart that was already under it"


def test_a_waterfall_paints_by_role_and_keeps_its_falls_on_the_page(charts, slide, theme) -> None:
    """Taking `chart_series` by position gave the opening, the rise and the closing
    the same deep navy and the one fall a lavender at 2.3:1 -- and the fall was what
    the page was about. Levels, rises and falls are three depths of one line, all
    three readable, and none of them red or green: no palette here carries either
    and up is not good in every column."""
    from raven.ppt.services.assets.color import contrast_ratio, relative_luminance

    box = _box(charts, y1=5.5)
    charts.waterfall(
        slide,
        box,
        theme,
        [("Open", 280), ("Growth", 120), ("Cost", -45), ("Close", 355)],
        totals=(0, -1),
    )

    bars = sorted(
        (where[0], f"#{shape.fill.fore_color.rgb}")
        for shape, where in ((shape, _rect(shape)) for shape in slide.shapes)
        if where is not None and where[3] - where[1] > 0.05
    )
    opening, rise, fall, closing = [paint for _, paint in bars]
    assert opening == closing, "both levels are the same paint"
    assert len({opening, rise, fall}) == 3, (opening, rise, fall)
    assert relative_luminance(opening) < relative_luminance(rise) < relative_luminance(fall), (opening, rise, fall)
    for role, paint in (("level", opening), ("rise", rise), ("fall", fall)):
        ratio = contrast_ratio(paint, theme["background"])
        assert ratio >= 3.0 - 1e-9, f"the {role} is {ratio:.1f}:1 on the page"


def test_bars_standing_side_by_side_keep_the_series_palette_they_were_reviewed_in(charts, slide, theme) -> None:
    """The shade line is for one quantity divided. Several series side by side are
    several things, and `chart_series` is spaced by hue and checked for colour
    vision for exactly that -- so the fix for the striping must not reach it."""
    box = _box(charts, y1=5.0)
    charts.grouped_bar(slide, box, theme, ["Q1", "Q2"], [("A", [120, 145]), ("B", [85, 98]), ("C", [65, 78])])

    fills = {
        f"#{shape.fill.fore_color.rgb}"
        for shape, where in ((shape, _rect(shape)) for shape in slide.shapes)
        if where is not None and where[3] - where[1] > 0.3
    }
    assert fills == {paint.upper() for paint in theme["chart_series"][:3]}, fills


def test_the_quieter_end_of_a_dumbbell_is_still_a_mark_on_the_page(charts, slide, theme) -> None:
    """The "before" mark took the pale rung of `chart_series` -- 2.3:1 on white --
    and the gap the chart exists to show had only one end you could find."""
    from raven.ppt.services.assets.color import contrast_ratio

    box = _box(charts, y1=4.0)
    charts.dumbbell(slide, box, theme, [("Checkout", 61, 78), ("Search", 44, 71)], sides=("Before", "After"), unit="%")

    marks = {f"#{shape.fill.fore_color.rgb}" for shape in slide.shapes if _oval(shape)}
    assert len(marks) == 2, marks
    for paint in marks:
        ratio = contrast_ratio(paint, theme["background"])
        assert ratio >= 3.0 - 1e-9, f"{paint} is {ratio:.1f}:1 on the page"


def test_two_marks_close_together_do_not_have_their_labels_on_top_of_each_other(charts, slide, theme) -> None:
    """Direct labelling is the price of not having a legend, and the price is this:
    "Online" came out through "Offline" on the first render of a scatter whose two
    points were a tenth of an inch apart."""
    box = _box(charts, y1=5.0)
    charts.scatter(slide, box, theme, [("Online", 48, 420), ("Offline", 52, 410)], axes=("Spend", "Revenue"))

    placed = [(text, where) for text, where, _, _ in _labels(slide) if text in ("Online", "Offline")]
    assert len(placed) == 2
    (_, first), (_, second) = placed
    apart = (
        first[2] <= second[0] + 0.01
        or second[2] <= first[0] + 0.01
        or first[3] <= second[1] + 0.01
        or second[3] <= first[1] + 0.01
    )
    assert apart, f"{first} and {second} overlap"


def test_a_waterfall_step_starts_where_the_step_before_it_ended(charts, slide, theme) -> None:
    """A floating bar drawn from the wrong offset is a running total that does not
    run, and the page's claim -- 280 became 400 through these -- is then false."""
    box = _box(charts, y1=5.5)
    charts.waterfall(
        slide,
        box,
        theme,
        [("Open", 280), ("Up", 120), ("Down", -45), ("Close", 355)],
        totals=(0, -1),
    )

    bars = sorted((b for b in _bars(slide) if b[3] - b[1] > 0.05), key=lambda b: b[0])
    assert len(bars) == 4
    opening, rise, fall, closing = bars
    assert rise[3] == pytest.approx(opening[1], abs=0.01), "the rise should start at the opening's top"
    assert fall[1] == pytest.approx(rise[1], abs=0.01), "the fall should start at the rise's top"
    assert closing[3] == pytest.approx(opening[3], abs=0.01), "both totals stand on the baseline"


def test_a_pareto_sorts_itself_because_an_unsorted_one_is_not_a_pareto(charts, slide, theme) -> None:
    box = _box(charts, y1=5.0)
    charts.pareto(slide, box, theme, [("Small", 40), ("Big", 350), ("Middle", 150)])

    ordered = [text for text, _, _, _ in sorted(_labels(slide), key=lambda entry: entry[1][0])]
    assert [name for name in ordered if name in ("Big", "Middle", "Small")] == ["Big", "Middle", "Small"]


def test_a_histogram_bins_observations_without_leaving_a_gap_between_them(charts, slide, theme) -> None:
    """A gap says the axis underneath is a set of categories, and it is not."""
    box = _box(charts, y1=5.0)
    charts.histogram(slide, box, theme, list(range(1, 26)), bins=5)

    # The hairlines in the ground colour that keep contiguous bars countable are
    # rectangles too; the bars are the wide ones.
    bars = sorted((b for b in _bars(slide) if b[3] - b[1] > 0.2 and b[2] - b[0] > 0.5), key=lambda b: b[0])
    assert len(bars) == 5
    for left, right in zip(bars, bars[1:]):
        assert right[0] == pytest.approx(left[2], abs=0.001)


def test_a_gantt_reads_iso_dates_and_plain_numbers_and_refuses_anything_else(charts, slide, theme) -> None:
    box = _box(charts, y1=5.0)
    charts.gantt(slide, box, theme, [("A", "2026-01-06", "2026-02-14"), ("B", 1, 4)])
    with pytest.raises(ValueError, match="ISO dates"):
        charts.gantt(slide, box, theme, [("A", "next tuesday", "later")])
    with pytest.raises(ValueError, match="ends before it starts"):
        charts.gantt(slide, box, theme, [("A", "2026-03-01", "2026-01-01")])


def test_a_heatmap_tint_runs_monotone_between_two_of_the_theme_s_own_paints(charts, slide, theme) -> None:
    """The one paint in the module that is arithmetic rather than a lookup.

    `chart_series` is the only other place six paints sit ready and it is ordered
    by prominence, not by lightness, so a scale read off it would run light, dark,
    light -- and a tint that is not monotone is not a scale, it is six colours. So
    both ends are theme tokens (`accent_soft` and `chart_series[0]`, the paint a
    theme reserves for data) and only the distance between them is computed, which
    is measured here: eleven steps, each darker than the last, and the ends exact.
    """
    box = _box(charts, y1=5.0)
    charts.heatmap(slide, box, theme, ["Ramp"], [str(step) for step in range(11)], [[step * 10 for step in range(11)]])

    from raven.ppt.services.assets.color import relative_luminance

    cells = sorted(
        (where[0], f"#{shape.fill.fore_color.rgb}")
        for shape, where in ((shape, _rect(shape)) for shape in slide.shapes)
        if where is not None and where[3] - where[1] > 0.3
    )
    assert len(cells) == 11, cells
    ramp = [paint for _, paint in cells]
    assert ramp[0].upper() == theme["accent_soft"].upper(), "the low end is the theme's own soft accent"
    assert ramp[-1].upper() == theme["chart_series"][0].upper(), "the high end is the deepest data paint"
    lightness = [relative_luminance(paint) for paint in ramp]
    assert lightness == sorted(lightness, reverse=True), ramp
    assert len(set(ramp)) == 11, "two values a step apart came out the same colour"


def test_a_heatmap_writes_the_ends_of_its_scale_under_the_grid(charts, slide, theme) -> None:
    """A tint with no scale beside it is a ranking nobody can turn back into a
    number, which is half of what a heatmap is for."""
    box = _box(charts, y1=5.0)
    charts.heatmap(slide, box, theme, ["North", "South"], ["Q1", "Q2"], [[10, 40], [20, 30]])

    written = {text for text, _, _, _ in _labels(slide)}
    assert "10 to 40" in written, written


def test_a_chart_never_sets_type_under_the_floor_the_measurements_enforce(charts, slide, theme) -> None:
    """A chart that shrank its labels to fit would pass every geometry check and be
    unreadable projected, which is exactly the trade the type floor exists to refuse."""
    _draw_one_of_each(charts, slide, theme, _box(charts))

    sizes = {size for _, _, size, _ in _labels(slide)}
    assert sizes, "nothing was labelled"
    assert min(sizes) >= MIN_FLOOR_PT, sizes


def test_nothing_any_chart_draws_escapes_the_region_it_was_given(charts, slide, theme) -> None:
    """A chart that overflows its box lands on whatever the page put beside it, and
    the author who placed the box has no way to know. Four of these overflowed on
    the first render -- a percentage past the right margin, a histogram's first edge
    label half off the canvas -- so every form the module draws is measured at once.
    """
    box = _box(charts)
    _draw_one_of_each(charts, slide, theme, box)

    outside = _escapes(slide, box)
    assert not outside, f"{len(outside)} shapes outside {box}: {outside[:4]}"


def test_every_colour_a_chart_paints_is_a_theme_paint_or_lies_between_two_of_them(charts, slide, theme) -> None:
    """The ten palettes are reviewed -- contrast on their own ground, series ordered
    by prominence and checked for colour vision. A chart that invents an RGB throws
    all of that away and usually lands on something worse.

    The ordered forms mix, because a palette ordered by prominence is not a scale
    and reading it in order stripes them. What they are allowed is a point on the
    straight line between two of the theme's own paints, which is checked here by
    solving for where on the line each painted colour sits and confirming all three
    channels agree -- a colour off either line is an invented one.
    """
    box = _box(charts)
    charts.column(slide, box, theme, [("A", 2), ("B", 1)], accent="A")
    charts.grouped_bar(slide, box, theme, ["Q1"], [("A", [1]), ("B", [2]), ("C", [3])], accent="A")
    charts.waterfall(slide, box, theme, [("Open", 10), ("Up", 5), ("Down", -3)])
    charts.waterfall(slide, box, theme, [("Open", 10), ("Up", 5), ("Down", -3)], accent="Down")
    charts.stacked_bar(slide, box, theme, ["FY25"], {"A": [5], "B": [3], "C": [2], "D": [1]})
    charts.dumbbell(slide, box, theme, [("Checkout", 61, 78)])

    tokens = {str(value).upper() for value in theme.values() if isinstance(value, str) and value.startswith("#")}
    tokens |= {str(value).upper() for value in theme["chart_series"]}
    lines = ((theme["accent_soft"], theme["chart_series"][0]), (theme["grid"], theme["muted"]))
    painted = {f"#{shape.fill.fore_color.rgb}" for shape in slide.shapes if _rect(shape) is not None}
    invented = [paint for paint in painted if paint not in tokens and not any(_between(paint, *ends) for ends in lines)]
    assert not invented, invented


def test_a_chart_refuses_data_it_cannot_draw_rather_than_drawing_it_wrong(charts, slide, theme) -> None:
    """Every one of these produced a chart of some kind before it raised: a stack
    with a negative part drew a segment upside down, a short series drew a group
    with a hole in it, and both look like charts."""
    box = _box(charts)
    with pytest.raises(ValueError, match="at least one value"):
        charts.column(slide, box, theme, [])
    with pytest.raises(ValueError, match=r"\(label, value\)"):
        charts.column(slide, box, theme, ["East", "South"])
    with pytest.raises(ValueError, match="is a number, not"):
        charts.column(slide, box, theme, [("East", "about a lot")])
    with pytest.raises(ValueError, match="2 values and there are 3"):
        charts.grouped_bar(slide, box, theme, ["Q1", "Q2", "Q3"], [("A", [1, 2])])
    with pytest.raises(ValueError, match="negative part"):
        charts.stacked_bar(slide, box, theme, ["Q1"], [("A", [1]), ("B", [-2])])
    with pytest.raises(ValueError, match='"column" or "bar"'):
        charts.stacked_bar(slide, box, theme, ["Q1"], [("A", [1])], direction="sideways")
    with pytest.raises(ValueError, match="rows and 2 columns"):
        charts.heatmap(slide, box, theme, ["a", "b"], ["Q1", "Q2"], [[1, 2], [3]])
    with pytest.raises(ValueError, match=r"\(label, x, y\)"):
        charts.scatter(slide, box, theme, [("A", 1)])
    with pytest.raises(ValueError, match="totals names step"):
        charts.waterfall(slide, box, theme, [("A", 1)], totals=(4,))
    with pytest.raises(ValueError, match="none of them may be negative"):
        charts.pareto(slide, box, theme, [("A", 1), ("B", -1)])


def test_a_region_too_small_for_a_chart_says_so_instead_of_drawing_a_smear(charts, slide, theme) -> None:
    """Given a footer's worth of height the first version drew bars a hundredth of an
    inch tall and reported nothing, which reads as a page that forgot its chart."""
    with pytest.raises(ValueError, match="needs about"):
        charts.column(slide, charts.Box(0.72, 6.4, 12.61, 6.7), theme, [("A", 1), ("B", 2)])
    with pytest.raises(ValueError, match="wider box or shorter labels"):
        charts.butterfly(slide, charts.Box(0.72, 1.0, 3.15, 5.0), theme, [("A long name", 1234567, 7654321)])


def test_no_chart_takes_a_size_a_face_or_a_coordinate_from_its_caller(charts) -> None:
    """The schema a model writes carries no physical quantity, and a helper with a
    `size=` on it is that rule leaking out through the back door -- an author would
    reach for it, and then one page's chart is set at a size no other page uses.

    Over the forms, and not over every public name: the drawing base under them is
    public too, and a rectangle takes the colour to paint it and a label takes the
    step to set it at because that is what a primitive *is*. The rule is that a
    *form* decides those for the page, which is what keeps a deck of charts one
    deck; it was never that the module holds no function with `colour` in its
    signature -- it always did, one underscore away.
    """
    import inspect

    forbidden = ("size", "font", "pt", "width", "height", "inches", "emu", "colour", "color")
    charted = list(_drawn_charts())
    assert len(charted) >= 15, charted
    for name in charted:
        parameters = set(inspect.signature(getattr(charts, name)).parameters)
        assert not parameters & set(forbidden), f"{name} takes {sorted(parameters & set(forbidden))}"


def test_a_polyline_closes_and_takes_a_solid_when_it_is_given_a_fill(charts, slide, theme) -> None:
    """The one shape the twenty-three never make, and the reason a radar was unreachable.

    Both directions, because the default is the one every form here relies on: a
    cumulative curve read against an axis is a line, and an area under it would
    claim the area means something.
    """
    from pptx.enum.dml import MSO_FILL

    corners = [(1.0, 1.0), (3.0, 1.0), (2.0, 3.0)]
    open_shape = charts.poly(slide, corners, theme["muted"])
    filled = charts.poly(slide, corners, theme["accent"], fill=theme["accent_soft"])

    assert open_shape.fill.type == MSO_FILL.BACKGROUND
    assert filled.fill.type == MSO_FILL.SOLID
    assert str(filled.fill.fore_color.rgb) == theme["accent_soft"].lstrip("#").upper()
    assert str(filled.line.color.rgb) == theme["accent"].lstrip("#").upper()


def _alphas(shape) -> list[str]:
    """The `a:alpha` values on a shape's own fill, in thousandths, as written."""
    drawingml = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    return [element.get("val") for element in shape._element.findall(f".//{drawingml}alpha")]


def test_a_fill_takes_an_opacity_and_writes_it_as_alpha(charts, slide, theme) -> None:
    """The claim this answers was wrong in the other direction, and cost a form.

    "The export dialect treats every paint as opaque" was written in three places and
    acted on in the reference, which told an author to fill one radar series and
    outline the rest because a second fill would hide the first. A second *opaque*
    fill does. The dialect carries alpha, the renderer composites it, and the reading
    survives to the raster -- so what was documented as a limit of the format was a
    default nobody had tried to change.
    """
    corners = [(1.0, 1.0), (3.0, 1.0), (2.0, 3.0)]
    assert _alphas(charts.rect(slide, charts.Box(1.0, 1.0, 2.0, 2.0), theme["accent"])) == []
    assert _alphas(charts.rect(slide, charts.Box(1.0, 1.0, 2.0, 2.0), theme["accent"], 0.45)) == ["45000"]
    assert _alphas(charts.disc(slide, 2.0, 2.0, 0.2, theme["accent"], 0.45)) == ["45000"]
    assert _alphas(charts.poly(slide, corners, theme["accent"], fill=theme["accent"], opacity=0.45)) == ["45000"]
    # An outline is not what layering needs made translucent: two fills over each
    # other want the edges left solid, or neither shape is a shape where they cross.
    assert _alphas(charts.poly(slide, corners, theme["accent"], opacity=0.45)) == []


def test_an_opacity_reads_the_three_spellings_a_share_does(charts, slide, theme) -> None:
    """`progress_bar` already reads "76%", "0.76" and 76 as one number, and an author
    who learned that there has no reason to meet a second convention here."""
    box = charts.Box(1.0, 1.0, 2.0, 2.0)
    written = {spelling: _alphas(charts.rect(slide, box, theme["accent"], spelling)) for spelling in (0.45, 45, "45%")}
    assert list(written.values()) == [["45000"]] * 3, written
    assert _alphas(charts.rect(slide, box, theme["accent"], 1.0)) == []

    # Named for what it is: the refusal used to say "a progress track" whatever was
    # being read, which sends the author to the wrong argument.
    with pytest.raises(ValueError, match="opacity.*needs a share"):
        charts.rect(slide, box, theme["accent"], "half")
    with pytest.raises(ValueError, match="opacity.*needs a finite share"):
        charts.rect(slide, box, theme["accent"], float("nan"))


def test_the_drawing_base_is_public_and_is_not_offered_as_a_form(charts) -> None:
    """An author who needs a shape none of the forms make has to reach the base.

    Two claims in one, because they pull against each other. Every name below is one
    an author's program can `from ppt_charts import`, which is the whole point of
    making them public -- a scale, a paint line and a fitted label are the hard half
    of a chart, and a page that cannot reach them draws the nearest of the
    twenty-three instead of the shape of its own argument. And none of them is a
    chart: `chart_names` is what the brief lists as forms to pick, and a page that
    picked `rect` off that list would be picking a rectangle.
    """
    import inspect

    base = (
        "rect disc ring hline vline poly write_label span snap linear "
        "series_paints stack_paints shades emphasis ink_on contrast "
        "type_face text_width pick_size line_height key scale_top number fmt"
    ).split()
    for name in base:
        assert inspect.isfunction(getattr(charts, name, None)), f"{name} is not public on ppt_charts"
    assert not set(base) & set(_drawn_charts())
    assert not set(base) & set(charts._drawable_names())


def test_the_module_the_author_imports_reaches_back_into_nothing_of_ours(charts) -> None:
    """The build directory has to run as a plain python-pptx project: the author
    reads these files to learn what it may draw with, and an import of raven
    internals would break the moment those moved."""
    from raven.ppt.services.assets.charts import chart_module_source

    source = chart_module_source()
    assert "raven" not in source
    assert "from ppt_layout import" in source


def test_the_second_way_of_giving_a_chart_its_data_draws_the_same_chart(charts, slide, theme) -> None:
    """Bins already counted, a schedule in weeks rather than dates, a stack running
    across rather than up: every one of these is a shape the author will reach for,
    and every one was a path nothing exercised until a build failed on it.
    """
    import datetime

    box = _box(charts, y1=5.0)
    charts.histogram(slide, box, theme, [("0-10", 7), ("10-20", 12), ("20-30", 5)])
    charts.gantt(slide, box, theme, [("Sprint 1", 1, 4), ("Sprint 2", 3, 8)], ticks=4)
    charts.gantt(slide, box, theme, [("Q1", datetime.date(2026, 1, 6), datetime.date(2026, 3, 30))])
    charts.stacked_bar(
        slide,
        box,
        theme,
        ["North", "South", "East"],
        {"New": [40, 30, 20], "Renewed": [60, 70, 80]},
        share=True,
        direction="bar",
    )
    charts.dumbbell(slide, box, theme, [("A", 10, 20), ("B", 15, 25)], axis_max=400)

    written = {text for text, _, _, _ in _labels(slide)}
    assert {"0-10", "10-20", "20-30"} <= written
    assert "Sprint 1" in written and "Q1" in written


def test_a_column_below_the_baseline_hangs_down_and_keeps_its_number(charts, slide, theme) -> None:
    """A negative reading drawn upward is a chart that says the opposite of the
    data, and one drawn downward with its label still overhead is a number sitting
    on nothing."""
    box = _box(charts, y1=5.0)
    charts.column(slide, box, theme, [("Up", 60), ("Down", -40)])

    bars = sorted((b for b in _bars(slide) if b[3] - b[1] > 0.2), key=lambda b: b[0])
    assert len(bars) == 2
    up, down = bars
    assert up[3] == pytest.approx(down[1], abs=0.01), "both stand on one baseline"
    labelled = {text: where for text, where, _, _ in _labels(slide)}
    assert labelled["-40"][1] >= down[3] - 0.01, "the negative's number belongs under it"


def test_when_no_number_fits_the_top_of_the_scale_is_written_instead(charts, slide, theme) -> None:
    """A plot of lengths with no figure anywhere on it cannot be read at all, so the
    scale's top replaces the per-bar values rather than joining them in being absent.
    """
    narrow = charts.Box(0.72, 1.0, 3.0, 5.0)
    charts.column(slide, narrow, theme, [("Alpha", 1250000), ("Beta", 980000), ("Gamma", 760000), ("Delta", 610000)])
    charts.waterfall(
        slide,
        narrow,
        theme,
        [("Open", 1250000), ("Up", 320000), ("Down", -180000), ("Close", 1390000)],
        totals=(0, -1),
        accent="Close",
    )

    written = {text for text, _, _, _ in _labels(slide)}
    assert "1250000" in written
    assert "980000" not in written


def test_a_value_that_is_not_finite_is_refused_by_the_row_it_arrived_on(charts, slide, theme) -> None:
    """`(new - old) / old` on the row whose `old` is zero, which every deck has one of.

    The author of a build script is a model doing arithmetic, so a NaN or an
    infinity arrives computed rather than typed. Both used to clear the type check
    and fail deep inside instead: "cannot convert float NaN to integer" out of the
    label formatter, "cannot convert float infinity to integer" out of the axis,
    `pareto` turning an infinity into a NaN on the way to a stranger one -- every
    message from a module the author did not write and none of them naming the row.
    `progress_bar` did not fail at all: a NaN clamped to an empty track and an
    infinity to a full one, which is worse than any error.

    So every chart is driven here, with the bad value on a row called "East", and
    the error has to say both that the value is not finite and which row it was.
    """
    box = _box(charts)
    draw = {
        "column": lambda v: charts.column(slide, box, theme, [("West", 1), ("East", v)]),
        "horizontal_bar": lambda v: charts.horizontal_bar(slide, box, theme, [("West", 1), ("East", v)]),
        "grouped_bar": lambda v: charts.grouped_bar(slide, box, theme, ["Q1"], {"East": [v], "West": [1]}),
        "stacked_bar": lambda v: charts.stacked_bar(slide, box, theme, ["Q1"], {"East": [v], "West": [1]}),
        "butterfly": lambda v: charts.butterfly(slide, box, theme, [("East", 1, v)]),
        "progress_bar": lambda v: charts.progress_bar(slide, box, theme, [("East", v)]),
        "bullet": lambda v: charts.bullet(slide, box, theme, [("East", 1, v)]),
        "dumbbell": lambda v: charts.dumbbell(slide, box, theme, [("East", 1, v)]),
        "waterfall": lambda v: charts.waterfall(slide, box, theme, [("Start", 10), ("East", v)]),
        "pareto": lambda v: charts.pareto(slide, box, theme, [("East", v), ("West", 1)]),
        "histogram": lambda v: charts.histogram(slide, box, theme, [("West", 4), ("East", v)]),
        "heatmap": lambda v: charts.heatmap(slide, box, theme, ["East"], ["Q1"], [[v]]),
        "matrix_2x2": lambda v: charts.matrix_2x2(slide, box, theme, [("East", 1, v)]),
        "scatter": lambda v: charts.scatter(slide, box, theme, [("East", 1, v)]),
        "dot_plot": lambda v: charts.dot_plot(slide, box, theme, ["Q1"], {"East": [v], "West": [1]}),
        "line": lambda v: charts.line(slide, box, theme, ["Q1", "Q2"], {"East": [1, v]}),
        "combo": lambda v: charts.combo(slide, box, theme, [("East", 1), ("West", 2)], [v, 1]),
        "funnel": lambda v: charts.funnel(slide, box, theme, [("West", 10), ("East", v)]),
        "box_plot": lambda v: charts.box_plot(slide, box, theme, [("East", [1, 2, v])]),
        "treemap": lambda v: charts.treemap(slide, box, theme, [("West", 10), ("East", v)]),
        "marimekko": lambda v: charts.marimekko(slide, box, theme, ["Q1"], {"East": [v], "West": [1]}),
    }
    # gantt and milestone read dates rather than numbers and have their own
    # refusal; every other chart the module draws is here, which is the point of
    # one guard at the door.
    assert set(draw) | {"gantt", "milestone"} == set(_drawn_charts())
    for name, call in draw.items():
        for value in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(ValueError) as caught:
                call(value)
            assert "finite" in str(caught.value), f"{name} with {value}: {caught.value}"
            assert "East" in str(caught.value), f"{name} with {value}: {caught.value}"


def test_a_reading_that_is_a_string_of_four_hundred_digits_is_an_infinity_too(charts, slide, theme) -> None:
    """The other way in, since a value may arrive as text off a table.

    `float("9" * 400)` is `inf`, so the guard sits after both paths rather than
    beside the type check that only the first one passes through.
    """
    with pytest.raises(ValueError, match="finite"):
        charts.column(slide, _box(charts), theme, [("East", "9" * 400)])


def test_a_value_that_is_not_a_number_is_named_in_the_error_rather_than_coerced(charts, slide, theme) -> None:
    """`True` reads as 1 to float() and as a chart to everything downstream, which is
    how a boolean column of "supported / not" once came out as a bar chart of ones."""
    box = _box(charts)
    with pytest.raises(ValueError, match="not a boolean"):
        charts.column(slide, box, theme, [("A", True)])
    with pytest.raises(ValueError, match="not a boolean"):
        charts.column(slide, box, theme, [("A", 1), ("B", 2)], accent=True)
    with pytest.raises(ValueError, match="needs a share"):
        charts.progress_bar(slide, box, theme, [("A", "most of it")])
    with pytest.raises(ValueError, match="at least one item"):
        charts.progress_bar(slide, box, theme, [])
    with pytest.raises(ValueError, match="at least one row"):
        charts.dumbbell(slide, box, theme, [])
    with pytest.raises(ValueError, match=r"\(label, value, right\) rows"):
        charts.butterfly(slide, box, theme, [("A", 1)])
    with pytest.raises(ValueError, match=r"\(name, values\) series"):
        charts.grouped_bar(slide, box, theme, ["Q1"], ["A"])
    with pytest.raises(ValueError, match="one category and one series"):
        charts.stacked_bar(slide, box, theme, [], {})
    with pytest.raises(ValueError, match="at least one row and one column"):
        charts.heatmap(slide, box, theme, [], ["Q1"], [])
    with pytest.raises(ValueError, match="at least one point"):
        charts.scatter(slide, box, theme, [])
    with pytest.raises(ValueError, match="at least one task"):
        charts.gantt(slide, box, theme, [])
    with pytest.raises(ValueError, match=r"\(task, start, end\)"):
        charts.gantt(slide, box, theme, [("A", 1)])
    with pytest.raises(ValueError, match=r"x_low, x_high, y_low, y_high"):
        charts.scatter(slide, box, theme, [("A", 1, 2)], limits=(0, 1))


def test_a_chart_of_one_reading_still_has_a_scale_to_draw_it_against(charts, slide, theme) -> None:
    """Every span here is a division, and a deck with one number on a page is common
    enough that a zero-width one has to be a scale rather than a ZeroDivisionError."""
    box = _box(charts, y1=5.0)
    charts.column(slide, box, theme, [("Only", 0)])
    charts.histogram(slide, box, theme, [5, 5, 5, 5, 5])
    charts.heatmap(slide, box, theme, ["One"], ["Q1"], [[7]])
    charts.gantt(slide, box, theme, [("A", 3, 3)])

    assert _labels(slide), "nothing was drawn"


def test_a_stack_part_too_narrow_for_its_number_keeps_it_outside_the_part(charts, slide, theme) -> None:
    """A nine per cent sliver with nothing written on it is a share the reader
    cannot recover: the segment is too short to measure by eye and the number that
    would say so was dropped for not fitting inside it. So it moves out of the
    segment instead, on a leader, and the page still carries every reading it drew.
    """
    box = charts.Box(0.72, 1.0, 4.6, 3.0)
    charts.stacked_bar(slide, box, theme, ["FY25"], {"A": [61], "B": [30], "C": [9]}, share=True, direction="bar")

    placed = {text: where for text, where, _, _ in _labels(slide)}
    assert "9%" in placed, f"the sliver lost its reading: {sorted(placed)}"
    where = placed["9%"]
    assert where[0] >= box.x0 - 0.01 and where[2] <= box.x1 + 0.01, where
    assert where[1] >= box.y0 - 0.01 and where[3] <= box.y1 + 0.01, where
    segments = [bar for bar in _bars(slide) if bar[3] - bar[1] > 0.2]
    assert segments, "no segments were drawn"
    for segment in segments:
        assert _apart(where, segment), f"the callout sits on the stack at {segment}"


def test_a_reading_with_nowhere_to_go_is_dropped_rather_than_written_over_its_neighbour(charts, slide, theme) -> None:
    """Moving a number outside its segment only helps while there is somewhere to
    put it. Three rows of four per cent on a shallow region have air for two of
    them, and the third has to go: two readings on top of each other is not a
    reading, and it is the failure the callout exists to avoid rather than cause.
    """
    box = _box(charts, y1=3.6)
    charts.stacked_bar(
        slide,
        box,
        theme,
        ["North", "South", "East"],
        {"New": [48, 62, 71], "Renewed": [48, 34, 25], "Other": [4, 4, 4]},
        share=True,
        direction="bar",
    )

    written = [(text, where) for text, where, _, _ in _labels(slide)]
    assert sum(1 for text, _ in written if text == "4%") >= 1, "every sliver lost its reading"
    for index, (first_text, first) in enumerate(written):
        for second_text, second in written[index + 1 :]:
            assert _apart(first, second), f"{first_text!r} at {first} sits on {second_text!r} at {second}"
    for _, where in written:
        assert where[2] <= box.x1 + 0.01 and where[3] <= box.y1 + 0.01, where


def test_a_point_whose_name_clears_nothing_goes_unnamed_rather_than_over_another_name(charts, slide, theme) -> None:
    """Four positions were tried here once and the last was taken whether it was
    free or not, so five points in one corner came out as a knot of overlapping
    words -- two names neither of which can be read, where an unlabelled mark is at
    least still a reading of x and y. Twelve positions are tried now and a name
    that clears none of them is not written."""
    box = _box(charts, y1=5.5)
    charts.scatter(
        slide,
        box,
        theme,
        [
            ("Alpha", 1, 1),
            ("Beta", 1.2, 1.1),
            ("Gamma", 1.1, 1.25),
            ("Delta", 1.3, 1.3),
            ("Epsilon", 1.05, 1.4),
            ("Zeta", 1.35, 1.05),
            ("Eta", 9, 9),
        ],
        axes=("Effort", "Impact"),
    )

    greek = ("Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta", "Eta")
    written = [(text, where) for text, where, _, _ in _labels(slide) if text in greek]
    assert written, "no point was named at all"
    for index, (first_text, first) in enumerate(written):
        for second_text, second in written[index + 1 :]:
            assert _apart(first, second), f"{first_text!r} at {first} sits on {second_text!r} at {second}"
    assert len(written) < len(greek), "a cluster this tight cannot have held every name"
    assert "Eta" in {text for text, _ in written}, "the point with room around it should keep its name"


def test_the_point_carrying_the_accent_is_the_one_that_keeps_its_name_on_a_crowded_plot(charts, slide, theme) -> None:
    """Names are placed in input order and a crowded plot runs out of room, so
    whichever point happened to be written last lost its name -- which on the one
    page that argues about a single point is the point the page is about. The
    accented one is placed first for exactly that reason."""
    box = _box(charts, y1=5.5)
    # A far point holds the scale open, so the other eight land on top of each
    # other rather than being spread out by the axis snapping to them.
    crowd = [(f"Item {index}", 1 + (index % 4) * 0.1, 1 + (index // 4) * 0.1) for index in range(8)]
    charts.scatter(
        slide,
        box,
        theme,
        [*crowd, ("Chosen", 1.15, 1.05), ("Far", 9, 9)],
        axes=("Effort", "Impact"),
        accent="Chosen",
    )

    names = [(text, where) for text, where, _, _ in _labels(slide) if text.startswith(("Item", "Chosen"))]
    assert "Chosen" in {text for text, _ in names}, sorted(text for text, _ in names)
    assert len(names) < 9, "a cluster this tight cannot have held every name; the test is not measuring anything"
    for index, (first_text, first) in enumerate(names):
        for second_text, second in names[index + 1 :]:
            assert _apart(first, second), f"{first_text!r} at {first} sits on {second_text!r} at {second}"


def test_a_magnitude_too_small_to_draw_to_scale_is_an_open_mark_and_not_a_disc(charts, slide, theme) -> None:
    """A bubble's area is its magnitude, and under about a thirtieth of the largest
    one there is no circle small enough to say so and still be visible. The floor
    used to be a filled disc, so 4, 0.5 and 0.05 came out as three identical discs
    -- three areas the reader reads and none of them true. They are drawn open
    instead: one size, visibly not filled in, which reads as "below the scale"."""
    box = _box(charts, y1=5.5)
    charts.scatter(
        slide,
        box,
        theme,
        [("A", 10, 10, 400), ("B", 30, 30, 100), ("C", 50, 50, 25), ("D", 70, 70, 1), ("E", 90, 90, 0.1)],
        axes=("Spend", "Revenue"),
    )

    drawn = _marks(slide)
    scaled = sorted(diameter for diameter, filled in drawn if filled)
    open_marks = sorted(diameter for diameter, filled in drawn if not filled)
    assert len(scaled) == 3 and len(open_marks) == 2, drawn
    # Area, not radius: four times the magnitude is twice the diameter.
    assert scaled[1] / scaled[0] == pytest.approx(2.0, rel=0.01)
    assert scaled[2] / scaled[1] == pytest.approx(2.0, rel=0.01)
    assert open_marks[0] == pytest.approx(open_marks[1]), "below the scale they are all one size"
    assert open_marks[1] < scaled[0], "the open mark is the floor, under everything drawn to scale"


def test_a_point_s_name_never_lands_on_the_number_that_says_where_the_axis_ends(charts, slide, theme) -> None:
    """The axis readings were not among the boxes a label checked itself against,
    so a point at an end of either scale could put its name over the number that
    says where that end is -- and then neither the point nor the axis is readable."""
    box = _box(charts, y1=5.5)
    charts.scatter(
        slide,
        box,
        theme,
        [("NW", 0, 10), ("NE", 10, 10), ("SW", 0, 0), ("SE", 10, 0), ("Mid", 5, 5)],
        axes=("Effort", "Impact"),
        limits=(0, 10, 0, 10),
    )

    written = [(text, where) for text, where, _, _ in _labels(slide)]
    names = [entry for entry in written if entry[0] in ("NW", "NE", "SW", "SE", "Mid")]
    readings = [entry for entry in written if entry[0] in ("0", "10")]
    assert len(names) == 5, [text for text, _ in written]
    assert len(readings) == 4, [text for text, _ in written]
    for name, where in names:
        for reading, other in readings:
            assert _apart(where, other), f"{name!r} at {where} sits on the axis reading {reading!r} at {other}"


# The cell a chart gets on a page of four or six of them, which is the box the two
# label collisions below were found in. A chart given half the page has room to
# spare and shows neither.
def _cell(charts):
    return charts.Box(0.72, 1.0, 4.50, 3.42)


def test_a_quadrant_s_name_is_written_where_the_data_in_it_is_not(charts, slide, theme) -> None:
    """The name of a quadrant used to be nailed to the corner the data is in.

    Furthest from the crossing is where a quadrant name is least likely to be read
    as the next quadrant's -- and it is also exactly where the extreme point of
    that quadrant sits, because that is what makes it extreme. Read off a render of
    the four named quarters: "second" was written across the mark at (4.1, 4.6)
    with 0.188in of the disc under the type, the disc being 0.150in across, so the
    label covered the whole of the point whose quadrant it was naming and the two
    of them read as neither.

    A quadrant name names a region and reads from any corner of it; a point is at
    its reading and nowhere else. So the name is the one that moves, and it moves
    within its own quarter -- which is asserted here as well, because a name that
    solved the overlap by drifting across the cross would be a worse defect than
    the one it fixed.
    """
    charts.matrix_2x2(
        slide,
        _cell(charts),
        theme,
        [("A", 1.2, 3.4), ("B", 2.8, 1.9), ("C", 4.1, 4.6), ("D", 3.3, 2.2)],
        axes=("cost", "return"),
        quadrants=("first", "second", "third", "fourth"),
    )

    quadrants = ("first", "second", "third", "fourth")
    written = {}
    for shape in slide.shapes:
        if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip() in quadrants:
            written[shape.text_frame.text.strip()] = _type_in(charts, shape, theme)
    assert sorted(written) == sorted(quadrants), sorted(written)
    marks = _discs(slide)
    assert len(marks) == 4, marks
    for name, ink in written.items():
        for mark in marks:
            assert _apart(ink, mark, tolerance=0.0), (
                f"the quadrant name {name!r} at {ink} is written over the mark at {mark}"
            )
    # Left of the cross or right of it, above it or below it, is the whole of what
    # makes a name belong to a quarter, so it is measured against the cross the
    # chart drew rather than against the box it was handed.
    shapes = _every_shape(slide)
    upright = [box for box in shapes if box[2] - box[0] < 0.05 and box[3] - box[1] > 0.5]
    flat = [box for box in shapes if box[3] - box[1] < 0.05 and box[2] - box[0] > 0.5]
    assert len(upright) == 1 and len(flat) == 1, (upright, flat)
    middle_x = (upright[0][0] + upright[0][2]) / 2
    middle_y = (flat[0][1] + flat[0][3]) / 2
    for name, left, top in (
        ("first", True, True),
        ("second", False, True),
        ("third", True, False),
        ("fourth", False, False),
    ):
        ink = written[name]
        assert (ink[2] <= middle_x) if left else (ink[0] >= middle_x), f"{name!r} at {ink} crossed x={middle_x}"
        assert (ink[3] <= middle_y) if top else (ink[1] >= middle_y), f"{name!r} at {ink} crossed y={middle_y}"


def test_two_readings_on_a_schedule_s_axis_are_never_printed_on_each_other(charts, slide, theme) -> None:
    """The last two readings of a ruler came out as "05-1606-30", and the pitch said it was fine.

    A tick label is centred on its own tick and slid back inside the region, so the
    last one moves left by up to half its width and eats the gap the even spacing
    left it. The pitch cannot see that -- it is the same for every pair and only
    the ends are clamped -- so five marks in the cell a chart gets on a page of six
    put the last two readings 0.252in into each other, which is the whole of the
    space between them and then some.

    The axis loses the tick rather than the type: a ruler set in two sizes reads as
    two rulers. What went is reported, because a schedule quietly missing a reading
    is the kind of drop this module refuses to make silently -- and the gridline
    goes with the label, a hairline nobody can turn back into a date being exactly
    the decoration a tick is supposed not to be.
    """
    drawn = charts.gantt(
        slide,
        _cell(charts),
        theme,
        [
            ("req", "2026-01-01", "2026-02-15"),
            ("dev", "2026-02-01", "2026-05-30"),
            ("launch", "2026-06-01", "2026-06-30"),
        ],
        accent="launch",
    )

    tasks = {"req", "dev", "launch"}
    ruler = sorted(
        ((text, where) for text, where, _, _ in _labels(slide) if text not in tasks), key=lambda entry: entry[1][0]
    )
    assert len(ruler) >= 3, ruler
    for (left_text, left), (right_text, right) in zip(ruler, ruler[1:]):
        assert _apart(left, right, tolerance=0.0), (
            f"the reading {left_text!r} at {left} is printed on {right_text!r} at {right}"
        )
    assert drawn.readings_not_written == ("05-16",), drawn.readings_not_written
    assert not drawn.nothing_was_dropped
    # One gridline per surviving reading, and none for the one that went.
    assert len([box for box in _every_shape(slide) if box[2] - box[0] < 0.05 and box[3] - box[1] > 1.0]) == len(ruler)


# What "12M" actually sets at 14pt, in inches, in the face fontconfig resolves
# each theme's `font_family` to on this renderer -- read off FreeType once and
# recorded here so the test does not depend on which fonts a container has.
# `Cambria` has no metric-compatible stand-in installed and lands on DejaVu
# Serif; `Arial` lands on Liberation Sans, which was drawn to Arial's widths.
_TWELVE_M_IN = {"Century Schoolbook": 0.3998, "Arial": 0.3782}
# `write` keeps 0.04in of margin on each side before a character is set.
_WRITE_MARGINS = 0.08


# The bundled themes all name one face now, so the second case is the face a theme
# derived from a user's own template takes -- the other face the product still sets
# type in, and the reason this test is still a comparison rather than one number.
@pytest.mark.parametrize(("theme_id", "face"), [("warm-paper", "Century Schoolbook"), ("ink-graphite", "Arial")])
def test_a_value_label_gets_a_box_the_face_it_is_set_in_actually_fits(charts, slide, theme_id, face) -> None:
    """ "12M" came back as "12" over "M" in every Cambria theme, and nothing saw it.

    The box a value label gets is `_em_width` of the widest label plus an inset,
    and `_em_width` was one class average per character class with no face in it
    -- so it answered 0.342in for "12M" at 14pt whichever of the six faces the
    deck named. The inset leaves 0.08in of headroom over that, which covers the
    Arial themes (0.378in on this renderer, 10% over the estimate) and does not
    cover the Cambria ones (0.446in, 30% over): `warm-paper` and
    `terracotta-craft` folded the label onto two lines, and `plum-editorial`
    followed on Bookman Old Style.

    No check could catch it after the fact. A label that wraps inside its own box
    overlaps nothing, leaves nothing, and is the right size -- the render is clean
    and the number is in pieces. So it has to be decided before it is drawn, which
    means the estimate has to know which face is going to draw it.
    """
    from raven.ppt.services.assets.script_helpers import theme_catalog

    theme = dict(theme_catalog()[theme_id])
    theme["font_family"] = face
    charts.horizontal_bar(
        slide,
        _box(charts),
        theme,
        [("Alpha", 12), ("Bravo", 9), ("Charlie", 7), ("Delta", 4), ("Echo", 2)],
        unit="M",
    )

    written = [(text, where, size) for text, where, size, _ in _labels(slide)]
    label = next(entry for entry in written if entry[0] == "12M")
    _, (x0, _, x1, _), size = label
    assert size == 14
    room = x1 - x0 - _WRITE_MARGINS
    assert room >= _TWELVE_M_IN[face], (
        f"'12M' sets {_TWELVE_M_IN[face]}in in {face} and its box holds {room:.3f}in, so the render breaks it"
    )


# Every chart with data that exercises it, as (name, positional arguments, knobs).
# The measurement tests below read this rather than picking one chart each: an
# answer that is right for `column` and wrong for `gantt` is worse than no answer,
# because a build script asks the same question of all fifteen.
_EVERY_CHART = (
    ("column", ([("East", 185), ("South", 142), ("North", 128)],), {"accent": "East", "unit": "M"}),
    ("horizontal_bar", ({"TarViS": 48.3, "VITA": 45.7, "IDOL": 43.9},), {"accent": 0}),
    ("grouped_bar", (["Q1", "Q2"], [("A", [120, 145]), ("B", [85, 98])]), {"accent": "A"}),
    ("stacked_bar", (["FY24", "FY25"], {"Licence": [140, 160], "Service": [90, 100]}), {}),
    ("stacked_bar", (["FY25"], {"Licence": [160], "Service": [100]}), {"share": True, "direction": "bar"}),
    ("progress_bar", ([("Single sign-on", "88%"), ("Audit trail", 0.62)],), {"accent": 0}),
    ("bullet", ([("Revenue", 182, 200), ("Retention", 92, 90)],), {"accent": "Retention"}),
    ("butterfly", ([("18-24", 320, 280), ("25-34", 460, 510)],), {"sides": ("Cost", "Revenue")}),
    ("dumbbell", ([("Checkout", 61, 78), ("Search", 44, 71)],), {"sides": ("Before", "After"), "unit": "%"}),
    ("waterfall", ([("Open", 280), ("Growth", 120), ("Cost", -45), ("Close", 355)],), {"totals": (0, -1)}),
    ("pareto", ([("Function", 350), ("Finish", 220), ("Missing", 150)],), {"accent": 0}),
    ("histogram", ([12, 15, 18, 22, 25, 31, 38, 44, 52, 61, 72, 80],), {}),
    ("gantt", ([("Discovery", "2026-01-06", "2026-02-14"), ("Build", "2026-02-01", "2026-04-20")],), {}),
    ("heatmap", (["North", "South"], ["Q1", "Q2", "Q3"], [[12, 18, 24], [9, 11, 14]]), {}),
    (
        "matrix_2x2",
        ([("SSO", 8, 3), ("Audit", 6, 7)],),
        {
            "axes": ("Effort", "Impact"),
            "quadrants": ("Quick wins", "Big bets", "Fill-ins", "Money pits"),
            "accent": "Audit",
            "limits": (0, 10, 0, 10),
        },
    ),
    (
        "scatter",
        ([("Online", 48, 420, 28), ("Offline", 52, 410, 12)],),
        {"axes": ("Spend", "Revenue"), "accent": "Online"},
    ),
    ("dot_plot", (["Homepage", "Blog", "Repo"], {"LoCoMo": [93.05, 92.3, 92.5]}), {"unit": "%"}),
    (
        "dot_plot",
        (["LoCoMo", "LongMemEval"], {"Platform": [92.5, 94.4], "Open source": [88.1, 83.0]}),
        {"accent": "Platform"},
    ),
    ("line", (["Q1", "Q2", "Q3"], {"Calls": [88, 124, 186]}), {"unit": "M"}),
    ("line", (["1M", "5M", "10M"], {"Platform": [64.1, 55.2, 48.6], "Local": [66.3, 61.0, 57.4]}), {"accent": "Local"}),
    (
        "combo",
        ([("Q1", 88), ("Q2", 124), ("Q3", 186)], [44, 41, 27]),
        {"sides": ("Calls", "Growth"), "curve_unit": "%"},
    ),
    ("funnel", ([("Visited", 120000), ("Installed", 42000), ("Paid", 1450)],), {"accent": "Paid"}),
    ("milestone", ([("Seed", "2024-05-14"), ("Series A", "2025-09-09")],), {"accent": "Series A"}),
    (
        "box_plot",
        ([("Platform", [180, 210, 248, 275, 340, 520]), ("Local", [210, 240, 268, 295, 330])],),
        {"unit": "ms"},
    ),
    ("treemap", ([("Downloads", 1400), ("Stars", 410), ("Calls", 186), ("Frameworks", 24)],), {}),
    ("marimekko", (["Personal", "Enterprise"], {"Mem0": [640, 310], "EverOS": [160, 90]}), {"accent": "EverOS"}),
)

# Ten sectors with names of the length a real deck carries. The set the size
# question exists for: it is well inside every flat floor the module used to
# check and far outside what a quadrant of a 13.33in page can hold as columns.
_TEN_SECTORS = [
    ("Manufacturing", 312),
    ("Professional Services", 268),
    ("Healthcare", 241),
    ("Retail Banking", 205),
    ("Public Sector", 188),
    ("Transport", 154),
    ("Education", 131),
    ("Utilities", 118),
    ("Hospitality", 96),
    ("Agriculture", 71),
]


def test_every_chart_says_how_big_it_has_to_be_and_the_answer_is_tight(charts, slide, theme) -> None:
    """The question this module could not answer, asked of all fifteen.

    `_room` refused a box and said "needs about 1.0x0.9in", which was a constant:
    the same pair for three categories and for twelve. So the only way to find the
    real floor was to draw, fail, read the prose and guess again -- a whole render
    round for a number the chart works out before its first rectangle.

    Both halves are asserted. The box that comes back has to *draw* -- an answer
    that is still refused is worse than none -- and it has to be tight, because a
    floor quoted an inch over what is wanted costs a page the form it should have
    had. A fiftieth of an inch off either side and every one of the fifteen refuses.
    """
    for name, data, knobs in _EVERY_CHART:
        chart = getattr(charts, name)
        room = charts.the_smallest_box_a_chart_needs(chart, theme, *data, **knobs)

        chart(slide, charts.Box(0.0, 0.0, room.w, room.h), theme, *data, **knobs)
        assert not charts.whether_a_chart_fits(
            chart, charts.Box(0.0, 0.0, room.w - 0.02, room.h), theme, *data, **knobs
        ), f"{name} says it needs {room.w:.3f}in across and draws in {room.w - 0.02:.3f}in"
        assert not charts.whether_a_chart_fits(
            chart, charts.Box(0.0, 0.0, room.w, room.h - 0.02), theme, *data, **knobs
        ), f"{name} says it needs {room.h:.3f}in down and draws in {room.h - 0.02:.3f}in"


# The regions a page actually divides itself into, on the 13.33x7.5in canvas every
# theme is measured against: the whole body, a half, a quadrant, the shallow band a
# chart shares with copy above it, and the tall narrow column a two-thirds split
# leaves. A form is at its most likely to spill in the last two, where the axis
# readings and the key have least room to take theirs out of.
_REAL_REGIONS = (
    ("body", (0.72, 1.35, 12.61, 6.60)),
    ("half", (0.72, 1.35, 6.50, 6.60)),
    ("quadrant", (6.90, 1.35, 12.61, 3.80)),
    ("band", (0.72, 4.90, 12.61, 6.60)),
    ("column", (0.72, 1.35, 4.20, 6.60)),
)


def test_every_chart_keeps_every_shape_inside_the_box_it_was_given(charts, theme) -> None:
    """The region invariant, asked of each form on its own and at its own floor.

    `test_nothing_any_chart_draws_escapes_the_region_it_was_given` draws the lot
    into one generous box, which answers the question for a chart with room to
    spare and not for the one being squeezed. Every overflow this module has
    shipped was a label that ran out of a box which was tight rather than
    comfortable -- a percentage past the right margin, an edge reading half off the
    canvas -- so the smallest box each chart says it takes is in the sweep, beside
    the five shapes of region a real page divides itself into.

    Per chart on its own slide, because "which chart put that there" is the first
    thing anyone asks of a failure and a shared slide cannot answer it.
    """
    from pptx import Presentation
    from pptx.util import Inches

    spilled: dict[str, list[str]] = {}
    for name, data, knobs in _EVERY_CHART:
        chart = getattr(charts, name)
        floor = charts.the_smallest_box_a_chart_needs(chart, theme, *data, **knobs)
        regions = [*_REAL_REGIONS, ("floor", (0.72, 1.0, 0.72 + floor.w, 1.0 + floor.h))]
        for where, corners in regions:
            presentation = Presentation()
            presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
            slide = presentation.slides.add_slide(presentation.slide_layouts[6])
            box = charts.Box(*corners)
            if not charts.whether_a_chart_fits(chart, box, theme, *data, **knobs):
                continue  # refused, and a refusal draws nothing -- measured on its own above
            chart(slide, box, theme, *data, **knobs)
            outside = _escapes(slide, box)
            if outside:
                spilled.setdefault(name, []).append(
                    f"{where} {len(outside)} of {len(_every_shape(slide))}: {outside[0]}"
                )
    assert not spilled, f"shapes outside the box they were given: {spilled}"


def test_the_room_a_chart_asks_for_is_its_data_and_not_a_constant(charts, theme) -> None:
    """The floor that made the size question worth asking, and the bug it hides.

    A column chart's `_room` asked for 1.0x0.9in whether it carried three
    categories or ten, so ten sectors in a quadrant passed the check and drew ten
    labels 0.59in apart -- wrapped, overlapping, and nothing measured it. The floor
    has to follow the names: ten of these want 9.5in of box, which is most of a
    13.33in page and the reason the page picks a ranking instead.
    """
    three = charts.the_smallest_box_a_chart_needs(charts.column, theme, [("A", 1), ("B", 2), ("C", 3)])
    ten = charts.the_smallest_box_a_chart_needs(charts.column, theme, _TEN_SECTORS)

    assert three.w == pytest.approx(1.0, abs=0.01), "three one-letter names ask for the flat floor"
    assert ten.w > 9.0, f"ten sector names ask for {ten.w:.2f}in"
    quadrant = charts.Box(0.0, 0.0, 5.81, 2.27)
    assert not charts.whether_a_chart_fits(charts.column, quadrant, theme, _TEN_SECTORS)
    # Nine and not eight, and the difference is the face: rows share the box's height,
    # so nine of them are set smaller than eight and the narrower type is what fits the
    # names. The count is the theme's answer, not a property of the chart.
    assert charts.whether_a_chart_fits(charts.horizontal_bar, quadrant, theme, _TEN_SECTORS[:9])


def test_asking_a_chart_what_it_will_do_draws_nothing_and_answers_what_it_then_does(charts, theme) -> None:
    """The rehearsal has to be the chart, or the answer is a second opinion.

    Two bodies of arithmetic answering the same question is the drift this module
    refuses everywhere else, and it would be worst here: the whole value of "which
    names will not fit" is that it is what the draw is about to decide. So the
    measurement runs the real chart against a slide that swallows shapes, and this
    asserts both halves of that -- nothing reaches the slide, and everything the
    rehearsal reported is what the draw reports.
    """
    from pptx import Presentation
    from pptx.util import Inches

    for name, data, knobs in _EVERY_CHART:
        chart = getattr(charts, name)
        presentation = Presentation()
        presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
        rehearsed = presentation.slides.add_slide(presentation.slide_layouts[6])
        drew = presentation.slides.add_slide(presentation.slide_layouts[6])
        box = _box(charts, y1=5.0)

        will = charts.what_a_chart_will_do(chart, box, theme, *data, **knobs)
        assert len(rehearsed.shapes) == 0, f"{name} put {len(rehearsed.shapes)} shapes on a slide it was only asked"
        did = chart(drew, box, theme, *data, **knobs)

        assert tuple(will) == tuple(did), name
        assert will.type_pt == did.type_pt, name
        assert will.names_not_written == did.names_not_written, name
        assert will.readings_not_written == did.readings_not_written, name
        assert will.marks_not_to_scale == did.marks_not_to_scale, name


def test_the_points_a_scatter_could_not_name_are_the_ones_the_page_does_not_carry(charts, slide, theme) -> None:
    """The blindest of the module's silent decisions, and the one a render hides.

    `_plot_points` tries twelve positions for a name and writes nothing where none
    of them clears, which is right -- two names on top of each other read as
    neither. But it said so nowhere, so an author with eleven points and eight
    labels had to count them off a render to find out, and then guess whether
    `limits` would fix it. Asserted against the page rather than against itself:
    what comes back has to be exactly the labels the slide does not carry.
    """
    crowded = [
        ("Atlas", 62, 71),
        ("Bravo", 63, 72),
        ("Cobalt", 64, 70),
        ("Delta", 61, 69),
        ("Ember", 65, 73),
        ("Fjord", 63, 68),
        ("Gamut", 66, 71),
        ("Harbor", 60, 74),
        ("Lumen", 62.5, 70.5),
        ("Juno", 24, 21),
    ]

    drawn = charts.scatter(slide, _box(charts), theme, crowded, axes=("Effort", "Impact"))

    written = {text for text, _, _, _ in _labels(slide)}
    missing = {label for label, _, _ in crowded} - written
    assert drawn.names_not_written, "this cluster is meant to cost some names"
    assert set(drawn.names_not_written) == missing, (drawn.names_not_written, sorted(missing))


def test_a_chart_that_drops_its_values_says_which_readings_went(charts, slide, theme) -> None:
    """The scale's top replaces the per-bar numbers, and which numbers those were.

    A narrow column chart writes the top of its scale instead of a figure over each
    bar, which keeps the plot readable and takes eleven readings off the page. The
    author is the one who has to decide whether that is acceptable, and could not
    see it happen.
    """
    narrow = charts.Box(0.72, 1.0, 3.0, 5.0)
    drawn = charts.column(slide, narrow, theme, [("A", 1250000), ("B", 980000), ("C", 760000), ("D", 610000)])

    written = {text for text, _, _, _ in _labels(slide)}
    assert drawn.readings_not_written == ("1250000", "980000", "760000", "610000")
    assert {"980000", "760000", "610000"}.isdisjoint(written), "these four numbers are not on the page"
    assert "1250000" in written, "the top of the scale is what replaced them"


def test_the_readings_a_stack_and_a_grid_could_not_place_are_the_ones_off_the_page(charts, slide, theme) -> None:
    """The other two places a reading is dropped, asserted against the render.

    A stack segment too thin for its own number moves it outside on a leader, and
    where there is no air for that either the number goes; a heatmap cell narrower
    than its reading holds a tint and nothing else. Both are the right call and
    both were made silently, so a page could lose thirty of thirty figures and
    report a clean build.
    """
    stack = charts.stacked_bar(
        slide,
        _box(charts, y1=3.6),
        theme,
        ["North", "South", "East"],
        {"New": [48, 62, 71], "Renewed": [48, 34, 25], "Other": [4, 4, 4]},
        share=True,
        direction="bar",
    )
    written = [text for text, _, _, _ in _labels(slide)]
    assert stack.readings_not_written == ("4%",)
    assert written.count("4%") == 2, f"three slivers, one of which lost its reading: {written}"

    grid = charts.Box(0.72, 4.0, 6.5, 5.6)
    cells = charts.heatmap(
        slide,
        grid,
        theme,
        ["North", "South", "East"],
        [f"W{index}" for index in range(1, 11)],
        [[1200000 + row * 30000 + column * 1000 for column in range(10)] for row in range(3)],
    )
    on_page = {text for text, _, _, _ in _labels(slide)}
    assert len(cells.readings_not_written) == 30, "no cell is wide enough for a seven-figure reading"
    assert on_page.isdisjoint(cells.readings_not_written)


def test_a_bubble_too_small_to_carry_an_area_is_named_rather_than_left_to_be_counted(charts, slide, theme) -> None:
    """A mark under the floor stops being its magnitude, and says so.

    Below `BUBBLE_FLOOR` the diameter is the floor rather than the value, so the
    mark is drawn open to say it is off the scale. Which marks those were was
    visible only as "some of these circles are hollow", and an author reading a
    render cannot tell a hollow mark from a small one at review size.
    """
    drawn = charts.scatter(
        slide,
        _box(charts),
        theme,
        [("Alpha", 20, 30, 900), ("Beta", 50, 60, 480), ("Gamma", 70, 25, 12), ("Delta", 35, 75, 4)],
        axes=("x", "y"),
    )

    hollow = [diameter for diameter, filled in _marks(slide) if not filled]
    assert drawn.marks_not_to_scale == ("Gamma", "Delta")
    assert len(hollow) == len(drawn.marks_not_to_scale)


def test_a_refusal_carries_the_shortfall_and_adding_it_is_enough(charts, slide, theme) -> None:
    """ "Too small" is a sentence; "0.31in too short" is something a program can act on.

    The message was already there and the number was not, so a build script that
    caught the refusal could do nothing with it but hand the prose back to a model
    and pay for another round. The deficit comes with the exception now, and adding
    it to the box is the whole of the fix.
    """
    short = charts.Box(0.72, 1.0, 5.0, 1.2)
    with pytest.raises(charts.TooSmall) as refused:
        charts.horizontal_bar(slide, short, theme, _TEN_SECTORS)

    across, down = refused.value.short
    assert down > 0.0, refused.value.short
    charts.horizontal_bar(
        slide, charts.Box(short.x0, short.y0, short.x1 + across, short.y1 + down), theme, _TEN_SECTORS
    )


def test_a_chart_of_one_row_each_refuses_a_box_too_short_for_a_line_of_type(charts, slide, theme) -> None:
    """Where the type ramp runs out, the answer stops being a smaller size.

    Every row chart steps from 14pt to 12pt when a row is shorter than a line, and
    then draws whatever it was given: ten rows in 0.5in came out as ten labels a
    twentieth of an inch apart, one on top of the next, and the flat floor said
    0.5in was enough. Under a line of type per row there is no size left to step
    down to and the box has to be taller instead.
    """
    with pytest.raises(charts.TooSmall, match="needs about"):
        charts.horizontal_bar(slide, charts.Box(0.72, 1.0, 8.0, 1.6), theme, _TEN_SECTORS)
    charts.horizontal_bar(slide, charts.Box(0.72, 1.0, 8.0, 4.2), theme, _TEN_SECTORS)


def test_a_chart_hands_back_the_scale_that_puts_a_reading_on_its_plot(charts, slide, theme) -> None:
    """The plot without its scale is a rectangle, and an overlay drawn by eye.

    An author wanting a rule at last year's level, a target on a scatter or a today
    line on a schedule had the plot's corners and nothing that turns a number into
    an inch inside them -- so it divided the box by hand, which is the arithmetic
    every chart here exists to take off it. Asserted against the drawn geometry:
    the scale has to land on the mark, not near it.
    """
    box = _box(charts, y1=5.0)
    column = charts.column(slide, box, theme, [("A", 200), ("B", 150), ("C", 50)])
    tallest = min(bar[1] for bar in _bars(slide) if bar[3] - bar[1] > 0.2)
    assert column.where(200) == pytest.approx(tallest, abs=0.005)
    assert column.where(0) == pytest.approx(column.y1, abs=0.005)

    plotted = charts.scatter(slide, box, theme, [("One", 10, 40), ("Two", 90, 60)], limits=(0, 100, 0, 100))
    x, y = plotted.where(50, 50)
    assert x == pytest.approx((plotted.x0 + plotted.x1) / 2, abs=0.005)
    assert y == pytest.approx((plotted.y0 + plotted.y1) / 2, abs=0.005)


def test_what_a_chart_hands_back_is_still_the_plot_it_always_was(charts, slide, theme) -> None:
    """The findings ride on the return value rather than replacing it.

    Everything already written against these charts reads the plot's corners off
    what comes back, so `Drawn` is a `Box` and equal to the four numbers it always
    was. A named tuple of (plot, findings) would have been the same information and
    a break in every build script that had one.
    """
    box = _box(charts, y1=5.0)
    drawn = charts.column(slide, box, theme, [("East", 185), ("South", 142)], unit="M")

    assert isinstance(drawn, charts.Box)
    assert tuple(drawn) == (drawn.x0, drawn.y0, drawn.x1, drawn.y1)
    assert drawn.x0 >= box.x0 - 1e-9 and drawn.x1 <= box.x1 + 1e-9
    assert drawn.y0 >= box.y0 - 1e-9 and drawn.y1 <= box.y1 + 1e-9
    assert drawn.nothing_was_dropped
    assert drawn.inset(0.1).w == pytest.approx(drawn.w - 0.2)


def test_a_chart_s_plot_reads_back_as_box_as_well(charts, slide, theme) -> None:
    """The two `Drawn` types answer the same question under two names.

    A chart's *is* its plot box; `ppt_layout`'s is a shape carrying one under `.box`.
    An author who has written `picture_fit(...).box` writes `line(...).box` next, and
    it cost a live build a whole round -- `print("p11 chart", drawn.box, ...)` on the
    line after the chart was drawn, `AttributeError` before the deck existed.
    """
    box = _box(charts, y1=5.0)
    drawn = charts.line(slide, box, theme, ["2010", "2012"], [("top-5", [28.2, 15.3])], unit="%")

    assert isinstance(drawn.box, charts.Box)
    assert tuple(drawn.box) == (drawn.x0, drawn.y0, drawn.x1, drawn.y1)
    # And it is a reading, not a second rectangle: asking twice gives the same one.
    assert tuple(drawn.box) == tuple(drawn.box)


def test_a_chart_that_refuses_a_box_has_not_touched_the_slide(charts, theme) -> None:
    """Refusing is only safe to catch if it left nothing behind.

    `whether_a_chart_fits` exists so a script choosing between two forms gets a
    boolean rather than a traceback, and its docstring offers the two-line
    try/except as the equivalent. That equivalence holds only while a refusal is
    clean, and it was not: `stacked_bar` drew its key between the box check and the
    plot check, so every refusal in that gap left six shapes -- three swatches and
    three series names -- on a slide the caller believed untouched. They then
    measured as real content, and on the render they read as a legend belonging to
    nothing. `grouped_bar` had the same two checks in the other order and was clean,
    which is what said this was an oversight rather than a design.

    Probed across the whole refusal range rather than at one box, because the gap
    was between two checks and only boxes that fell inside it were affected.
    """
    from pptx import Presentation
    from pptx.util import Inches

    dirty: dict[str, int] = {}
    for name, data, knobs in _EVERY_CHART:
        chart = getattr(charts, name)
        for across in (0.4, 0.9, 1.6, 2.4, 4.0, 8.0):
            for down in (0.2, 0.5, 0.9, 1.2, 1.6, 2.4):
                presentation = Presentation()
                presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
                slide = presentation.slides.add_slide(presentation.slide_layouts[6])
                try:
                    chart(slide, charts.Box(0.0, 0.0, across, down), theme, *data, **knobs)
                except charts.TooSmall:
                    if len(slide.shapes):
                        dirty[name] = max(dirty.get(name, 0), len(slide.shapes))
                except ValueError:
                    pass  # a refusal about the data, not the room -- not what this measures
    assert not dirty, f"a refused chart left shapes on the slide: {dirty}"


# ------------------------------------------------ the forms added after the first fifteen
#
# Each of these was drawn by hand on a delivered page, or not drawn at all: the
# eight decks in the corpus carry two charts between them across 160 pages, and
# the argument the other 123 content pages made still had a shape. What is
# asserted here is the same class of thing the fifteen above assert -- the
# mapping from a value to a length or a position, the refusal, and the reading a
# render cannot show you is wrong.


_TWELVE_DIMENSIONS = [
    "Fast personalisation",
    "Managed procurement",
    "Local-first export",
    "Multimodal memory",
    "Multi-agent sharing",
    "Developer distribution",
    "Lifecycle governance",
    "Pricing transparency",
    "Graph semantics",
    "Compliance evidence",
    "Time to first value",
    "Migration cost",
]

# Ten retrieval timings with one slow request in them, which is the shape the
# whiskers exist to keep: q1 215.5, median 254, q3 286.25, and 900 outside the fence.
_TEN_LATENCIES = [180, 195, 210, 232, 248, 260, 275, 290, 340, 900]


def test_a_line_reads_by_position_so_its_scale_is_never_pulled_down_to_zero(charts, slide, theme) -> None:
    """The reason `dumbbell` does not start at zero, applied to the other form whose
    marks are positions.

    Four releases running 88 to 94 against a scale containing zero are four marks
    inside the top sixteenth of the plot -- one flat line, which says the opposite
    of a page about a series that moved. So the scale is snapped out to round ends
    and both of them are written, which is what keeps a non-zero axis honest.
    """
    box = _box(charts, y1=5.0)
    drawn = charts.line(slide, box, theme, ["Q1", "Q2", "Q3", "Q4"], {"LoCoMo": [88, 90, 92, 94]}, unit="%")

    marks = sorted(_discs(slide), key=lambda mark: mark[0])
    assert len(marks) == 4
    middles = [(mark[1] + mark[3]) / 2 for mark in marks]
    steps = [middles[index] - middles[index + 1] for index in range(3)]
    assert max(steps) - min(steps) < 0.01, "equal steps in value are equal steps in inches"
    assert middles[0] - middles[-1] > drawn.h * 0.5, "a zero-based scale would flatten these four"
    written = {text for text, _, _, _ in _labels(slide)}
    assert {"87%", "95%"} <= written, written
    assert drawn.where(94) == pytest.approx(middles[-1], abs=0.01)


def test_a_line_writes_its_readings_beside_the_slope_and_never_across_it(charts, slide, theme) -> None:
    """A number set over a point on a rising line lands on the segment leaving it.

    The first render of this put "88M" through the line between Q3 and Q4 on a
    six-quarter series -- legible in neither direction, and the segment is the one
    thing on the plot that has to stay unbroken. So the reading steps off to
    whichever side the slope leaves free, and this is the check that it did.
    """
    box = _box(charts, y1=5.0)
    charts.line(
        slide, box, theme, ["Q1", "Q2", "Q3", "Q4", "Q5", "Q6"], {"Calls": [42, 61, 88, 124, 158, 186]}, unit="M"
    )

    marks = sorted(_discs(slide), key=lambda mark: mark[0])
    centres = [((mark[0] + mark[2]) / 2, (mark[1] + mark[3]) / 2) for mark in marks]
    readings = [
        (text, where) for text, where, _, _ in _labels(slide) if text.endswith("M") and where[0] > centres[0][0] - 0.6
    ]
    assert len(readings) == 6, [text for text, _ in readings]
    for index in range(len(centres) - 1):
        (near_x, near_y), (far_x, far_y) = centres[index], centres[index + 1]
        for step in range(21):
            x = near_x + (far_x - near_x) * step / 20
            y = near_y + (far_y - near_y) * step / 20
            for text, where in readings:
                on_it = where[0] + 0.01 < x < where[2] - 0.01 and where[1] + 0.01 < y < where[3] - 0.01
                assert not on_it, f"{text} is written across the line at {x:.2f}, {y:.2f}"


def test_a_combo_holds_two_units_apart_and_writes_the_ends_of_both_scales(charts, slide, theme) -> None:
    """Two units on one plot is a trick unless both scales are written, and a rate
    written in the accent across a filled column is a third thing that is neither.

    The columns are lengths from zero and the curve is a position on its own
    scale; the four readings down the two sides are what let a reader turn either
    back into a number, and a rate whose mark sits inside a column steps out into
    the gutter rather than being set on the column.
    """
    box = _box(charts, y1=5.0)
    charts.combo(
        slide,
        box,
        theme,
        [("Q1", 88), ("Q2", 124), ("Q3", 186)],
        [44, 41, 27],
        sides=("Calls", "Growth"),
        unit="M",
        curve_unit="%",
    )

    columns = sorted(
        (bar for bar in _bars(slide) if bar[2] - bar[0] > 0.3 and bar[3] - bar[1] > 0.3), key=lambda bar: bar[0]
    )
    assert len(columns) == 3
    heights = [bar[3] - bar[1] for bar in columns]
    assert heights[1] / heights[2] == pytest.approx(124 / 186, rel=0.01), "a column is a length from zero"
    written = {text for text, _, _, _ in _labels(slide)}
    assert {"0M", "186M"} <= written, "the columns' scale, both ends"
    assert {"25%", "45%"} <= written, "the curve's own scale, snapped out to round ends"
    for text, where, _, _ in _labels(slide):
        if not text.endswith("%") or text in {"25%", "45%"}:
            continue
        for column in columns:
            assert _apart(where, column), f"{text} is set on a column"
    with pytest.raises(ValueError, match="one curve value per column"):
        charts.combo(slide, box, theme, [("Q1", 88), ("Q2", 124)], [44])


def test_a_combo_keeps_its_curve_off_the_colour_its_columns_are_painted(charts, slide, theme) -> None:
    """The render caught what the reviewed palettes hide.

    Ten reviewed themes keep `accent` and `chart_series[0]` apart, so a curve that
    simply took the accent looked right in every one of them. A theme derived from
    a template need not: the blue template these were rendered against has both the
    same #155FFD, and the growth curve came back in the columns' own blue -- on the
    page it was visible in the gutters between the columns and nowhere else, which
    is a chart with one unit on it and a second one implied.
    """
    collided = dict(theme)
    collided["chart_series"] = [theme["accent"], *list(theme["chart_series"])[1:]]
    charts.combo(
        slide, _box(charts, y1=5.0), collided, [("Q1", 88), ("Q2", 124), ("Q3", 186)], [44, 41, 27], curve_unit="%"
    )

    columns = {
        str(shape.fill.fore_color.rgb)
        for shape in slide.shapes
        if _rect(shape) is not None and _rect(shape)[3] - _rect(shape)[1] > 0.3
    }
    curve = {str(shape.fill.fore_color.rgb) for shape in slide.shapes if _oval(shape)}
    assert columns == {theme["accent"].lstrip("#").upper()}, columns
    assert curve and not curve & columns, f"the curve is painted {curve} and so are the columns"
    assert all(paint in {c.lstrip("#").upper() for c in collided["chart_series"]} for paint in curve), curve


def test_a_dot_plot_takes_the_rows_a_grouped_plot_has_to_refuse(charts, slide, theme) -> None:
    """The form for the case the fifteen had no answer to.

    Twelve capability names against two competitors is the page eight delivered
    decks carry between them thirty times, and every one of them is a plain text
    table: a bar wants a slot deep enough to still be a bar, so a grouped plot in
    half a page refuses twelve of them, and a mark wants one line of type.
    """
    # 7.38in and not 7.00: the rows share the box's height, so a box this tall sets its
    # names at a size whose width the box then has to carry, and the deck's face is
    # wider than the one this box was first written against.
    half = charts.Box(0.72, 1.0, 8.10, 5.8)
    series = {
        "Mem0": [92, 88, 54, 66, 61, 94, 48, 90, 71, 86, 95, 44],
        "EverOS": [61, 52, 93, 89, 87, 43, 91, 22, 84, 49, 58, 79],
    }
    assert not charts.whether_a_chart_fits(charts.grouped_bar, half, theme, _TWELVE_DIMENSIONS, series)
    assert charts.whether_a_chart_fits(charts.dot_plot, half, theme, _TWELVE_DIMENSIONS, series)

    drawn = charts.dot_plot(slide, half, theme, _TWELVE_DIMENSIONS, series, accent="EverOS", unit="")
    marks = _discs(slide)
    assert len(marks) == 24, "one mark per series per row"
    rows = {round((mark[1] + mark[3]) / 2, 2) for mark in marks}
    assert len(rows) == 12, "twelve rows, and every mark on one of them"
    highest = max(marks, key=lambda mark: (mark[0] + mark[2]) / 2)
    assert (highest[0] + highest[2]) / 2 == pytest.approx(drawn.where(95), abs=0.02)
    with pytest.raises(charts.TooSmall, match="needs about"):
        charts.dot_plot(slide, charts.Box(0.72, 1.0, 2.2, 1.4), theme, _TWELVE_DIMENSIONS, series)


def test_a_dot_plot_reads_a_mapping_and_a_list_of_series_as_the_same_plot(charts, slide, theme) -> None:
    """Both ways in, because a build script has the readings either way round."""
    box = _box(charts, y1=4.0)
    mapped = charts.what_a_chart_will_do(
        charts.dot_plot, box, theme, ["Homepage", "Blog", "Repo"], {"LoCoMo": [93.05, 92.3, 92.5]}
    )
    listed = charts.what_a_chart_will_do(
        charts.dot_plot, box, theme, ["Homepage", "Blog", "Repo"], [("LoCoMo", [93.05, 92.3, 92.5])]
    )
    assert tuple(mapped) == tuple(listed)
    assert mapped.where(93.05) == pytest.approx(listed.where(93.05))


def test_a_funnel_is_as_wide_as_the_value_and_says_what_each_stage_kept(charts, slide, theme) -> None:
    """Both halves of the form. The narrowing has to be the loss -- a stage drawn
    at a width somebody chose is decoration -- and the drop between two stages has
    to be written, because the one number a funnel is read for belongs to a pair of
    rows and neither of them can carry it.
    """
    box = _box(charts, y1=6.0)
    charts.funnel(slide, box, theme, [("Visited", 120000), ("Installed", 42000), ("Paid", 1450)])

    bars = sorted((bar for bar in _bars(slide) if bar[3] - bar[1] > 0.2), key=lambda bar: bar[1])
    assert len(bars) == 3
    widths = [bar[2] - bar[0] for bar in bars]
    assert widths[1] / widths[0] == pytest.approx(42000 / 120000, rel=0.01)
    assert widths[2] / widths[0] == pytest.approx(1450 / 120000, rel=0.02)
    assert len({round((bar[0] + bar[2]) / 2, 3) for bar in bars}) == 1, "every stage on one axis"
    written = {text for text, _, _, _ in _labels(slide)}
    assert {"-65%", "-97%"} <= written, written
    with pytest.raises(ValueError, match="may be negative"):
        charts.funnel(slide, box, theme, [("Visited", 120000), ("Lost", -400)])
    with pytest.raises(charts.TooSmall, match="needs about"):
        charts.funnel(slide, charts.Box(0.72, 1.0, 3.0, 1.4), theme, {"Visited": 12, "Installed": 4, "Paid": 1})


def test_a_milestone_is_a_mark_and_a_date_where_a_bar_would_be_a_sliver(charts, slide, theme) -> None:
    """A `gantt` bar says how long something took, and a milestone took no time:
    drawn as a bar it comes out as MIN_LENGTH, which reads as a rounding error.
    Sorted here too, because a timeline given out of order is not one.
    """
    band = charts.Box(0.72, 1.0, 12.61, 3.0)
    drawn = charts.milestone(
        slide,
        band,
        theme,
        [("Series A", "2025-09-09"), ("Seed", "2024-05-14"), ("Platform", "2025-03-20")],
    )

    marks = _discs(slide)
    assert len(marks) == 3
    written = {text: where for text, where, _, _ in _labels(slide)}
    assert "2024-05-14" in written, "every mark carries its own date"
    assert written["Seed"][0] < written["Platform"][0] < written["Series A"][0], "sorted into time order"
    assert written["Seed"][1] != pytest.approx(written["Platform"][1]), "consecutive blocks take turns"
    assert drawn.where("2024-05-14") == pytest.approx(min((m[0] + m[2]) / 2 for m in marks), abs=0.01)
    charts.milestone(slide, band, theme, [("Kick-off", 0), ("Beta", 6), ("GA", 13)])
    with pytest.raises(ValueError, match="numbers or ISO dates"):
        charts.milestone(slide, band, theme, [("Whenever", "soon")])
    with pytest.raises(charts.TooSmall, match="needs about"):
        charts.milestone(slide, charts.Box(0.72, 1.0, 12.61, 1.4), theme, [("Seed", "2024-05-14")])


def test_a_box_plot_gives_an_outlier_a_mark_instead_of_the_end_of_a_whisker(charts, slide, theme) -> None:
    """The whole reason to draw this rather than a bar of averages.

    One slow request in nine stretches the reach to four times the spread, and a
    reader takes the width of the shape off the page without reading a number. So
    the whiskers stop at Tukey's fence and what is past it gets its own mark.
    """
    box = _box(charts, y1=5.0)
    drawn = charts.box_plot(slide, box, theme, [("Platform", _TEN_LATENCIES)], unit="ms")

    marks = _discs(slide)
    assert len(marks) == 1, "only the outlier is a mark"
    rules = sorted(
        (bar for bar in _bars(slide) if bar[3] - bar[1] < 0.03 and bar[2] - bar[0] > 0.3), key=lambda bar: bar[1]
    )
    whisker = rules[0]
    assert whisker[2] < marks[0][0], "the reach stops at the fence and not at the outlier"
    assert whisker[0] == pytest.approx(drawn.where(180), abs=0.02)
    assert whisker[2] == pytest.approx(drawn.where(340), abs=0.02)
    quarters = [bar for bar in _bars(slide) if bar[3] - bar[1] > 0.2 and bar[2] - bar[0] > 0.1]
    assert len(quarters) == 1
    assert quarters[0][0] == pytest.approx(drawn.where(215.5), abs=0.02), "q1"
    assert quarters[0][2] == pytest.approx(drawn.where(286.25), abs=0.02), "q3"
    assert "254ms" in {text for text, _, _, _ in _labels(slide)}, "the median is the number the page carries"


def test_a_box_plot_takes_the_five_numbers_as_readily_as_the_observations(charts, slide, theme) -> None:
    """`histogram`'s two ways in, on the form that has the same choice: a page that
    was handed a p50 and a p95 has no observations to hand over."""
    box = _box(charts, y1=5.0)
    drawn = charts.box_plot(slide, box, theme, [("Platform", 180, 215.5, 254, 286.25, 340)], unit="ms")

    quarters = [bar for bar in _bars(slide) if bar[3] - bar[1] > 0.2 and bar[2] - bar[0] > 0.1]
    assert len(quarters) == 1
    assert quarters[0][0] == pytest.approx(drawn.where(215.5), abs=0.02)
    assert quarters[0][2] == pytest.approx(drawn.where(286.25), abs=0.02)
    assert "254ms" in {text for text, _, _, _ in _labels(slide)}
    assert not _discs(slide), "five numbers carry no observations, so there is nothing to call an outlier"
    with pytest.raises(ValueError, match="low, q1, median, q3, high"):
        charts.box_plot(slide, box, theme, [("Platform", 10, 14, 13, 12, 15)])
    with pytest.raises(ValueError, match="rows, not"):
        charts.box_plot(slide, box, theme, [("Platform", 10, 12)])


def test_a_treemap_gives_every_part_the_share_of_the_area_its_value_is(charts, slide, theme) -> None:
    """The form for the parts that are too many and too uneven for a 100% bar.

    Six ecosystem figures spanning two orders of magnitude are six segments of one
    bar, five of which are thinner than their own names. As areas they are all on
    the page and the big ones are still the big ones -- which only holds if the
    area really is the value, so that is what is measured.
    """
    import itertools

    box = _box(charts, y1=5.0)
    parts = [("Downloads", 1400), ("Stars", 410), ("Calls", 186), ("Docs", 95), ("Customers", 60), ("Frameworks", 24)]
    charts.treemap(slide, box, theme, parts)

    tiles = _bars(slide)
    assert len(tiles) == len(parts)
    # The drawn tile is inset by a hundredth a side to leave the gutter, so the
    # gutter goes back on before the area is compared with the value.
    areas = sorted((tile[2] - tile[0] + 0.02) * (tile[3] - tile[1] + 0.02) for tile in tiles)
    whole = box.w * box.h
    total = sum(value for _, value in parts)
    for area, (_, value) in zip(areas, sorted(parts, key=lambda part: part[1])):
        assert area / whole == pytest.approx(value / total, rel=0.03), f"{value} took {area / whole:.4f} of the box"
    for one, other in itertools.combinations(tiles, 2):
        assert _apart(one, other, tolerance=0.0), "two tiles on top of each other is two areas nobody can read"
    with pytest.raises(ValueError, match="zero or negative"):
        charts.treemap(slide, box, theme, [("Downloads", 1400), ("Nothing", 0)])
    with pytest.raises(charts.TooSmall, match="needs about"):
        charts.treemap(slide, charts.Box(0.72, 1.0, 2.0, 1.1), theme, parts)


def test_a_marimekko_makes_a_column_as_wide_as_its_own_total(charts, slide, theme) -> None:
    """What equal-width columns throw away.

    Six segments and a winner in each is the page every deck in the corpus
    carries, and drawn as equal columns it says the winner of the smallest segment
    won as much as the winner of the biggest. The width is the segment and the
    height is the share, so the area is what the part is worth against the page.
    """
    box = _box(charts, y1=5.0)
    charts.marimekko(slide, box, theme, ["Big", "Small"], {"Us": [80, 10], "Them": [120, 40]}, unit="")

    segments = [bar for bar in _bars(slide) if bar[3] - bar[1] > 0.2]
    assert len(segments) == 4
    columns = {}
    for segment in segments:
        columns.setdefault(round(segment[0], 2), []).append(segment)
    assert len(columns) == 2, "two columns, each of its parts flush with the others"
    widths = sorted((max(s[2] for s in stack) - min(s[0] for s in stack) for stack in columns.values()), reverse=True)
    assert widths[0] / sum(widths) == pytest.approx(200 / 250, rel=0.03)
    wide = columns[min(columns, key=lambda x0: -(max(s[2] for s in columns[x0]) - x0))]
    heights = sorted(segment[3] - segment[1] for segment in wide)
    assert heights[0] / sum(heights) == pytest.approx(80 / 200, rel=0.02), "and the height is the share inside it"
    assert {"200", "50"} <= {text for text, _, _, _ in _labels(slide)}, "a width nobody can read is half a chart"
    with pytest.raises(ValueError, match="may be negative"):
        charts.marimekko(slide, box, theme, ["Big"], {"Us": [-1]})
    with pytest.raises(charts.TooSmall, match="needs about"):
        charts.marimekko(slide, charts.Box(0.72, 1.0, 1.6, 1.6), theme, ["Big", "Small"], {"Us": [80, 10]})


def test_the_forms_added_after_the_first_fifteen_are_all_reachable_by_name(charts) -> None:
    """A chart the catalogue does not list is one an author draws by hand instead.

    `chart_names` is read off the module rather than kept beside it, and the three
    measuring calls take a name as readily as the function, so this is the check
    that the new forms arrived through the same door as the old ones rather than
    beside it.
    """
    added = {"dot_plot", "line", "combo", "funnel", "milestone", "box_plot", "treemap", "marimekko"}
    assert added <= _drawn_charts()
    for name in sorted(added):
        assert charts._chart_of(name) is getattr(charts, name)
    assert set(_drawn_charts()) == {name for name, _, _ in _EVERY_CHART}
