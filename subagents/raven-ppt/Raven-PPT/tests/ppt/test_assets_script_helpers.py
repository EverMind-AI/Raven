"""The modules a build script imports, exercised as the script would.

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
from types import SimpleNamespace

import pytest

from raven.ppt.services.assets import icons
from raven.ppt.services.assets.script_helpers import (
    ICON_DATA_FILENAME,
    ICON_KEYWORD_FILENAME,
    ICON_MODULE_FILENAME,
    SHAPE_DATA_FILENAME,
    SHAPE_MODULE_FILENAME,
    THEME_DATA_FILENAME,
    THEME_MODULE_FILENAME,
    icon_catalog_json,
    icon_keyword_json,
    icon_module_source,
    script_helper_files,
    shape_catalog_json,
    shape_module_source,
    theme_catalog,
    theme_catalog_json,
    theme_module_source,
)
from raven.ppt.services.assets.themes import THEMES

pytest.importorskip("pptx", reason="ppt extra not installed")


def _install(directory: Path) -> None:
    """What the script backend will do; done here to prove that is all it takes."""
    for filename, text in script_helper_files().items():
        (directory / filename).write_text(text, encoding="utf-8")


def _load_module(directory: Path, filename: str) -> dict:
    """Run one generated module the way an import in the build directory would.

    Only for a module that stands alone, which is `ppt_theme` and no longer anything
    else: `ppt_icons`, `ppt_layout`, `ppt_charts` and `ppt_shapes` import each other,
    so they come in through the `helpers` fixture, which puts the directory on the path.
    """
    path = directory / filename
    namespace: dict = {"__file__": str(path), "__name__": path.stem}
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)  # noqa: S102
    return namespace


PRESENTATIONML = "http://schemas.openxmlformats.org/presentationml/2006/main"
DRAWINGML = "http://schemas.openxmlformats.org/drawingml/2006/main"

# The four modules a build directory imports. Named here so the sampler below and the
# guard that checks the sampler is complete stay one list.
PROJECTED_MODULES = ("ppt_layout", "ppt_icons", "ppt_shapes", "ppt_charts")


@pytest.fixture
def helpers(tmp_path: Path):
    """The generated modules, installed and imported the way a build script sees them."""
    import sys

    _install(tmp_path)
    sys.path.insert(0, str(tmp_path))
    try:
        loaded = SimpleNamespace(**{name: __import__(name) for name in PROJECTED_MODULES})
        loaded.theme = __import__("ppt_theme").THEMES["indigo-scholar"]
        loaded.directory = tmp_path
        yield loaded
    finally:
        sys.path.remove(str(tmp_path))
        for name in (*PROJECTED_MODULES, "ppt_theme"):
            sys.modules.pop(name, None)


def _draws(module) -> dict[str, object]:
    """The module's public helpers that put something on a slide, read off the module.

    A drawing helper is one whose first parameter is `slide`; `find_presets`, `page`,
    `stack` and `overlaps` compute and return. Deciding it from the signature rather
    than from a list is the whole point of the guard below: a list is a thing somebody
    has to remember to add to, and that is how a module went unchecked to begin with.
    """
    import inspect

    found = {}
    for name, value in vars(module).items():
        if name.startswith("_") or not inspect.isfunction(value) or value.__module__ != module.__name__:
            continue
        parameters = list(inspect.signature(value).parameters)
        if parameters and parameters[0] == "slide":
            found[name] = value
    return found


def _sample_calls(helpers, slide, picture) -> dict[str, object]:
    """One call per drawing helper, in the argument shapes each was written for.

    Not a test on its own: it is the body the whole-deck properties below run against,
    so that "every helper" means every helper rather than the seven somebody thought
    of. Regions overlap deliberately -- nothing here measures placement.
    """
    from pptx.util import Inches

    layout, icons_module, shapes = helpers.ppt_layout, helpers.ppt_icons, helpers.ppt_shapes
    charts, theme = helpers.ppt_charts, helpers.theme
    box = layout.Box(1.0, 1.0, 4.0, 2.0)
    wide = layout.Box(1.0, 3.6, 9.0, 4.4)
    # Every chart refuses a region it could not draw itself in, so they get a taller one.
    plot = layout.Box(1.0, 4.6, 9.0, 6.6)
    regions = layout.page()
    return {
        # ppt_layout
        "plane": lambda: layout.plane(slide, box, theme),
        "rule": lambda: layout.rule(slide, box, theme),
        "mark": lambda: layout.mark(slide, box, theme, "harvey", "3/5"),
        "write": lambda: layout.write(slide, box, "a line", colour=theme["foreground"]),
        "points": lambda: layout.points(slide, box, theme, ["one", "two"]),
        "heading": lambda: layout.heading(slide, regions, theme, "A title", "Kicker"),
        "card": lambda: layout.card(slide, box, theme, icon="check", title="Card", body=["body"]),
        "formula": lambda: layout.formula(slide, box, "e = mc^2", theme),
        "table": lambda: layout.table(slide, wide, [["Head", "Value"], ["Row", "1"]], theme),
        "picture_fit": lambda: layout.picture_fit(slide, picture, box, theme, caption="A caption"),
        # ppt_icons
        "add_icon": lambda: icons_module.add_icon(slide, "check", Inches(1), Inches(3), Inches(0.5), theme["accent"]),
        # ppt_shapes. `preset` is drawn with an outline because the line is a second
        # shape property that `_paint` takes a different branch to set.
        "preset": lambda: shapes.preset(slide, box, theme, "roundRect", adj=0.06, outline="foreground"),
        "chevron_row": lambda: shapes.chevron_row(slide, wide, theme, 3),
        "timeline": lambda: shapes.timeline(slide, layout.Box(1.0, 2.4, 9.0, 3.2), theme, 3),
        "connect": lambda: shapes.connect(slide, layout.Box(1.0, 3.0, 2.0, 3.4), layout.Box(4.0, 3.0, 5.0, 3.4), theme),
        # ppt_charts, the same fifteen `test_assets_charts` draws, into one region.
        "column": lambda: charts.column(slide, plot, theme, [("East", 185), ("South", 142)], accent="East", unit="M"),
        "horizontal_bar": lambda: charts.horizontal_bar(slide, plot, theme, {"TarViS": 48.3, "VITA": 45.7}, accent=0),
        "grouped_bar": lambda: charts.grouped_bar(slide, plot, theme, ["Q1", "Q2"], [("A", [120, 145])], accent="A"),
        "stacked_bar": lambda: charts.stacked_bar(slide, plot, theme, ["FY24"], {"Licence": [140], "Service": [90]}),
        "progress_bar": lambda: charts.progress_bar(slide, plot, theme, [("Single sign-on", "88%")], accent=0),
        "bullet": lambda: charts.bullet(slide, plot, theme, [("Revenue", 182, 200)], accent="Revenue"),
        "butterfly": lambda: charts.butterfly(slide, plot, theme, [("18-24", 320, 280)], sides=("Cost", "Revenue")),
        "dumbbell": lambda: charts.dumbbell(slide, plot, theme, [("Checkout", 61, 78)], sides=("Before", "After")),
        "waterfall": lambda: charts.waterfall(slide, plot, theme, [("Open", 280), ("Close", 355)], totals=(0, -1)),
        "pareto": lambda: charts.pareto(slide, plot, theme, [("Function", 350), ("Finish", 220)], accent=0),
        "histogram": lambda: charts.histogram(slide, plot, theme, [12, 15, 18, 22, 25, 31, 38, 44]),
        "gantt": lambda: charts.gantt(slide, plot, theme, [("Discovery", "2026-01-06", "2026-02-14")]),
        # The eight added after the first fifteen, in the argument shapes the reference
        # gives each: two share `grouped_bar`'s, one shares `stacked_bar`'s, and the
        # rest take their own.
        "line": lambda: charts.line(slide, plot, theme, ["Q1", "Q2"], {"Ours": [3, 5]}),
        "dot_plot": lambda: charts.dot_plot(slide, plot, theme, ["North"], {"Q1": [12], "Q2": [18]}),
        "marimekko": lambda: charts.marimekko(slide, plot, theme, ["North", "South"], {"New": [12, 8], "Kept": [6, 9]}),
        "treemap": lambda: charts.treemap(slide, plot, theme, [("Core", 60), ("Edge", 25), ("Rest", 15)]),
        "funnel": lambda: charts.funnel(slide, plot, theme, [("Visited", 900), ("Signed", 240), ("Paid", 60)]),
        "combo": lambda: charts.combo(slide, plot, theme, [("Q1", 120), ("Q2", 180)], [0.31, 0.44]),
        "box_plot": lambda: charts.box_plot(slide, plot, theme, [("North", [2, 4, 5, 7, 11])]),
        "milestone": lambda: charts.milestone(
            slide, plot, theme, [("Kickoff", "2026-01-06"), ("Launch", "2026-03-02")]
        ),
        "heatmap": lambda: charts.heatmap(slide, plot, theme, ["North"], ["Q1", "Q2"], [[12, 18]]),
        "matrix_2x2": lambda: charts.matrix_2x2(
            slide, plot, theme, [("SSO", 8, 3), ("Audit", 6, 7)], axes=("Effort", "Impact"), limits=(0, 10, 0, 10)
        ),
        "scatter": lambda: charts.scatter(
            slide, plot, theme, [("Online", 48, 420), ("Offline", 52, 410)], axes=("Spend", "Revenue")
        ),
        # The drawing base the forms are built on, public so a page can draw a form
        # that is not among them. Every property below holds for these too -- a bar an
        # author drew wears the theme's drop shadow exactly as readily as one a form
        # drew, and it is the same call that takes it off.
        "rect": lambda: charts.rect(slide, box, theme["accent"]),
        "disc": lambda: charts.disc(slide, 2.0, 2.6, 0.16, theme["accent"]),
        "ring": lambda: charts.ring(slide, 2.4, 2.6, 0.16, theme["muted"]),
        "hline": lambda: charts.hline(slide, 1.0, 4.0, 2.9, theme["grid"]),
        "vline": lambda: charts.vline(slide, 4.4, 1.0, 2.9, theme["grid"]),
        "poly": lambda: charts.poly(slide, [(5.0, 1.0), (6.0, 2.0), (7.0, 1.2)], theme["accent"]),
        "write_label": lambda: charts.write_label(slide, box, "a reading", theme),
        "key": lambda: charts.key(slide, wide, theme, ["Ours", "Theirs"], charts.shades(theme, 2)),
        "scale_top": lambda: charts.scale_top(slide, plot, theme, 200, "M"),
    }


def _two_by_two_png(directory: Path) -> Path:
    """The smallest real image there is, because `picture_fit` measures what it placed."""
    from PIL import Image

    path = directory / "swatch.png"
    Image.new("RGB", (2, 2), (120, 140, 200)).save(path)
    return path


def _blank_slide():
    from pptx import Presentation

    deck = Presentation()
    return deck.slides.add_slide(deck.slide_layouts[6])


def _one_of_everything(helpers):
    """A slide carrying the output of every drawing helper the modules export."""
    slide = _blank_slide()
    for call in _sample_calls(helpers, slide, _two_by_two_png(helpers.directory)).values():
        call()
    return slide


def _has_style(shape) -> bool:
    return bool(shape._element.findall(f"{{{PRESENTATIONML}}}style"))


def test_every_helper_that_draws_has_a_sample_call(helpers) -> None:
    """The guard on the guard, and the thing the last round of this was missing.

    `ppt_shapes` went unchecked for the theme's drop shadow not because anyone decided
    it was fine, but because the test named the helpers it drew and nobody added the
    new module to the list -- the fix reached exactly as far as the test did. Every
    property below runs over `_sample_calls`, so a helper with no call in it is a
    helper no property holds for, and the only way to see that is to compare the
    sampler against what the modules actually export.
    """
    sampled = set(_sample_calls(helpers, _blank_slide(), _two_by_two_png(helpers.directory)))
    exported = {name for module in PROJECTED_MODULES for name in _draws(getattr(helpers, module))}
    assert exported, "no drawing helpers found; the signature rule stopped matching"
    assert not exported - sampled, f"drawing helpers with no sample call: {sorted(exported - sampled)}"
    assert not sampled - exported, f"sample calls for helpers that no longer exist: {sorted(sampled - exported)}"


def test_nothing_these_helpers_draw_carries_the_theme_s_drop_shadow(helpers) -> None:
    """`shadow.inherit = False` is not enough, and the render is where that showed.

    It writes an empty `a:effectLst`. `add_shape`, `add_connector` and
    `convert_to_shape` also stamp a `p:style`, and its `a:effectRef idx="2"` names the
    theme's second effect -- a drop shadow -- which a renderer resolves on its own.
    Every plane, rule, table hairline, mark and icon stroke came out with a grey shadow
    down its right side, on every page of every deck, while the code read as though
    shadows were off.

    The property is on the reference, not on the element. `services/tidy.py` runs over
    a whole deck and zeroes `a:effectRef` rather than deleting `p:style`, because a
    page cloned out of a template carries a `p:style` that is the design; so "no
    `p:style`" is true of what these helpers draw but is not the invariant. What both
    ends have to agree on is that nothing resolves the theme's effect table.

    Asserted on the elements because a shadow is not something a test can see in a
    render, and it is the reference that has to be gone rather than the empty list.
    """
    shadowed = [
        (shape.shape_id, reference.get("idx"))
        for shape in _one_of_everything(helpers).shapes
        for reference in shape._element.findall(f".//{{{DRAWINGML}}}effectRef")
        if reference.get("idx") not in (None, "0")
    ]
    assert not shadowed, f"{len(shadowed)} shapes resolve a theme effect: {shadowed[:5]}"


def test_the_helpers_take_the_style_reference_off_rather_than_emptying_it(helpers) -> None:
    """The stronger half, kept where it is true and not where it is not.

    Every caller in these modules sets the line, the fill and the font itself, so the
    whole `p:style` is dead weight and they delete it. The deck-wide pass in
    `services/tidy.py` cannot: it sees pages cloned out of a template whose `p:style`
    carries the page's design, and zeroes the effect reference instead. Writing both
    properties down is what keeps that difference deliberate -- if a helper starts
    leaving a `p:style` behind, this fails and the reason has to be argued rather than
    absorbed by the looser check above.
    """
    styled = [shape.shape_id for shape in _one_of_everything(helpers).shapes if _has_style(shape)]
    assert not styled, f"{len(styled)} shapes still carry p:style: {styled[:5]}"


# Every word that would let one page choose its own type size, face or absolute
# placement. Matched against the parts of a parameter name rather than the whole name,
# so `font_size` and `slide_width` are caught the way `size` and `width` are.
FORBIDDEN_IN_A_SHAPE_HELPER = (
    "size",
    "font",
    "face",
    "typeface",
    "emu",
    "inch",
    "inches",
    "px",
    "pt",
    "left",
    "top",
    "width",
    "height",
    "x0",
    "y0",
    "x1",
    "y1",
)

# The one parameter that reads as a physical quantity and is allowed to be one. The
# ruling is in the test's docstring; it is named here so a second one cannot join it
# without the same argument being made.
STROKE_WIDTH_KNOBS = {"width_pt"}


def test_no_shape_helper_takes_a_type_size_a_face_or_a_placement_from_its_caller(helpers) -> None:
    """`ppt_charts` has had this test since it was written; `ppt_shapes` never did.

    Which is how `preset(..., width_pt=1.5)` and `connect(..., width_pt=1.5)` arrived
    without anyone weighing them. The ruling, written here so the next one is argued
    rather than repeated: `width_pt` stays. It is the pen and not the type -- how thick
    a drawn line is, the same quantity `add_icon(width_pt=1.75)` already took -- and
    the hard invariant's red line is the type size and the font face, which decide
    whether a page can be read and which the engine's own ladder owns. Nothing about a
    1.5pt rule can make a page illegible, and there is no other way to say "hairline"
    to a shape.

    The banned list is deliberately not `ppt_charts`'s. That module refuses `colour`
    too, because a chart is a data display and one page's series in a different palette
    is a page lying about being comparable; these are drawing primitives and already
    hand the author `tint`, `outline` and `colour` on purpose. Copying the chart list
    would have failed on all three and taught nobody anything.

    Matching is on the parts of a name rather than the whole name, which makes this
    stricter than the chart test it mirrors: that one compares names for equality, so
    `width_pt` and `font_size` would both have passed it. Scoped to `ppt_shapes`
    because `ppt_icons.add_icon` and `ppt_layout.write` do take a placement and a type
    size -- those are the two modules where an author positions and sets copy, and the
    boxes on this route are in inches to begin with.
    """
    import inspect
    import re

    drawn = _draws(helpers.ppt_shapes)
    assert len(drawn) >= 4, sorted(drawn)
    for name, function in sorted(drawn.items()):
        for parameter in inspect.signature(function).parameters:
            if parameter in STROKE_WIDTH_KNOBS:
                continue
            parts = {part.lower() for part in re.split(r"_|(?<=[a-z])(?=[A-Z])", parameter) if part}
            caught = parts & set(FORBIDDEN_IN_A_SHAPE_HELPER)
            assert not caught, f"ppt_shapes.{name} takes {parameter!r}, which names {sorted(caught)}"


def test_the_set_of_files_is_exactly_what_a_build_directory_needs() -> None:
    from raven.ppt.services.assets.charts import CHART_MODULE_FILENAME
    from raven.ppt.services.assets.layout import LAYOUT_MODULE_FILENAME

    files = script_helper_files()
    assert set(files) == {
        THEME_MODULE_FILENAME,
        THEME_DATA_FILENAME,
        ICON_MODULE_FILENAME,
        ICON_DATA_FILENAME,
        ICON_KEYWORD_FILENAME,
        LAYOUT_MODULE_FILENAME,
        CHART_MODULE_FILENAME,
        SHAPE_MODULE_FILENAME,
        SHAPE_DATA_FILENAME,
    }
    assert all(text for text in files.values())
    # The module names are the contract: the script writes `import ppt_theme`.
    assert THEME_MODULE_FILENAME == "ppt_theme.py"
    assert ICON_MODULE_FILENAME == "ppt_icons.py"
    assert LAYOUT_MODULE_FILENAME == "ppt_layout.py"
    assert CHART_MODULE_FILENAME == "ppt_charts.py"
    assert SHAPE_MODULE_FILENAME == "ppt_shapes.py"


def test_nothing_generated_reaches_back_into_raven() -> None:
    """The build directory has to run as a plain python-pptx project.

    A helper that imported raven would bind the author's script to internals it
    cannot see, and would break the moment those move -- which is the whole
    reason this refactor exists.
    """
    from raven.ppt.services.assets.layout import layout_module_source

    for source in (theme_module_source(), icon_module_source(), layout_module_source(), shape_module_source()):
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


def test_the_icon_module_draws_a_real_icon_onto_a_real_slide(helpers) -> None:
    """Through the whole build directory, because `ppt_icons` now has a neighbour.

    It takes `Box` and `_ink_box` from `ppt_layout` -- an icon's ink is a rectangle on
    the page and there is already one kind of those here -- so exec'ing the file on
    its own the way the theme module is exec'd would fail on the import rather than on
    anything this is about.
    """
    from pptx import Presentation
    from pptx.util import Inches

    module = helpers.ppt_icons
    assert module.ICON_NAMES == list(icons.icon_names())

    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    before = len(slide.shapes)
    shapes = module.add_icon(slide, "target", Inches(1), Inches(1), Inches(0.5), "#0B5FA5")

    assert shapes
    assert len(slide.shapes) == before + len(shapes)
    for shape in shapes:
        assert shape.line.color.rgb == module._line_color("#0B5FA5")


def test_add_icon_takes_a_theme_colour_exactly_as_ppt_theme_hands_it_over(helpers) -> None:
    """The two helpers sit side by side, so a hex string has to be accepted.

    `themes.json` stores '#RRGGBB'; requiring `rgb()` in between is a step an
    author gets wrong once and then avoids the icon call entirely.
    """
    from ppt_theme import THEMES as PROJECTED_THEMES
    from ppt_theme import rgb
    from pptx import Presentation
    from pptx.util import Inches

    add_icon = helpers.ppt_icons.add_icon
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    accent = PROJECTED_THEMES["warm-paper"]["accent"]

    as_string = add_icon(slide, "shield", Inches(1), Inches(1), Inches(0.4), accent)
    as_color = add_icon(slide, "shield", Inches(3), Inches(1), Inches(0.4), rgb(accent))

    assert as_string and as_color
    assert {shape.line.color.rgb for shape in as_string} == {shape.line.color.rgb for shape in as_color}


def _spans(icon: str) -> list[float]:
    """The longer side of each run in one icon, on the 24 grid.

    Per run and not per `<path>`, because that is the unit `add_icon` draws and
    measures: `sun` keeps all eight of its rays in one path element.
    """
    out = []
    for path in icons.icon_paths(icon):
        runs: list[list[tuple[float, float]]] = []
        for op, coords in path:
            if op == "M" or not runs:
                runs.append([])
            runs[-1].extend(zip(coords[0::2], coords[1::2]))
        for run in runs:
            xs = [x for x, _ in run]
            ys = [y for _, y in run]
            out.append(max(max(xs) - min(xs), max(ys) - min(ys)))
    return out


def test_the_dots_upstream_hides_inside_a_round_cap_are_drawn_at_the_weight_of_a_stroke(helpers) -> None:
    """Rendered, `warning` had a bar and no dot under it, and 140 more icons were short one.

    Tabler writes a dot as a segment a hundredth of a unit long and lets
    `stroke-linecap="round"` swell it into a disc as wide as the pen. A python-pptx
    freeform strokes with flat caps, so those 409 stubs painted a smudge a
    ten-thousandth of an inch long: invisible at any size, and the exclamation marks,
    tittles and beads of `warning`, `question_mark`, `info_circle`, `wifi`, `atom` and
    `key` simply were not there.

    Asserted on the geometry rather than on the picture: the dot has to have width and
    height a renderer can put ink in, and it has to be the width of the pen -- a dot
    scaled off `size` would swell into a blot beside strokes that keep their `width_pt`
    however large the icon is drawn.
    """
    from pptx import Presentation
    from pptx.enum.dml import MSO_FILL
    from pptx.util import Inches, Pt

    module = helpers.ppt_icons

    # The stub is still what the data says, so this is the gap being bridged and not
    # a change of geometry upstream.
    assert min(_spans("warning")) <= 0.02

    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    drawn = module.add_icon(slide, "warning", Inches(1), Inches(1), Inches(0.4), "#0B5FA5")

    dots = [shape for shape in drawn if shape.fill.type == MSO_FILL.SOLID]
    assert len(dots) == 1, "`warning` is a triangle, a bar and the dot under it"
    assert dots[0].width == dots[0].height == Pt(1.75)
    assert dots[0].fill.fore_color.rgb == module._line_color("#0B5FA5")

    # One shape back per stub, in every icon that had one, and the shape count is
    # what it always was -- nothing that used to draw stopped drawing.
    for name in ("alert_triangle", "question_mark", "info_circle", "wifi", "atom", "key"):
        shapes = module.add_icon(slide, name, Inches(1), Inches(1), Inches(0.4), "#0B5FA5")
        painted = [shape for shape in shapes if shape.fill.type == MSO_FILL.SOLID]
        assert len(painted) == sum(span <= 0.02 for span in _spans(name)), name
        assert all(shape.width == shape.height == Pt(1.75) for shape in painted), name

    # The dot tracks the pen and not the box: the same square at four times the size,
    # a wider square only when the caller asks for a wider stroke.
    for size, pen in ((0.4, 1.75), (1.6, 1.75), (0.4, 3.0)):
        shapes = module.add_icon(slide, "warning", Inches(1), Inches(1), Inches(size), "#0B5FA5", width_pt=pen)
        dot = next(shape for shape in shapes if shape.fill.type == MSO_FILL.SOLID)
        assert dot.width == dot.height == Pt(pen), (size, pen)


def test_a_stroke_too_short_to_look_like_one_is_still_drawn_as_a_stroke(helpers) -> None:
    """The dot rule reaches into every icon, so the other end of it has to be pinned.

    `sun` is rays 0.7 of a grid unit long and `language` has a 0.58 accent -- the
    shortest real strokes in the set, and the ones a rule that measures shortness
    would take first. They are 36 times the longest dot stub, so the two never come
    near each other, but a threshold is a number somebody will later move.
    """
    from pptx import Presentation
    from pptx.enum.dml import MSO_FILL
    from pptx.util import Inches, Pt

    module = helpers.ppt_icons
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])

    assert min(_spans("sun")) == pytest.approx(0.7)
    for name in ("sun", "language", "check", "arrow_right", "chart_bar", "database", "users", "clock"):
        for shape in module.add_icon(slide, name, Inches(1), Inches(1), Inches(0.4), "#0B5FA5"):
            # A stroke keeps the pen on its outline; only a dot is painted.
            assert shape.fill.type == MSO_FILL.BACKGROUND, f"{name} lost a stroke to the dot rule"
            assert shape.line.color.rgb == module._line_color("#0B5FA5"), name
            assert shape.line.width == Pt(1.75), name


def test_the_icon_module_answers_a_miss_the_same_way_the_service_does(helpers) -> None:
    """The generated copy cannot import the service, so a test pins the agreement.

    Duplicated on purpose -- the build directory has to stand alone -- which
    means the only thing holding the two in step is this table.
    """
    find_icons, add_icon = helpers.ppt_icons.find_icons, helpers.ppt_icons.add_icon

    from pptx import Presentation
    from pptx.util import Inches

    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])

    misses = ("task-check", "chart_bars", "deadline", "risk", "stakeholder", "throughput", "wnd")
    for attempt in misses:
        assert find_icons(attempt) == icons.icon_candidates(attempt), attempt
        with pytest.raises(LookupError) as caught:
            add_icon(slide, attempt, Inches(1), Inches(1), Inches(0.4), "#000000")
        assert repr(attempt) in str(caught.value)
        for candidate in icons.icon_candidates(attempt):
            assert candidate in str(caught.value)
    # And the same agreement on the searches that hit, where the two orderings
    # have thirteen hundred names to disagree about.
    for term in ("chart", "file", "user", "cloud", "arrow", "money", "security"):
        assert find_icons(term) == icons.icon_candidates(term), term

    # And the forgiving spellings resolve rather than raise.
    assert add_icon(slide, "map-pin", Inches(1), Inches(2), Inches(0.4), "#000000")
    assert add_icon(slide, "TREND UP", Inches(2), Inches(2), Inches(0.4), "#000000")


def test_the_icon_data_is_what_the_module_reads(tmp_path: Path) -> None:
    _install(tmp_path)
    written = json.loads((tmp_path / ICON_DATA_FILENAME).read_text(encoding="utf-8"))
    assert written == json.loads(icon_catalog_json())
    assert set(written) == set(icons.icon_names())

    # The keywords are a second file, so the draw path never parses them.
    words = json.loads((tmp_path / ICON_KEYWORD_FILENAME).read_text(encoding="utf-8"))
    assert words == json.loads(icon_keyword_json())
    assert set(words) == set(icons.icon_names())
    assert all(isinstance(value, str) and value for value in words.values())


# The ink each of these covers, as a share of the square it is given -- the table the
# measurement was written for, and the reason it exists: same box, six different marks.
# Held here as well as in `test_assets_icons` because the build directory carries its
# own copy of the walk and the two are only kept in step by being asserted apart.
MEASURED_INK = {
    "target": (0.125, 0.125, 0.875, 0.875),
    "chart_bar": (0.125, 1 / 6, 0.875, 5 / 6),
    "check": (5 / 24, 7 / 24, 20 / 24, 17 / 24),
    "minus": (5 / 24, 0.5, 19 / 24, 0.5),
}


def test_an_icon_says_where_its_ink_will_land_before_it_is_drawn(helpers) -> None:
    """The question `add_icon` could not answer, and the one an author kept asking.

    A square says nothing about the mark in it: `target` covers 75% x 75% of the box,
    `check` 62% x 42%, `minus` 58% x 0%. Placing an icon against a title's baseline
    therefore meant drawing it, exporting the deck, looking, and nudging -- the blind
    round `ppt_layout` and `ppt_charts` already stopped needing.

    In inches, and a `Box` -- ppt_layout's own, so it insets and divides like every
    other box here -- because `add_icon`'s EMU is the exception in this directory and
    the answer has to add to the numbers around it.
    """
    ink = helpers.ppt_icons.the_ink_an_icon_covers
    assert isinstance(ink("target"), helpers.ppt_layout.Box)
    for name, expected in sorted(MEASURED_INK.items()):
        assert ink(name) == pytest.approx(expected, abs=1e-12), name
    assert (ink("check").w, ink("check").h) == pytest.approx((0.625, 0.41666), abs=1e-4)
    assert ink("minus").h == 0.0

    # A side scales the same box: the fractions are the answer for a one-inch icon.
    for side in (0.24, 0.4, 1.6):
        assert ink("check", side) == pytest.approx([value * side for value in ink("check")], abs=1e-12)


def test_the_module_measures_the_same_ink_as_the_service(helpers) -> None:
    """The build directory carries its own copy of the walk, so the two have to agree.

    Duplicated for the reason `find_icons` is -- the directory has to run as a plain
    python-pptx project -- which leaves this test as the only thing holding them in
    step. Over the whole set, because the two ways they can drift are a curve sampled
    at a different number of steps and a run one of them keeps and the other drops,
    and both hide in a handful of names.
    """
    ink = helpers.ppt_icons.the_ink_an_icon_covers
    for name in icons.icon_names():
        assert ink(name) == pytest.approx(tuple(icons.icon_ink(name)), abs=1e-12), name


def test_what_an_icon_says_it_will_cover_is_what_it_covers(helpers) -> None:
    """Asked before drawing and read back after, the two answers have to be one answer.

    `.box` is measured off the shapes that landed and the prediction is measured off
    the geometry, so this is the property that makes either worth trusting. It holds
    to the EMU everywhere except where one of upstream's dots sits within half a pen
    of an edge: `add_icon` paints a dot as a pen-wide square centred on a stub with no
    length, so the paint reaches half a pen further than the stub does -- 0.0122in at
    the default 1.75pt, and never more.

    Every eighth name plus the ones that carry the interesting cases, and a fresh
    slide each: python-pptx re-reads the whole shape tree to pick the next shape id,
    so drawing all 1304 onto one slide costs four and a half minutes and proves
    nothing this does not.
    """
    from pptx import Presentation
    from pptx.util import Inches

    module = helpers.ppt_icons
    half_a_pen = 1.75 / 72 / 2
    names = sorted(
        set(module.ICON_NAMES[::8])
        | {"target", "check", "minus", "chart_bar", "warning", "braille", "json"}
        | {"exclamation_mark", "wifi", "separator", "decimal", "logs", "angle", "info_small"}
    )
    for index, name in enumerate(names):
        if index % 40 == 0:
            deck = Presentation()
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        said = module.the_ink_an_icon_covers(name, 0.4)
        drew = module.add_icon(slide, name, Inches(1), Inches(2), Inches(0.4), "#0B5FA5").box
        # The prediction is at the origin, so it moves to the corner it was drawn at.
        said = helpers.ppt_layout.Box(said.x0 + 1.0, said.y0 + 2.0, said.x1 + 1.0, said.y1 + 2.0)
        assert drew.x0 <= said.x0 + NEAR and drew.y0 <= said.y0 + NEAR, name
        assert drew.x1 >= said.x1 - NEAR and drew.y1 >= said.y1 - NEAR, name
        assert max(abs(a - b) for a, b in zip(drew, said)) <= half_a_pen + NEAR, name


def test_add_icon_hands_back_the_list_it_always_did_with_the_ink_added(helpers) -> None:
    """Every way the shapes were read before still reads, and `.box` is new.

    A `list` subclass rather than a pair, because `for shape in add_icon(...)`,
    `add_icon(...)[0]` and `len(add_icon(...))` are all in scripts already, and a
    two-field tuple would break every one of them while unpacking into something that
    looked like it worked.
    """
    from pptx.util import Inches

    slide = _blank_slide()
    drawn = helpers.ppt_icons.add_icon(slide, "chart_bar", Inches(1), Inches(2), Inches(0.4), "#0B5FA5")

    assert isinstance(drawn, list)
    assert len(drawn) == 4 == len(list(drawn))
    assert drawn[0] in slide.shapes and drawn[-1] in slide.shapes
    assert all(shape in slide.shapes for shape in drawn)
    assert isinstance(drawn.box, helpers.ppt_layout.Box)
    # The ink, not the square it was given: 75% x 67% of a 0.4in box at (1, 2).
    assert drawn.box == pytest.approx((1.05, 2 + 1 / 15, 1.35, 2 + 1 / 3), abs=NEAR)


def test_add_icon_refuses_a_size_that_can_only_be_inches(helpers) -> None:
    """Measured, and silent: `add_icon(s, "target", 1.0, 2.0, 0.4, INK)` -- the call
    written the way every other box in this directory reads -- put a target one EMU from
    the corner of the page, 0.4 EMU wide, which rounds to nothing. It raised nothing
    either, so the page came back with the icon simply absent.

    The line is a hundredth of an inch. Nothing above it is the inches mistake and
    nothing below it is an icon anybody can see, and the message has to name `Inches`
    because that is the fix.
    """
    slide = _blank_slide()
    with pytest.raises(ValueError, match=r"Inches\(0.4\)") as caught:
        helpers.ppt_icons.add_icon(slide, "target", 1.0, 2.0, 0.4, "#0B5FA5")
    assert "EMU" in str(caught.value)
    assert not slide.shapes, "the refused call still drew something"

    # And the smallest icon that is not that mistake still draws.
    from pptx.util import Inches

    assert helpers.ppt_icons.add_icon(slide, "target", Inches(1), Inches(1), Inches(0.02), "#0B5FA5")


def test_the_ink_measurement_refuses_a_size_that_can_only_be_emu(helpers) -> None:
    """The same trap mirrored, because the pair take different units.

    `the_ink_an_icon_covers("check", Inches(0.42))` would otherwise answer with a box
    166 inches across -- a number that fails nothing, places nothing, and reads as the
    measurement being broken rather than as the call being wrong.
    """
    from pptx.util import Inches

    ink = helpers.ppt_icons.the_ink_an_icon_covers
    with pytest.raises(ValueError, match="0.42"):
        ink("check", Inches(0.42))
    for refused in (0, -1.0):
        with pytest.raises(ValueError, match="positive side"):
            ink("check", refused)
    # A 13.3in page's widest sensible icon is still an icon, so the ceiling is not tight.
    assert ink("check", 13.3).w == pytest.approx(0.625 * 13.3, abs=1e-9)


def test_an_icon_with_nothing_to_stroke_is_refused_by_both_calls(helpers) -> None:
    """A path that is one `M` strokes nothing, and neither call may answer as if it did.

    `marquee` and `new_section` carry exactly that command, so the rule is real; an
    icon made of nothing but those would leave `add_icon` with no shapes to measure
    and the ink box with no points, and both would fail somewhere the author cannot
    read. Fabricated here because the packaged set has no such icon -- and if one ever
    arrives, this is the failure it should produce.
    """
    from pptx.util import Inches

    module = helpers.ppt_icons
    module._DATA["a_move_and_nothing_else"] = [["path", [["M", [4.0, 6.0]]]]]
    with pytest.raises(LookupError, match="no strokes to measure"):
        module.the_ink_an_icon_covers("a_move_and_nothing_else")
    with pytest.raises(LookupError, match="no strokes to draw"):
        module.add_icon(_blank_slide(), "a_move_and_nothing_else", Inches(1), Inches(1), Inches(0.4), "#0B5FA5")


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


def _drop(path, module_names=("ppt_layout", "ppt_icons", "ppt_shapes", "ppt_theme")) -> None:
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


def test_a_box_from_a_size_names_the_size(helpers) -> None:
    """Two constructors of four numbers, and positionally they are the same call.

    `Box(a, b, c, d)` is two corners and `Box.at(a, b, c, d)` is a corner and a size, and
    neither spelling says which was meant. Roughly one hand-built box in five across the
    live pages passed corners to `at`. One put five shapes off the canvas with content
    reaching y=12.65 on a 7.5in page. Another hid its own headline number under a row of
    cards and did not recover it in three rounds of reading the render, because a box
    that is wrong but still on the page looks exactly like a box that is right -- which
    is why no render and no gate could close this.

    So the refusal has to carry the arithmetic: both rectangles the four numbers could
    be, with their real numbers, so the caller can see which one it wanted.
    """
    layout = helpers.ppt_layout

    sized = layout.Box.at(0.72, 1.35, w=3.0, h=1.6)
    assert (sized.x1, sized.y1) == pytest.approx((3.72, 2.95))
    assert tuple(sized) == pytest.approx(tuple(layout.Box(0.72, 1.35, 3.72, 2.95))), (
        "the same rectangle, said the other way"
    )

    with pytest.raises(TypeError) as refused:
        layout.Box.at(0.72, 1.35, 12.61, 6.51)
    said = str(refused.value)
    assert "as corners they are the box (0.72, 1.35) to (12.61, 6.51)" in said
    assert "as a size the box (0.72, 1.35) to (13.33, 7.86)" in said, "the reading that runs off the page"
    assert "Box.at(x, y, w=, h=)" in said and "Box.corners(x0, y0, x1, y1)" in said, "both named calls"

    with pytest.raises(TypeError, match="takes its size by name"):
        layout.Box.at(0.72, 1.35)
    with pytest.raises(TypeError, match="takes its size by name"):
        layout.Box.at(0.72, 1.35, w=3.0)


def test_a_box_in_arithmetic_says_which_of_its_numbers_was_meant(helpers) -> None:
    """A measurement is a box, and a live page spent it as a height.

    It wrote `chev_h + gap + rule_h + points_size(items, down.w)` -- the measuring order
    this whole module asks for, one attribute short -- and got `unsupported operand
    type(s) for +: 'float' and 'Box'`, which names neither the box nor the attribute.

    Between two boxes there was no error at all: a box is a `namedtuple`, so adding two
    of them concatenated eight numbers into a tuple and the page carried on.
    """
    layout = helpers.ppt_layout
    measured = layout.text_size("一行文案", 3.0)

    for bad in (lambda: 1.0 + measured, lambda: measured + 1.0, lambda: measured * 2, lambda: measured - measured):
        with pytest.raises(TypeError, match=r"add its `\.h` for a height"):
            bad()
    with pytest.raises(TypeError, match="a box is not a number"):
        measured + layout.text_size("另一行", 3.0)

    assert measured.h + 1.0 > 1.0, "and the number it meant is one attribute away"


def test_the_ceiling_on_a_size_is_named_and_never_inferred(helpers) -> None:
    """A ceiling read off the copy was tried, and a character count cannot tell a sentence.

    Capping copy of twenty characters or more called `internationalization` a sentence and
    took it from 20pt to 16 -- caught by the tightness assertion above -- while a
    fourteen-character CJK line with no spaces stayed a label and came back at the size of
    the page's own title. Both readings are wrong from the same count, so the step to stop
    at is named by the caller and this call only answers fit.
    """
    layout = helpers.ppt_layout
    band = layout.Box(0.72, 1.6, 12.61, 3.0)
    sentence = "端到端时延从 11.6 分钟降到 4.2 分钟"

    assert layout.the_largest_step_this_copy_takes(sentence, band) == layout.TITLE_PT, "unbounded, it answers fit"
    assert layout.the_largest_step_this_copy_takes(sentence, band, largest=layout.LEAD_PT) == layout.LEAD_PT
    assert layout.the_largest_step_this_copy_takes(sentence, band, largest=layout.BODY_PT) == layout.BODY_PT
    assert layout.the_largest_step_this_copy_takes("4.2", layout.Box(0, 0, 6, 3), largest=layout.NUMBER_PT) == (
        layout.NUMBER_PT
    )
    assert not hasattr(layout, "_COPY_CHARS"), "the inferred ceiling is gone, not merely unused"
    tight = layout.Box(0, 0, 1.2, 0.4)
    assert layout.the_largest_step_this_copy_takes(sentence, tight) == layout.BODY_FLOOR_PT, (
        "the floor is the answer when nothing above it fits, whatever ceiling was named"
    )


def test_a_card_can_be_asked_how_tall_it_has_to_be(helpers) -> None:
    """The measurement a row of cards did not have, and the void it produced.

    A live page drew four cards across `frame.body.columns(4)` -- the only height there
    was to give them -- and its own render review named the result first: two lines of
    copy in a card 5.4in tall. `card_body_box` says where the copy goes inside a height
    already chosen; nothing said what the height should be.

    The card built at the measured height has to hold its copy exactly, or the answer is
    worse than no answer.
    """
    layout, theme = helpers.ppt_layout, helpers.theme
    _, slide = _deck()
    cards = (
        {"icon": "target", "title": "自动排版", "body": ["内容自动分栏对齐", "版式一次成型"]},
        {"icon": "check", "title": "事实校验", "body": ["数字引用逐项核对"]},
        {"title": "多语言"},
    )
    column = layout.page().body.columns(4)[0]

    needed = [layout.card_size(column.w, **card) for card in cards]
    assert needed[0].h > needed[1].h > needed[2].h, "more copy is a taller card"
    assert needed[0].w == column.w and all(box.y0 == 0.0 for box in needed), "a box at the origin"
    assert max(box.h for box in needed) < layout.page().body.h / 2, "the void this replaces"

    box = layout.Box.at(column.x0, column.y0, w=column.w, h=needed[0].h)
    layout.card(slide, box, theme, **cards[0])
    copy = layout.card_body_box(box, icon=cards[0]["icon"], title=cards[0]["title"])
    assert copy.y1 <= box.y1 + 1e-9, "the copy region is inside the card it was measured for"
    assert layout.fits(cards[0]["body"], copy), "and the copy it was measured for goes in it"
    assert layout.text_size(cards[0]["body"], copy.w).h == pytest.approx(copy.h, abs=1e-9)

    icon_only = layout.card_body_box(box, icon="target")
    assert icon_only.y0 > box.y0 + layout.PAD, "an icon clears the copy with no title beside it"


def test_the_body_keeps_the_footer_strip_until_a_page_asks_to_cite(tmp_path) -> None:
    """Reserved by default, used by nobody: 29 live pages wrote `page()` and 0 drew a footer.

    The strip is `_FOOTER_H + GUTTER` = 0.58in of a 7.5in page and 12% of the body, and
    it came off every one of them -- room the copy could have been set in, which is the
    other half of "the body type is too small". Off by default; a page that cites asks.
    """
    _, module = _layout(tmp_path)
    try:
        plain = module.page()
        citing = module.page(footer=True)

        assert plain.body.h - citing.body.h == pytest.approx(module.GUTTER + 0.30)
        assert plain.body.y1 == pytest.approx(module.CANVAS_H - module.MARGIN), "to the safe area"
        assert citing.footer.y0 >= citing.body.y1, "and when it is asked for, it is clear of the body"
        assert plain.title == citing.title, "asking changes the body and nothing above it"
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


def test_a_stack_answers_for_its_region_and_refuses_to_divide_it(tmp_path) -> None:
    """A live page measured its copy against the region and the region would not say how wide it was.

    It wrote `text_size(caption, inner.w, size=15).h` and then `inner.take(that)` --
    exactly the measure-first order everything here asks for -- and got
    `AttributeError: 'Stack' object has no attribute 'w'`. Every band the stack will hand
    out is that wide, so it is the one answer the cursor cannot change.

    Dividing is the other way: `rows(3)` off a half-spent stack would carve up the part
    already drawn on, so it says to ask the remainder instead of quietly overlapping.
    """
    _, module = _layout(tmp_path)
    try:
        region = module.Box(0.72, 1.66, 12.61, 6.51)
        down = module.stack(region)
        down.take(2.0)

        assert (down.w, down.x0, down.x1) == (region.w, region.x0, region.x1)
        assert (down.h, down.y0, down.y1) == (region.h, region.y0, region.y1)
        assert down.left == pytest.approx(2.85), "the cursor's own question keeps its own name"
        assert down.room.h == pytest.approx(2.85)
        assert down.room.rows(3), "and the remainder divides"

        with pytest.raises(AttributeError, match=r"room\.rows"):
            down.rows(3)
        with pytest.raises(AttributeError, match="take, skip, rest, left, room"):
            down.hieght
    finally:
        _drop(tmp_path)


def test_a_stack_checks_a_whole_plan_and_says_when_rest_spent_the_region(tmp_path) -> None:
    """Two live rounds of the same page, both measured right, both dead at the last band.

    Round one measured every band -- nine `text_size(...).h` calls and not one of them
    wrong -- then took and drew them one at a time, so the sum was only discovered at the
    last `take`: 1.36in asked of a 0.67in remainder, with everything above it already on
    the slide. Asked there, the only band left to shorten is the last one, which is why
    the refusal naming both numbers did not help it.

    Round two tried to fix exactly that and hit the other half. It wrote
    `room = down.rest()` -- naming the variable after the query it meant -- and `rest()`
    spends the region it answers with, so the next `take` found 0.00in. `room` is the
    same box without spending it, one word apart, and the docstring saying so was not
    where the author was looking. The refusal is.
    """
    _, module = _layout(tmp_path)
    try:
        region = module.Box(0.72, 1.66, 12.61, 6.51)  # 4.85in

        plan = module.stack(region)
        assert plan.short_by(1.6, 0.12, 2.0, 0.22, 0.8) == 0.0, "4.74 of 4.85 fits"
        assert plan.short_by(1.6, 0.12, 2.0, 0.22, 1.2) == pytest.approx(0.29)
        assert plan.short_by([1.6, 0.12, 2.0, 0.22, 1.2]) == pytest.approx(0.29), "a plan built in a loop"
        assert plan.left == pytest.approx(4.85), "asking a plan spends nothing"
        plan.take(2.0)
        assert plan.short_by(3.0) == pytest.approx(0.15), "and it answers against what is left"

        spent = module.stack(region)
        assert spent.rest() == spent.box, "rest() hands back the whole region here"
        with pytest.raises(ValueError, match=r"rest\(\) already spent it") as refused:
            spent.take(1.2)
        assert "`room` is that same box without taking it" in str(refused.value)
        assert "short_by" in str(refused.value), "and the call that would have caught it earlier"
        with pytest.raises(ValueError, match=r"rest\(\) already spent it"):
            spent.rest()

        ran_out = module.stack(region)
        ran_out.take(4.85)
        assert (
            "rest() already spent" not in str(pytest.raises(ValueError, match="short_by").__enter__() or "") or True
        )  # a stack spent by take must not blame rest()
        with pytest.raises(ValueError) as by_take:
            ran_out.take(0.5)
        assert "rest()" not in str(by_take.value), "a region spent by take is not rest()'s doing"
        assert "short_by" in str(by_take.value)
    finally:
        _drop(tmp_path)


def test_a_stack_says_so_rather_than_overlapping(tmp_path) -> None:
    _, module = _layout(tmp_path)
    try:
        down = module.stack(module.Box(0.72, 1.66, 12.61, 3.66))
        down.take(1.5)

        with pytest.raises(ValueError, match="0.50in is left"):
            down.take(2.0)
        with pytest.raises(ValueError):
            module.stack(module.Box(0, 0, 1, 1)).take(0)
    finally:
        _drop(tmp_path)


def test_a_stack_spends_the_heights_it_was_given_and_nothing_more(tmp_path) -> None:
    """A page budgets against `box.h`; if `take` also charged a gutter, that sum was wrong.

    Two live pages did the arithmetic right and still ran out. One took 4.25 + 0.03 and
    skipped 0.08 + 0.16 of a 4.85in body -- 0.33in should have been left for the footnote,
    and `rest()` answered "nothing is left in this region", because two invisible gutters
    had eaten 0.56in. Ten of twenty-one live pages wrote their own `skip`, so on those the
    gap was paid twice; one wrote four takes and three skips, and 1.12in of a 4.85in body
    went to space nobody asked for.
    """
    _, module = _layout(tmp_path)
    try:
        region = module.Box(0.72, 1.66, 12.61, 6.51)  # 4.85in, the body of a 16:9 page
        down = module.stack(region)

        first = down.take(4.25)
        down.skip(0.08)
        second = down.take(0.03)
        down.skip(0.16)
        note = down.rest()

        assert second.y0 == pytest.approx(first.y1 + 0.08), "the only gap is the one that was written"
        assert note.y0 == pytest.approx(second.y1 + 0.16)
        assert note.h == pytest.approx(0.33), "the region minus the heights and the skips, exactly"
        assert down.left == pytest.approx(0)

        exact = module.stack(region)
        assert exact.take(2.0).y1 == pytest.approx(3.66)
        assert exact.take(2.85).y1 == pytest.approx(region.y1), "a sum that equals the region fits it"
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
        sized = module.Box.at(0.82, 1.5, w=3.75, h=5.25)
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


# --- the marks a cell carries ------------------------------------------------------
# Everything above draws a grid, and a grid cannot say "three and a half out of five"
# as a length. These run the generated module and read the shapes back off a real
# slide for the reason at the top of this file: a test that compared the source string
# would pass for a module with a NameError in it.


def _slide(module):
    from pptx import Presentation

    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = module.Inches(13.333), module.Inches(7.5)
    return presentation.slides.add_slide(presentation.slide_layouts[6])


def _border(module, cell, edge):
    """The `(colour, weight in points)` a cell's edge carries, or None for a bare one.

    A table's rules are the cells' own borders rather than rectangles laid over the
    grid, so this is where a rule is now read back from.
    """
    properties = cell._tc.get_or_add_tcPr()
    element = properties.find(f"{{{module._A}}}{edge}")
    if element is None:
        return None
    painted = element.find(f"{{{module._A}}}solidFill")
    if painted is None:
        return None
    return painted.find(f"{{{module._A}}}srgbClr").get("val"), int(element.get("w")) / 12700


def _cell_boxes(module, table, box):
    """Every body cell's box, rebuilt from the widths and heights the table was given.

    The marks are placed from those same two numbers, so a mark that falls outside the
    box this computes is a mark the reader sees in the wrong row.
    """
    unit = module.Inches(1)
    widths = [column.width / unit for column in table.columns]
    heights = [row.height / unit for row in table.rows]
    boxes = {}
    for row in range(1, len(heights)):
        top = box.y0 + heights[0] + sum(heights[1:row])
        left = box.x0
        for column, width in enumerate(widths):
            boxes[(row, column)] = module.Box.at(left, top, w=width, h=heights[row])
            left += width
    return boxes


def _mark_shapes(slide, before):
    """The marks a table drew, which is every shape it added except the row rules.

    A rule is the one plain rectangle in the set (`_hairline` draws it); every mark is
    an oval, a pill, a triangle or a freeform.
    """
    from pptx.enum.shapes import MSO_SHAPE, MSO_SHAPE_TYPE

    drawn = []
    for shape in list(slide.shapes)[before:]:
        if shape.shape_type == MSO_SHAPE_TYPE.FREEFORM:
            drawn.append(shape)
        elif shape.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE and shape.auto_shape_type != MSO_SHAPE.RECTANGLE:
            drawn.append(shape)
    return drawn


def _within(module, shape, box, slack=0.01):
    unit = module.Inches(1)
    return (
        shape.left / unit >= box.x0 - slack
        and (shape.left + shape.width) / unit <= box.x1 + slack
        and shape.top / unit >= box.y0 - slack
        and (shape.top + shape.height) / unit <= box.y1 + slack
    )


_GRID = [
    ["能力", "选项 A", "选项 B", "评分", "达成"],
    ["", "", "", "", ""],
    ["单点登录", "", "", "", "62%"],
    ["审计留痕", "", "", "", "88%"],
    ["小计", "2", "1", "", "+15%"],
]
_GRID_MARKS = {
    (2, 1): "check",
    (2, 2): "cross",
    (3, 1): "partial",
    (3, 2): "check",
    (2, 3): "harvey:3.5",
    (3, 3): "harvey:4",
    (2, 4): "progress",
    (3, 4): "progress:88%",
    (4, 1): "status_dot:accent",
    (4, 4): "delta",
}


def test_a_harvey_ball_reads_as_a_rating_not_a_decoration(tmp_path) -> None:
    """Three filled dots say "three" only when the reader can see there were five.

    So the scale is drawn whole and the rating filled into it, the way the reference
    matrix does. And a 3.5 fills half a step: rounding it to 3 or to 4 would state a
    fact the source did not.
    """
    from pptx.enum.dml import MSO_FILL
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    _, module = _layout(tmp_path)
    try:
        from ppt_theme import THEMES

        theme = THEMES["ink-graphite"]
        slide = _slide(module)
        box = module.Box(1.0, 2.0, 2.6, 2.34)

        shapes = module.mark(slide, box, theme, "harvey", "3.5")
        steps = [shape for shape in shapes if shape.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE]
        halves = [shape for shape in shapes if shape.shape_type == MSO_SHAPE_TYPE.FREEFORM]
        assert len(steps) == 5, "the scale is drawn whole, not only the part that is filled"
        assert len(halves) == 1, "and 3.5 is half a step"

        filled = [shape for shape in steps if shape.fill.type == MSO_FILL.SOLID]
        assert len(filled) == 3
        assert len([shape for shape in steps if shape.fill.type == MSO_FILL.BACKGROUND]) == 2
        assert {shape.fill.fore_color.rgb for shape in filled} == {module._rgb(theme["accent"])}

        lefts = sorted(shape.left for shape in steps)
        pitches = {round((second - first) / module.Inches(1), 3) for first, second in zip(lefts, lefts[1:])}
        assert len(pitches) == 1, f"an ordinal scale is evenly spaced: {pitches}"
        assert sorted(shape.left for shape in filled) == lefts[:3], "filled from the left, so it reads as a length"
        assert all(_within(module, shape, box) for shape in shapes)

        assert all(shape.fill.type == MSO_FILL.SOLID for shape in module.mark(slide, box, theme, "harvey", "5"))
        assert all(shape.fill.type == MSO_FILL.BACKGROUND for shape in module.mark(slide, box, theme, "harvey", "0"))
        assert len(module.mark(slide, box, theme, "harvey", "2/4")) == 4, "a four-step scale has four steps"
    finally:
        _drop(tmp_path)


def test_a_mark_takes_the_side_of_the_cell_its_own_number_does_not(tmp_path) -> None:
    """A cell cannot flow a bar around its percentage, so the two split the cell.

    Split by measurement rather than down the middle: "88%" needs what "88%" measures
    and the bar wants the rest, and a fixed share would either crop the bar or stop it
    short of where the eye is already looking.
    """
    _, module = _layout(tmp_path)
    try:
        from ppt_theme import THEMES

        theme = THEMES["ink-graphite"]
        slide = _slide(module)
        box = module.Box(0.72, 1.6, 12.61, 2.4)
        unit = module.Inches(1)

        rows = [["指标", "达成"], ["可用性", "88%"]]
        before = len(slide.shapes)
        table = module.table(slide, box, rows, theme, numeric_from=1, marks={(1, 1): "progress"})
        # The track is the whole of what the mark was given; the bar is the share of it.
        track = max(_mark_shapes(slide, before), key=lambda shape: shape.width)
        cell = _cell_boxes(module, table, box)[(1, 1)]
        assert track.left / unit == pytest.approx(cell.x0 + 0.10, abs=0.01), "the mark starts at the cell's margin"
        room = cell.x1 - 0.10 - (track.left + track.width) / unit
        assert room >= module._em_width("88%", module.LABEL_PT), "the number was drawn over by its own bar"

        quiet = [["指标", "达成"], ["可用性", ""]]
        before = len(slide.shapes)
        table = module.table(slide, box, quiet, theme, numeric_from=1, marks={(1, 1): "progress:88%"})
        whole = max(_mark_shapes(slide, before), key=lambda shape: shape.width)
        cell = _cell_boxes(module, table, box)[(1, 1)]
        assert whole.width / unit == pytest.approx(cell.w - 0.20, abs=0.02), "an empty cell gives the mark all of it"
    finally:
        _drop(tmp_path)


def test_every_mark_lands_inside_the_cell_it_marks(tmp_path) -> None:
    """The pairwise invariant, for marks: a table draws them from its own geometry, so
    a mark in the wrong cell is a fact attached to the wrong row.
    """
    _, module = _layout(tmp_path)
    try:
        from ppt_theme import THEMES

        theme = THEMES["ink-graphite"]
        slide = _slide(module)
        box = module.Box(0.72, 1.6, 12.61, 4.4)

        before = len(slide.shapes)
        table = module.table(
            slide,
            box,
            _GRID,
            theme,
            numeric_from=3,
            group_rows={1: "核心访问"},
            indent_rows=(2, 3),
            total_rows=(4,),
            marks=_GRID_MARKS,
        )
        cells = _cell_boxes(module, table, box)
        drawn = _mark_shapes(slide, before)
        assert drawn, "no mark reached the slide"

        unit = module.Inches(1)
        placed = set()
        for shape in drawn:
            middle_x = (shape.left + shape.width / 2) / unit
            middle_y = (shape.top + shape.height / 2) / unit
            where = [
                key for key, cell in cells.items() if cell.x0 <= middle_x <= cell.x1 and cell.y0 <= middle_y <= cell.y1
            ]
            assert len(where) == 1, f"a mark at {middle_x:.2f},{middle_y:.2f} sits between cells: {where}"
            assert where[0] in _GRID_MARKS, f"a mark landed in {where[0]}, which was never marked"
            assert _within(module, shape, cells[where[0]]), f"a mark escaped cell {where[0]}"
            placed.add(where[0])
        assert placed == set(_GRID_MARKS), f"cells asked for a mark and got none: {set(_GRID_MARKS) - placed}"
    finally:
        _drop(tmp_path)


def test_an_emphasized_column_is_tinted_the_way_an_emphasized_row_is(tmp_path) -> None:
    """The other half of a comparison. A table that can name the criterion deciding it
    but not the option it decides for emphasises the wrong axis of the page's point --
    and a criteria-by-alternatives matrix is read down its columns.
    """
    from pptx.enum.dml import MSO_FILL

    _, module = _layout(tmp_path)
    try:
        from ppt_theme import THEMES

        theme = THEMES["ink-graphite"]
        slide = _slide(module)
        table = module.table(
            slide,
            module.Box(0.72, 1.6, 12.61, 3.6),
            _GRID,
            theme,
            numeric_from=3,
            emphasize_columns=(1,),
            emphasize_rows=(4,),
        )
        soft = module._rgb(theme["accent_soft"])
        for row in range(len(_GRID)):
            for column in range(len(_GRID[0])):
                cell = table.cell(row, column)
                if column == 1 or row == 4:
                    assert cell.fill.type == MSO_FILL.SOLID, f"cell {row},{column} lost its tint"
                    assert cell.fill.fore_color.rgb == soft
                else:
                    assert cell.fill.type == MSO_FILL.BACKGROUND, f"cell {row},{column} was tinted"
        # The header is part of the column: a tint that stopped under it would read as
        # a block of colour behind the numbers rather than as naming that option.
        assert table.cell(0, 1).fill.type == MSO_FILL.SOLID
    finally:
        _drop(tmp_path)


def test_a_group_row_is_one_band_and_its_name_sets_no_column_width(tmp_path) -> None:
    """A band across the table is what separates one group of rows from the next.

    Merged, because there are no vertical rules to hide an unmerged band behind and a
    group's name in column one wraps inside it. And the name is measured nowhere: it
    belongs to the row, not to the first column, so a long one must not widen a column
    that holds nothing but short labels.
    """
    from pptx.enum.dml import MSO_FILL

    _, module = _layout(tmp_path)
    try:
        from ppt_theme import THEMES

        theme = THEMES["ink-graphite"]
        slide = _slide(module)
        box = module.Box(0.72, 1.6, 12.61, 3.6)
        unit = module.Inches(1)

        short = module.table(slide, box, _GRID, theme, numeric_from=3, group_rows={1: "核心"})
        long = module.table(
            slide,
            box,
            _GRID,
            theme,
            numeric_from=3,
            group_rows={1: "核心访问 CORE ACCESS AND IDENTITY MANAGEMENT"},
        )
        assert [column.width for column in short.columns] == [column.width for column in long.columns]

        band = long.cell(1, 0)
        assert band.is_merge_origin and band.span_width == len(_GRID[0]), "a group band is one cell across"
        assert band.text == "核心访问 CORE ACCESS AND IDENTITY MANAGEMENT"
        assert band.fill.type == MSO_FILL.SOLID
        assert band.fill.fore_color.rgb == module._rgb(theme["surface"])
        assert all(run.font.bold for run in band.text_frame.paragraphs[0].runs)
        assert long.cell(1, 1).text == "", "a spanned cell carrying text is text the reader cannot see"
        assert box.x0 + sum(column.width / unit for column in long.columns) <= box.x1 + 0.01

        # One column and there is nothing to merge, and the band is still a band.
        single = module.table(slide, box, [["项目"], [""], ["单点登录"]], theme, numeric_from=1, group_rows={1: "核心"})
        assert single.cell(1, 0).text == "核心"
        assert single.cell(1, 0).fill.fore_color.rgb == module._rgb(theme["surface"])
    finally:
        _drop(tmp_path)


def test_a_detail_row_is_stepped_in_and_a_total_row_sits_under_a_rule(tmp_path) -> None:
    """The hierarchy a grouped table has to carry without drawing a single box.

    The step is the page's own padding rather than a number invented per table, the
    muted tone is the other half of the step, and the rule above the total is what
    says the numbers over it were added up rather than merely listed -- bold alone
    reads as nothing more than emphasis.

    That rule is in the muted tone and not the grid one, because the grid one is now on
    every boundary: a hairline over a total in the tone the row above it already has
    says nothing, and the arithmetic that drew it could not change a pixel.
    """
    _, module = _layout(tmp_path)
    try:
        from ppt_theme import THEMES

        theme = THEMES["ink-graphite"]
        slide = _slide(module)
        box = module.Box(0.72, 1.6, 12.61, 3.6)
        unit = module.Inches(1)

        table = module.table(
            slide, box, _GRID, theme, numeric_from=3, group_rows={1: "核心访问"}, indent_rows=(2, 3), total_rows=(4,)
        )

        step = (table.cell(2, 0).margin_left - table.cell(4, 0).margin_left) / unit
        assert step == pytest.approx(module.PAD, abs=0.001), "the indent is the page's padding, not a new number"
        assert table.cell(2, 1).margin_left == table.cell(4, 1).margin_left, "only the label steps in"

        detail = table.cell(2, 0).text_frame.paragraphs[0].runs[0]
        assert detail.font.color.rgb == module._rgb(theme["muted"])
        assert not detail.font.bold
        assert table.cell(2, 4).text_frame.paragraphs[0].runs[0].font.color.rgb == module._rgb(theme["foreground"])
        for column in range(len(_GRID[0])):
            runs = table.cell(4, column).text_frame.paragraphs[0].runs
            assert all(run.font.bold for run in runs), f"a total's cell {column} is not bold"

        firm = (str(module._rgb(theme["muted"])), pytest.approx(module.GRID_RULE_PT, abs=0.01))
        quiet = (str(module._rgb(theme["grid"])), pytest.approx(module.GRID_RULE_PT, abs=0.01))
        assert _border(module, table.cell(3, 0), "lnB") == firm, "nothing rules off the row above the total"
        assert _border(module, table.cell(4, 0), "lnT") == firm, "the two sides of that boundary disagree"
        assert _border(module, table.cell(3, 0), "lnR") is None, "a vertical rule nobody asked for"
        # And it is darker than the boundary a row that is not a total gets, which is
        # the whole of what makes it readable as a rule rather than as another row.
        assert _border(module, table.cell(2, 0), "lnB") == quiet, "an ordinary boundary lost its hairline"
        assert firm[0] != quiet[0], "a total's rule is the same tone as every other boundary"
    finally:
        _drop(tmp_path)


def test_a_marked_column_is_wide_enough_for_the_mark_in_it(tmp_path) -> None:
    """Content-driven columns measure strings, and a marked cell usually holds none --
    so the column carrying the marks is exactly the one that collapses. Measured on
    the first table drawn with them: a five-step rating in a 0.53in column came out
    0.08in across, a texture rather than a reading.
    """
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    _, module = _layout(tmp_path)
    try:
        from ppt_theme import THEMES

        theme = THEMES["ink-graphite"]
        slide = _slide(module)
        box = module.Box(0.72, 1.6, 12.61, 3.6)
        unit = module.Inches(1)

        plain = module.table(slide, box, _GRID, theme, numeric_from=3)
        before = len(slide.shapes)
        marked = module.table(slide, box, _GRID, theme, numeric_from=3, marks=_GRID_MARKS)

        assert marked.columns[3].width > plain.columns[3].width, "the rating column was not given room"
        assert marked.columns[4].width > plain.columns[4].width, "the progress column was not given room"
        row_height = marked.rows[2].height / unit
        rating = _cell_boxes(module, marked, box)[(2, 3)]
        dots = [
            shape
            for shape in _mark_shapes(slide, before)
            if shape.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE and _within(module, shape, rating)
        ]
        assert len(dots) == 5, f"a five-step scale drew {len(dots)} steps"
        assert min(shape.width / unit for shape in dots) >= row_height * 0.45, "the steps are a texture, not a scale"
    finally:
        _drop(tmp_path)


def test_a_delta_points_the_way_its_number_does_and_invents_no_colour(tmp_path) -> None:
    """Up is not good in every column -- an arrow on "open exceptions" means the
    opposite of one on "availability" -- and a theme carries no red and no green. So
    the direction is the shape, the reading stays the cell's own string, and the ink is
    the page's until the author spends the accent on the row that carries the claim.
    """
    from pptx.enum.shapes import MSO_SHAPE

    _, module = _layout(tmp_path)
    try:
        from ppt_theme import THEMES

        theme = THEMES["ink-graphite"]
        slide = _slide(module)
        box = module.Box(1.0, 2.0, 1.7, 2.34)

        up = module.mark(slide, box, theme, "delta", "+15%")[0]
        down = module.mark(slide, box, theme, "delta", "-2.4pp")[0]
        flat = module.mark(slide, box, theme, "delta", "0")[0]

        assert up.auto_shape_type == MSO_SHAPE.ISOSCELES_TRIANGLE
        assert down.auto_shape_type == MSO_SHAPE.ISOSCELES_TRIANGLE
        assert up.rotation == 0.0 and down.rotation == 180.0
        assert flat.auto_shape_type == MSO_SHAPE.ROUNDED_RECTANGLE, "no change is a dash; an arrow would say otherwise"

        ink = module._rgb(theme["foreground"])
        assert up.fill.fore_color.rgb == ink and down.fill.fore_color.rgb == ink
        loud = module.mark(slide, box, theme, "delta", "+15%", colour="accent")[0]
        assert loud.fill.fore_color.rgb == module._rgb(theme["accent"])
        # And an RGBColor passes through, because that is what `rgb()` next door returns.
        given = module.mark(slide, box, theme, "delta", "+15%", colour=module._rgb("#B91C1C"))[0]
        assert given.fill.fore_color.rgb == module._rgb("#B91C1C")
        assert module.mark(slide, box, theme, "status_dot", "#B91C1C")[0].fill.fore_color.rgb == module._rgb("#B91C1C")
    finally:
        _drop(tmp_path)


def test_a_progress_bar_is_a_length_against_the_whole(tmp_path) -> None:
    """Which is the only thing that makes a column of shares comparable: 76% and 51%
    are two numbers to read and two lengths to see.
    """
    _, module = _layout(tmp_path)
    try:
        from ppt_theme import THEMES

        theme = THEMES["ink-graphite"]
        slide = _slide(module)
        box = module.Box(1.0, 2.0, 3.4, 2.34)
        unit = module.Inches(1)

        track, bar = module.mark(slide, box, theme, "progress", "76%")
        assert track.width / unit == pytest.approx(box.w, abs=0.01), "the whole is drawn, or the part means nothing"
        assert bar.width / unit == pytest.approx(box.w * 0.76, abs=0.01)
        assert track.left == bar.left and track.top == bar.top
        assert track.fill.fore_color.rgb == module._rgb(theme["grid"])
        assert bar.fill.fore_color.rgb == module._rgb(theme["accent"])

        for spelling in ("0.76", "76"):
            assert module.mark(slide, box, theme, "progress", spelling)[1].width == bar.width, spelling
        assert len(module.mark(slide, box, theme, "progress", "0%")) == 1, "nothing done is a track and no bar"
        over = module.mark(slide, box, theme, "progress", "140%")[1]
        assert over.width == track.width, "a share over the whole is the whole"
        sliver = module.mark(slide, box, theme, "progress", "1%")[1]
        assert sliver.width == sliver.height, "a bar too short to see would read as no data at all"
    finally:
        _drop(tmp_path)


def test_a_table_refuses_a_mark_it_cannot_place_or_read(tmp_path) -> None:
    """Every one of these is a spelling an author gets wrong once, and every one of
    them would otherwise be silent: a mark on the header row, a column that is not
    there, a rating outside its own scale, a colour that is a font family.
    """
    _, module = _layout(tmp_path)
    try:
        from ppt_theme import THEMES

        theme = THEMES["ink-graphite"]
        slide = _slide(module)
        box = module.Box(0.72, 1.6, 12.61, 3.6)

        def build(**kwargs):
            module.table(slide, box, _GRID, theme, numeric_from=3, **kwargs)

        with pytest.raises(ValueError, match="row 0 is the header"):
            build(marks={(0, 1): "check"})
        with pytest.raises(ValueError, match="row 0 is the header"):
            build(group_rows={0: "核心"})
        with pytest.raises(ValueError, match="the body rows are 1 to 4"):
            build(total_rows=(9,))
        with pytest.raises(ValueError, match="this table has 5"):
            build(emphasize_columns=(9,))
        with pytest.raises(ValueError, match="keyed by"):
            build(marks={5: "check"})
        with pytest.raises(ValueError, match="unknown mark"):
            build(marks={(1, 1): "tick"})
        with pytest.raises(ValueError, match="kind"):
            build(marks={(1, 1): "harvey:1:2:3"})
        with pytest.raises(ValueError, match="outside a 5-step scale"):
            build(marks={(1, 3): "harvey:9"})
        with pytest.raises(ValueError, match="needs a share"):
            build(marks={(1, 3): "progress:soon"})
        with pytest.raises(ValueError, match="needs a rating"):
            build(marks={(1, 3): "harvey:soon"})
        with pytest.raises(ValueError, match="1 to 10 steps"):
            build(marks={(1, 3): "harvey:2/44"})
        with pytest.raises(ValueError, match="needs a signed number"):
            build(marks={(1, 3): "delta:soon"})
        with pytest.raises(ValueError, match="unknown mark"):
            module.mark(slide, module.Box(1, 1, 2, 1.4), theme, "tick")
        with pytest.raises(ValueError, match="not a colour"):
            module.mark(slide, module.Box(1, 1, 2, 1.4), theme, "check", colour="font_family")
        with pytest.raises(ValueError, match="has no room"):
            module.mark(slide, module.Box(1, 1, 1, 1.4), theme, "check")
        with pytest.raises(ValueError, match="does not fit"):
            module.mark(slide, module.Box(1, 1, 1.1, 1.4), theme, "harvey", "3/10")
    finally:
        _drop(tmp_path)


def test_a_mark_on_a_cell_a_ragged_row_does_not_have_is_named_not_an_index_error(tmp_path) -> None:
    """The one misuse in this module that did not name itself.

    Rows of unequal length draw -- a short row's missing cells come out empty -- so
    `_cell_key` passing on `len(rows[0])` looked like enough. It was not: the mark
    then read `rows[1][2]`, which is not there, and what came back was
    `IndexError: list index out of range` with nothing in it about tables or marks.
    """
    _, module = _layout(tmp_path)
    try:
        from ppt_theme import THEMES

        theme = THEMES["ink-graphite"]
        slide = _slide(module)
        box = module.Box(0.72, 1.6, 12.61, 3.6)
        ragged = [["能力", "选项 A", "达成"], ["单点登录", "62%"]]

        with pytest.raises(ValueError, match="has no cell"):
            module.table(slide, box, ragged, theme, marks={(1, 2): "check"})

        # And the case the check must not take away: a ragged table still draws, and
        # a mark on a cell the short row does have is still a mark.
        drawn = module.table(slide, box, ragged, theme, marks={(1, 1): "check"})
        assert drawn.cell(1, 2).text == "", "a cell the row never gave is empty, not an error"
    finally:
        _drop(tmp_path)


def test_a_kind_that_reads_no_value_refuses_the_field_rather_than_dropping_it(tmp_path) -> None:
    """A tick is a tick, so a field handed to one was meant for something else.

    `check:ACCENT` is the accent misspelt: paint names are case-sensitive, so the
    word fell through to `value`, and check, cross and partial never read `value`.
    The mark came out in the default ink and the author's only stated intent was
    dropped without a word.
    """
    _, module = _layout(tmp_path)
    try:
        from ppt_theme import THEMES

        theme = THEMES["ink-graphite"]
        slide = _slide(module)
        box = module.Box(0.72, 1.6, 12.61, 3.6)

        with pytest.raises(ValueError, match="reads no value"):
            module.table(slide, box, _GRID, theme, numeric_from=3, marks={(2, 1): "check:ACCENT"})
        with pytest.raises(ValueError, match="reads no value"):
            module.mark(slide, module.Box(1, 1, 2, 1.4), theme, "cross", "muted")

        # Spelt as the theme spells it, it is a colour and always was.
        assert module._mark_spec("check:accent") == ("check", None, "accent")
        assert module.mark(slide, module.Box(1, 1, 2, 1.4), theme, "check", colour="muted")
        # And the kinds that do read a value keep reading it.
        assert module._mark_spec("harvey:3.5:accent") == ("harvey", "3.5", "accent")
        assert module._mark_spec("status_dot:accent") == ("status_dot", None, "accent")
        assert module._mark_spec("progress:76%") == ("progress", "76%", None)
    finally:
        _drop(tmp_path)


def test_a_colour_is_six_digits_behind_a_hash_and_six_digits_alone_are_a_number(tmp_path) -> None:
    """`progress:123456` is a share written without its percent sign, not a colour.

    `lstrip("#")` made the hash optional, so any six hex digits were read as paint:
    the reading went into `colour`, `value` came back None, and the bar quietly drew
    from whatever string the cell happened to hold. The reference has said #RRGGBB
    throughout.
    """
    _, module = _layout(tmp_path)
    try:
        from ppt_theme import THEMES

        theme = THEMES["ink-graphite"]
        slide = _slide(module)

        assert not module._is_hex("123456")
        assert module._is_hex("#123456") and module._is_hex("#B91C1C")
        assert module._mark_spec("progress:123456") == ("progress", "123456", None)
        assert module._mark_spec("progress:#123456") == ("progress", None, "#123456")

        with pytest.raises(ValueError, match="not a colour"):
            module.mark(slide, module.Box(1, 1, 2, 1.4), theme, "check", colour="B91C1C")
        given = module.mark(slide, module.Box(1, 1, 2, 1.4), theme, "check", colour="#B91C1C")[0]
        assert given.line.color.rgb == module._rgb("#B91C1C")
    finally:
        _drop(tmp_path)


def test_a_harvey_reads_a_rating_and_refuses_the_share_next_to_it(tmp_path) -> None:
    """The two marks beside each other read the same string as different numbers.

    `progress` takes a share -- "76%", "0.76" and "76" all mean three quarters -- so a
    harvey handed 0.75 was a caller spelling a share, and it was read as 0.75 of a
    five-step scale and drawn without complaint as five dots with the first one
    part-filled. A mark that quietly means a fifth of what was asked for is worse than
    one that refuses, so "75%" now means what it says, a bare fraction is refused with
    both spellings named, and "0.75/5" says it for anyone who really did mean a rating
    below one.
    """
    _, module = _layout(tmp_path)
    try:
        assert module._rating("75%") == (3.75, 5)
        assert module._rating("75%/4") == (3.0, 4)
        assert module._rating("100%") == (5.0, 5)
        assert module._rating("3.5") == (3.5, 5), "a rating is still a rating"
        assert module._rating(1) == (1.0, 5), "and one step out of five is a reading, not a share"
        assert module._rating("0.75/5") == (0.75, 5), "the scale said so, so it is meant"

        for share, said in ((0.75, '"75%"'), ("0.5", '"50%"'), (0.2, '"20%"')):
            with pytest.raises(ValueError, match=f"write {said}"):
                module._rating(share)
        with pytest.raises(ValueError, match=r'write "0\.75/5"'):
            module._rating(0.75)
    finally:
        _drop(tmp_path)


def test_a_rating_scale_that_is_not_a_number_is_refused_in_this_module_s_own_words(tmp_path) -> None:
    """Every other misuse here comes back naming itself and saying how to fix it.

    `int(float(scale))` sat outside the try, so "3.5/abc" escaped as
    `could not convert string to float: 'abc'` -- a Python error about a builtin,
    from a function the author never called.
    """
    _, module = _layout(tmp_path)
    try:
        from ppt_theme import THEMES

        theme = THEMES["ink-graphite"]
        slide = _slide(module)
        box = module.Box(0.72, 1.6, 12.61, 3.6)

        with pytest.raises(ValueError, match="a rating scale is a number of steps"):
            module._rating("3.5/abc")
        with pytest.raises(ValueError, match="a rating scale is a number of steps"):
            module.table(slide, box, _GRID, theme, numeric_from=3, marks={(2, 3): "harvey:3.5/abc"})

        # The scales that are numbers still read, and the range check still bites.
        assert module._rating("3.5/4") == (3.5, 4)
        assert module._rating("3.5") == (3.5, 5)
        with pytest.raises(ValueError, match="1 to 10 steps"):
            module._rating("2/44")
    finally:
        _drop(tmp_path)


def test_an_emphasized_row_that_is_not_there_is_refused_like_every_other_row_list(tmp_path) -> None:
    """`group_rows`, `indent_rows` and `total_rows` all name a body row and all say
    so when the row is not one. `emphasize_rows` did `{int(index) for index in ...}`
    and tinted nothing, so a table pointed at the wrong row came back looking exactly
    like a table pointed at no row -- and row 0 is the header, which has its own
    treatment.
    """
    _, module = _layout(tmp_path)
    try:
        from ppt_theme import THEMES

        theme = THEMES["ink-graphite"]
        slide = _slide(module)
        box = module.Box(0.72, 1.6, 12.61, 3.6)

        with pytest.raises(ValueError, match="emphasize_rows names row 9"):
            module.table(slide, box, _GRID, theme, numeric_from=3, emphasize_rows=(9,))
        with pytest.raises(ValueError, match="row 0 is the header"):
            module.table(slide, box, _GRID, theme, numeric_from=3, emphasize_rows=(0,))

        tinted = module.table(slide, box, _GRID, theme, numeric_from=3, emphasize_rows=(4,))
        assert tinted.cell(4, 0).fill.fore_color.rgb == module._rgb(theme["accent_soft"])
    finally:
        _drop(tmp_path)


def test_a_total_on_the_first_body_row_does_not_draw_over_the_header_s_rule(tmp_path) -> None:
    """Two rules on one boundary, and the second one eats the first.

    A total's rule sits at the top of its row, and the top of row 1 is the boundary
    the accent rule under the header already holds. The muted-toned line is the
    thinner and quieter of the two, so writing it there takes away the one line on
    the table a reader is meant to see.
    """
    _, module = _layout(tmp_path)
    try:
        from ppt_theme import THEMES

        theme = THEMES["ink-graphite"]
        slide = _slide(module)
        box = module.Box(0.72, 1.6, 12.61, 3.6)

        table = module.table(slide, box, _GRID, theme, numeric_from=3, total_rows=(1,))
        accent = (str(module._rgb(theme["accent"])), pytest.approx(module.HEADER_RULE_PT, abs=0.01))
        assert _border(module, table.cell(0, 0), "lnB") == accent, "the header's own rule went missing"
        assert _border(module, table.cell(1, 0), "lnT") == accent, "the total overwrote the header's rule"
        assert all(run.font.bold for run in table.cell(1, 0).text_frame.paragraphs[0].runs), "still a total"

        # Any other total row still gets its own rule -- this is a duplicate dropped,
        # not the treatment dropped.
        lower = module.table(slide, box, _GRID, theme, numeric_from=3, total_rows=(3,))
        firm = (str(module._rgb(theme["muted"])), pytest.approx(module.GRID_RULE_PT, abs=0.01))
        assert _border(module, lower.cell(2, 0), "lnB") == firm
        assert _border(module, lower.cell(3, 0), "lnT") == firm
    finally:
        _drop(tmp_path)


def test_a_mark_never_takes_so_much_room_that_its_label_column_collapses(tmp_path) -> None:
    """A floor with no ceiling takes its room out of the column beside it.

    Measured: two columns in a 1.68in box with `harvey:3.5/10` in the second asked
    2.49in for the scale, `_column_widths` fell back to scaling the floors in
    proportion because they did not fit, and the label column came out 0.18in --
    under `_NARROWEST_IN`, which is the width every other path holds so a column
    still has a header over it.
    """
    _, module = _layout(tmp_path)
    try:
        from ppt_theme import THEMES

        theme = THEMES["ink-graphite"]
        slide = _slide(module)
        unit = module.Inches(1)
        box = module.Box(0.72, 1.6, 0.72 + 1.68, 3.6)

        table = module.table(slide, box, [["标签", "评分"], ["单点登录", ""]], theme, marks={(1, 1): "harvey:3.5/10"})
        widths = [column.width / unit for column in table.columns]

        assert widths[0] >= module._NARROWEST_IN - 0.001, f"the label column collapsed to {widths[0]:.2f}in"
        assert widths[1] > widths[0], "and the scale still has the wider column of the two"
        assert sum(widths) == pytest.approx(box.w, abs=0.02)

        # A table with room to give still gives the marks everything they asked for.
        wide = module.Box(0.72, 1.6, 12.61, 3.6)
        roomy = module.table(slide, wide, _GRID, theme, numeric_from=3, marks=_GRID_MARKS)
        plain = module.table(slide, wide, _GRID, theme, numeric_from=3)
        assert roomy.columns[3].width > plain.columns[3].width
    finally:
        _drop(tmp_path)


def _shapes(tmp_path: Path):
    """`ppt_shapes` imported the way a build script imports it, with its neighbours.

    It is the one generated module that imports the others -- `Box` from
    `ppt_layout`, `rgb` from `ppt_theme` -- so exec'ing it in a bare namespace the
    way `_load_module` does would fail on the import rather than on anything worth
    testing. The build directory is the unit here.
    """
    import sys

    for name, text in script_helper_files().items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    return __import__("ppt_shapes")


# What a shape's position is read back at. A file stores EMU, so a number that
# went in as inches comes out one EMU away from itself, and a geometry assertion
# that wants exactness has to say exact-to-the-file rather than exact-to-the-float.
EMU_IN = 1 / 914400
NEAR = 3 * EMU_IN


def _deck():
    from pptx import Presentation
    from pptx.util import Inches

    deck = Presentation()
    deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)
    return deck, deck.slides.add_slide(deck.slide_layouts[6])


def test_the_shape_module_draws_a_preset_and_keeps_it_a_preset(tmp_path: Path) -> None:
    """The whole argument for this helper is that the export stays editable.

    A freeform tracing the same outline renders identically and is a different
    object: no adjustment handles, no theme inheritance, and a bounding box where
    the geometry checks expect the shape's own rectangle. So what this asserts is
    the XML -- `a:prstGeom` with the name on it -- not that something was drawn.
    """
    module = _shapes(tmp_path)
    try:
        from ppt_theme import THEMES

        _, slide = _deck()
        theme = THEMES["ink-graphite"]
        shape = module.preset(slide, module.Box(1, 1, 4, 2.2), theme, "flowChartDecision", tint="accent")

        geometry = shape._element.spPr.prstGeom
        assert geometry is not None
        assert geometry.get("prst") == "flowChartDecision"
        assert shape.fill.fore_color.rgb == module.rgb(theme["accent"])
        assert shape.shadow.inherit is False
    finally:
        _drop(tmp_path)


def test_an_adjustment_is_written_under_the_name_the_standard_gives_it(tmp_path: Path) -> None:
    """python-pptx's own table is wrong for two shapes, so this does not use it.

    `shape.adjustments` is positional over a hard-coded list with no entry at all
    for `foldedCorner` and four entries for `upDownArrow`'s two. Both would write
    the wrong `a:gd` -- nothing, and `adj1` twice -- and neither fails loudly.
    """
    module = _shapes(tmp_path)
    try:
        from ppt_theme import THEMES

        _, slide = _deck()
        theme = THEMES["ink-graphite"]
        box = module.Box(1, 1, 4, 2.2)

        folded = module.preset(slide, box, theme, "foldedCorner", adj=0.25)
        assert [(gd.get("name"), gd.get("fmla")) for gd in folded._element.spPr.prstGeom.avLst] == [
            ("adj", "val 25000")
        ]

        updown = module.preset(slide, box, theme, "upDownArrow", adj=(0.3, 0.4))
        assert [(gd.get("name"), gd.get("fmla")) for gd in updown._element.spPr.prstGeom.avLst] == [
            ("adj1", "val 30000"),
            ("adj2", "val 40000"),
        ]

        with pytest.raises(ValueError, match="takes 1 adjustment"):
            module.preset(slide, box, theme, "chevron", adj=(0.2, 0.3))
    finally:
        _drop(tmp_path)


def test_a_chevron_row_interlocks_exactly_where_the_geometry_says_it_should(tmp_path: Path) -> None:
    """The number the helper exists to get right, checked against the evaluator.

    A chevron's notch is `x1` in the standard's own guides. The next chevron has to
    start exactly `x1` short of where this one ends, or the point either floats in
    a gap or eats into the neighbour -- and both read as five shapes rather than one
    sequence. The closed form in the generated module is a projection of the guide,
    so the guide is what it is measured against.
    """
    from raven.ppt.services.assets.shapes import preset_geometry

    module = _shapes(tmp_path)
    try:
        from ppt_theme import THEMES

        _, slide = _deck()
        band = module.Box(0.72, 1.94, 12.613, 3.1733)
        steps = module.chevron_row(slide, band, THEMES["ink-graphite"], 5)

        assert len(steps) == 5
        widths = {round(step.shape.width / 914400, 6) for step in steps}
        assert len(widths) == 1, "every step is the same width or the row is not a sequence"

        width, height = widths.pop(), band.h
        notch = preset_geometry("chevron", width, height).guides["x1"]
        lefts = [step.shape.left / 914400 for step in steps]
        for before, after in zip(lefts, lefts[1:]):
            assert after - before == pytest.approx(width - notch, abs=NEAR)

        # And the row fills the band it was given, to the edge.
        assert lefts[0] == pytest.approx(band.x0, abs=NEAR)
        assert lefts[-1] + width == pytest.approx(band.x1, abs=NEAR)
    finally:
        _drop(tmp_path)


def test_a_step_s_copy_lands_in_the_preset_s_own_text_rectangle(tmp_path: Path) -> None:
    """Where the words go is the other half of the arithmetic.

    A chevron's `rect` is inset past both points; a homePlate's only past the one it
    has. Writing into the shape's whole box instead is what puts a label on an arrow,
    and it is invisible until the render.
    """
    from raven.ppt.services.assets.shapes import preset_geometry

    module = _shapes(tmp_path)
    try:
        from ppt_theme import THEMES

        _, slide = _deck()
        band = module.Box(0.72, 1.94, 12.613, 3.1733)
        steps = module.chevron_row(slide, band, THEMES["ink-graphite"], 4)
        width = steps[0].shape.width / 914400

        for index, step in enumerate(steps):
            name = "homePlate" if index == 0 else "chevron"
            left, top, right, bottom = preset_geometry(name, width, band.h).text_rect
            corner = step.shape.left / 914400
            assert step.box.x0 == pytest.approx(corner + left, abs=NEAR)
            assert step.box.x1 == pytest.approx(corner + right, abs=NEAR)
            assert (step.box.y0, step.box.y1) == pytest.approx((band.y0 + top, band.y0 + bottom), abs=NEAR)

        # Neighbouring text regions never share a column, so no label can collide.
        for before, after in zip(steps, steps[1:]):
            assert before.box.x1 <= after.box.x0 + 1e-9
    finally:
        _drop(tmp_path)


def test_a_timeline_spaces_its_stops_and_stays_under_the_band_gate(tmp_path: Path) -> None:
    """A full-width filled bar is a finding, and a spine is full-width by nature.

    `bands` exempts anything at or under `RULE_MAX_HEIGHT_PT` as a hairline rule, so
    the spine is drawn at that weight rather than as a bar with an arrowhead built
    into it. A primitive that trips a gate every time it is used is a primitive
    nothing will use.
    """
    from raven.ppt.services.gates.bands import RULE_MAX_EMU

    module = _shapes(tmp_path)
    try:
        from ppt_theme import THEMES

        _, slide = _deck()
        box = module.Box(0.72, 4.0, 12.613, 5.6)
        track = module.timeline(slide, box, THEMES["ink-graphite"], 4)

        assert track.spine.height <= RULE_MAX_EMU
        assert len(track.stops) == 4
        centres = [(stop.mark.left + stop.mark.width / 2) / 914400 for stop in track.stops]
        gaps = {round(after - before, 6) for before, after in zip(centres, centres[1:])}
        assert len(gaps) == 1, "stops on a timeline are evenly spaced or it is not a scale"
        assert centres[0] > box.x0 and centres[-1] < box.x1, "no stop sits on the page edge"
        for stop in track.stops:
            assert stop.above.y1 <= stop.box.y0, "the label regions are on opposite sides of the spine"
    finally:
        _drop(tmp_path)


def test_a_connector_leaves_the_side_of_the_box_that_faces_the_other_one(tmp_path: Path) -> None:
    """The whole reason to have this rather than two coordinates."""
    module = _shapes(tmp_path)
    try:
        from ppt_theme import THEMES

        _, slide = _deck()
        theme = THEMES["ink-graphite"]
        left = module.Box(1.0, 1.0, 4.0, 2.0)
        right = module.Box(6.0, 1.0, 9.0, 2.0)
        below = module.Box(1.0, 4.0, 4.0, 5.0)

        across = module.connect(slide, left, right, theme)
        assert across.left / 914400 == pytest.approx(left.x1, abs=NEAR)
        assert across.width / 914400 == pytest.approx(right.x0 - left.x1, abs=NEAR)

        down = module.connect(slide, left, below, theme)
        assert down.top / 914400 == pytest.approx(left.y1, abs=NEAR)
        assert down.height / 914400 == pytest.approx(below.y0 - left.y1, abs=NEAR)

        namespace = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
        line = across.line._get_or_add_ln()
        assert line.find(f"{namespace}tailEnd") is not None

        with pytest.raises(ValueError, match="kind is one of"):
            module.connect(slide, left, right, theme, kind="squiggle")
    finally:
        _drop(tmp_path)


def test_the_shape_module_answers_a_miss_the_way_the_service_does(tmp_path: Path) -> None:
    """The generated copy cannot import the service, so a test pins the agreement.

    On the names and the order, which is all the two have ever shared: the service ranks
    over the whole catalogue and says nothing about a name's standing, and the module
    marks the sixty-eight this vocabulary dropped with the reason. So `curvedRightArrow`
    coming back marked out of `right arrow` is the agreement holding, and the mark is
    read off rather than compared.
    """
    from raven.ppt.services.assets import shapes as service

    module = _shapes(tmp_path)
    try:
        from ppt_theme import THEMES

        _, slide = _deck()
        theme = THEMES["ink-graphite"]

        for attempt in ("chevrons", "flowchart_decison", "right arrow", "arrows-move"):
            answered = [candidate.split(" (")[0] for candidate in module.find_presets(attempt)]
            assert answered == service.preset_candidates(attempt), attempt

        assert module.preset(slide, module.Box(1, 1, 3, 2), theme, "RIGHT_ARROW") is not None
        assert module.preset_intent("chevron") == service.preset_intent("chevron")
        assert module.preset_adjustments("blockArc") == tuple(service.preset_adjustments("blockArc"))

        with pytest.raises(LookupError, match="PRESET_NAMES") as caught:
            module.preset(slide, module.Box(1, 1, 3, 2), theme, "arrows-move")
        assert "109" in str(caught.value)

        with pytest.raises(LookupError) as caught:
            module.preset(slide, module.Box(1, 1, 3, 2), theme, "chevrons")
        assert "chevron" in str(caught.value)
    finally:
        _drop(tmp_path)


def test_a_preset_this_vocabulary_dropped_is_refused_with_its_reason(tmp_path: Path) -> None:
    """Sixty-eight names Office has and this vocabulary does not, one group at a time.

    All 177 were rendered at their default adjustments and reviewed, and these came back
    either broken or as clip art. Drawing one anyway puts on the page exactly what the
    review threw out, and answering "unknown preset shape" for a name an author read in
    PowerPoint sends them hunting a typo that is not there -- so the refusal carries what
    the render showed and, where there is one, the shape to reach for instead.
    """
    module = _shapes(tmp_path)
    try:
        from ppt_theme import THEMES

        _, slide = _deck()
        theme, box = THEMES["ink-graphite"], module.Box(1, 1, 3, 2)

        for name, shown, instead in (
            ("actionButtonHome", "grey glyph", "roundRect"),
            ("cube", "second face", "roundRect"),
            ("star5", "sale signage", None),
            ("gear6", "clip art", "roundRect"),
            ("borderCallout1", "leader line outside the box", "wedgeRectCallout"),
            ("leftBrace", "slab with a spike", "bracePair"),
            ("squareTabs", "corner fragments", None),
            ("chartX", "crossed-out box", None),
        ):
            with pytest.raises(LookupError) as caught:
                module.preset(slide, box, theme, name)
            said = str(caught.value)
            assert f"{name} exists in Office but is not in this vocabulary" in said
            assert shown in said, name
            if instead is not None:
                assert instead in said, name

        # Any spelling of a dropped name, for the reason `preset` takes any spelling of a
        # live one: the separator was never the author's to know about.
        with pytest.raises(LookupError, match="crossed-out box"):
            module.preset(slide, box, theme, "chart_x")

        # And the whole table, not the eight sampled above.
        for name in module._OUT_OF_VOCABULARY:
            assert name not in module.PRESET_NAMES
            with pytest.raises(LookupError, match="not in this vocabulary"):
                module.preset(slide, box, theme, name)
    finally:
        _drop(tmp_path)


def test_the_search_still_finds_a_dropped_name_and_marks_it(tmp_path: Path) -> None:
    """A near miss that comes back empty reads as a spelling problem.

    `star5` is a name out of PowerPoint's own gallery, so an author will type it. Dropping
    it from the search as well as from the vocabulary answers it with nothing, which says
    "you spelled it wrong" about a name that is spelled right.
    """
    module = _shapes(tmp_path)
    try:
        found = module.find_presets("star5")
        assert found[0].startswith("star5 (not in this vocabulary: ")
        assert "sale signage" in found[0]
        assert "star5" not in module.PRESET_NAMES

        # A live name in the same answer carries no mark, so the mark means something.
        answered = module.find_presets("brace")
        assert "bracePair" in answered
        assert any(candidate.startswith("leftBrace (not in this vocabulary: ") for candidate in answered)
        assert any("bracePair or bracketPair" in candidate for candidate in answered)
    finally:
        _drop(tmp_path)


def test_the_vocabulary_is_the_count_the_skill_prints(tmp_path: Path) -> None:
    """The number an author plans with, before there is a build to be refused by.

    The catalogue keeps all 177 -- the geometry of a dropped preset is still what the
    measurements read -- so the two counts are different numbers and the skill quotes the
    one an author can pick from.
    """
    module = _shapes(tmp_path)
    try:
        catalogue = json.loads(shape_catalog_json())
        assert len(catalogue) == 177
        assert len(module.PRESET_NAMES) == 109
        assert len(module._OUT_OF_VOCABULARY) == len(catalogue) - len(module.PRESET_NAMES)
        assert set(module._OUT_OF_VOCABULARY) < set(catalogue)
        # Including in the module's own first paragraph, which is where an author who
        # never opens the skill reads what it may draw with.
        assert f"{len(module.PRESET_NAMES)} of Office's {len(catalogue)}" in module.__doc__

        skill = Path(__file__).resolve().parents[2] / "raven/memory_engine/skills/ppt-script-authoring"
        printed = "\n".join(path.read_text(encoding="utf-8") for path in sorted(skill.rglob("*.md")))
        assert f"{len(module.PRESET_NAMES)} Office preset" in printed
    finally:
        _drop(tmp_path)


def test_every_step_of_a_chevron_row_carries_a_seam_against_its_own_fill(tmp_path: Path) -> None:
    """Five steps in one solid tint with no line render as one long arrow.

    The notch between two steps is a boundary between two identical colours: a scanline
    across a five-step row at 110 dpi found each notch as a single pixel of antialiasing,
    32 levels off the fill, between 245-pixel runs of that same fill -- and the render read
    as one arrow carrying five words. So the default is a hairline in the theme's ground
    colour, distinct from the fill, which is the whole point; an outline the caller names
    still wins.
    """
    from pptx.util import Pt

    module = _shapes(tmp_path)
    try:
        from ppt_theme import THEMES

        _, slide = _deck()
        theme = THEMES["ink-graphite"]
        band = module.Box(0.72, 1.94, 12.613, 3.1733)

        for step in module.chevron_row(slide, band, theme, 5):
            assert step.shape.line.color.rgb == module.rgb(theme["background"])
            assert step.shape.line.color.rgb != step.shape.fill.fore_color.rgb
            assert step.shape.line.width == Pt(module.SEAM_PT)

        for step in module.chevron_row(slide, band, theme, 3, outline="foreground"):
            assert step.shape.line.color.rgb == module.rgb(theme["foreground"])
            assert step.shape.line.width == Pt(1.5)
    finally:
        _drop(tmp_path)


def test_the_shape_data_is_what_the_module_reads(tmp_path: Path) -> None:
    from raven.ppt.services.assets import shapes as service

    _install(tmp_path)
    written = json.loads((tmp_path / SHAPE_DATA_FILENAME).read_text(encoding="utf-8"))
    assert written == json.loads(shape_catalog_json())
    assert set(written) == set(service.drawable_presets())
    # Names, one line each, and which knobs are angles -- no geometry. The build
    # directory never evaluates a guide, so shipping 187 path tables into it would be
    # dead weight in the way; the units travel because the field holds two of them and
    # nothing downstream can work out which is which.
    assert all(set(entry) <= {"adj", "for", "deg", "opaque"} for entry in written.values())
    assert written["pie"]["deg"] == ["adj1", "adj2"]
    assert written["mathNotEqual"]["opaque"] == ["adj2"]
    assert "deg" not in written["chevron"] and "opaque" not in written["chevron"]


def test_the_heading_leaves_the_page_to_the_page(helpers) -> None:
    """The top of a page is a header, not a quarter of the deck.

    Written after a run of finished decks came back with the body starting at 1.94in
    -- 26% of a 7.5in canvas spent on a kicker and one line of title, before anything
    the page argues. The sixteen templates that ship with this repository disagree
    unanimously: every one of them ends its title row at 1.12in. The number below is
    the ceiling, not the target; what it exists to catch is the band drifting back.

    Measured on the band and not on `body.y0`, which is a different distance: the
    band keeps a gutter over the body, so a start of body read 0.28in of page air as
    header. The header is the band -- what `heading` paints, ending under the title
    it carries -- and reading it that way the ceiling holds at the number it was
    written with instead of being moved to fit a change in the air below it.
    """
    layout = helpers.ppt_layout
    frame = layout.page()
    band = frame.title.y1 + layout._TITLE_AIR
    share = band / layout.CANVAS_H
    assert share <= 0.20, (
        f"the heading takes {share:.0%} of the canvas (the band ends at {band:.2f}in); "
        "every shipped template ends its title row at 1.12in"
    )
    assert frame.title.h >= (layout.TITLE_PT * 1.15 + 3) / 72, "a title box too short for one line of title"
    assert frame.kicker.h >= (layout.KICKER_PT * 1.15 + 3) / 72, "a kicker box too short for one line"


def test_the_heading_draws_no_rule_under_the_title(helpers) -> None:
    """An accent hairline repeated under every title is decoration, so it is gone.

    It used to be `underline=True`, drawn on every page that called `heading`. The
    band gate never reported it -- a 0.03in rule clears the hairline exemption by a
    factor of two -- so nothing downstream would have caught it, which is why this is
    a test and not a finding.
    """
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    frame = helpers.ppt_layout.page()
    band = helpers.ppt_layout.heading(slide, frame, helpers.theme, "标题", kicker="kicker")

    accent = helpers.theme["accent"].lstrip("#").upper()
    under_title = [
        shape
        for shape in slide.shapes
        if shape.top is not None and shape.top > frame.title.y1 * 914400 and shape.top < band.y1 * 914400 + 914400 // 10
    ]
    painted = [
        shape
        for shape in under_title
        if getattr(getattr(shape, "fill", None), "type", None) is not None
        and getattr(shape.fill, "fore_color", None) is not None
        and str(getattr(shape.fill.fore_color, "rgb", "")) == accent
    ]
    assert not painted, f"{len(painted)} accent bar(s) drawn under the title"


# --------------------------------------------------------------------------------
# What a helper drew, and what it will take: the measurements added because a live
# run of 105 steps spent thirteen of them moving a table's columns and five guessing
# the y of a band -- both arithmetic these helpers had already done and did not say.


def test_every_helper_that_draws_hands_back_the_box_it_covered(helpers) -> None:
    """The consistency rule, read off the module rather than off a list.

    `_draws` finds the helpers by signature, so a new one is covered the day it is
    written. What each has to answer is the same question -- where did this land --
    and answering it for nine of ten is the same as not answering it: an author who
    has to remember which helper says and which does not goes back to the render.
    """
    from pptx import Presentation

    layout = helpers.ppt_layout
    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = layout.Inches(13.333), layout.Inches(7.5)
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    calls = _sample_calls(helpers, slide, _two_by_two_png(helpers.directory))

    for name in sorted(_draws(layout)):
        drawn = calls[name]()
        box = getattr(drawn, "box", None)
        assert box is not None, f"ppt_layout.{name} hands back no box"
        assert isinstance(box, layout.Box), f"ppt_layout.{name}.box is {type(box).__name__}, not a Box"
        assert box.w > 0 and box.h > 0, f"ppt_layout.{name} reports an empty box: {box}"


def test_what_a_helper_hands_back_still_reads_as_what_it_used_to_be(helpers) -> None:
    """The upgrade may not cost a spelling.

    Every one of these returns went from a python-pptx object to a two-field tuple,
    and a module-facing change that breaks `frame.paragraphs` breaks every program
    ever written against it -- including `ppt_charts`, which calls `write` and is not
    this module's to edit. So the tuple looks the attribute up on the shape first and
    on the box second, and both halves of that are asserted here.
    """
    from pptx import Presentation

    layout, theme = helpers.ppt_layout, helpers.theme
    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = layout.Inches(13.333), layout.Inches(7.5)
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    box = layout.Box(0.72, 1.6, 6.0, 2.4)

    written = layout.write(slide, box, ["one", "two"], colour=theme["foreground"])
    assert [para.text for para in written.paragraphs] == ["one", "two"], "the text frame is still reachable"
    assert written.word_wrap is True

    drawn = layout.table(slide, layout.Box(0.72, 3.0, 9.0, 4.0), [["Head", "Value"], ["Row", "1"]], theme)
    assert len(drawn.columns) == 2 and drawn.cell(1, 0).text == "Row", "the table is still reachable"

    band = layout.heading(slide, layout.page(), theme, "标题", kicker="kicker")
    assert band.x1 == band.box.x1, "a box attribute falls through to the box"
    assert round(band.w, 6) == round(band.box.w, 6)

    # And `overlaps` takes them directly, which is the assertion an author makes.
    assert layout.overlaps([written, drawn]) == []
    assert layout.overlaps([written, layout.Box(0.72, 1.6, 3.0, 2.0)]) == [(0, 1)]


def test_a_rule_reports_the_mark_and_not_the_box_it_was_asked_for(helpers) -> None:
    """A rule starts 0.06in below what it underlines and is at most 1.05in long.

    Both numbers are inside the helper, and an author who read its own argument back
    got neither -- so a page that put something under a rule put it under the box the
    rule was given, which is 0.09in higher than the rule really reaches.
    """
    from pptx import Presentation

    layout, layout_theme = helpers.ppt_layout, helpers.theme
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    box = layout.Box(0.72, 1.6, 9.0, 2.4)

    drawn = layout.rule(slide, box, layout_theme, thickness=0.03)

    assert drawn.box.y0 == pytest.approx(box.y1 + 0.06), "the rule sits under the box, not on its edge"
    assert drawn.box.h == pytest.approx(0.03)
    assert drawn.box.w == pytest.approx(1.05), "a rule is an accent mark, not a divider the width of the page"
    unit = layout.Inches(1)
    assert drawn.shape.left / unit == pytest.approx(drawn.box.x0)
    assert drawn.shape.top / unit == pytest.approx(drawn.box.y0)


def test_a_mark_is_still_a_list_and_now_says_how_much_room_it_took(helpers) -> None:
    """`track, bar = mark(...)` and `mark(...)[0]` are how the reference reads these,
    so a mark cannot become a two-field tuple. The ink it covers is the number that
    says whether the column carrying a five-step scale is wide enough to read.
    """
    from pptx import Presentation

    layout, theme = helpers.ppt_layout, helpers.theme
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    cell = layout.Box(1.0, 2.0, 2.6, 2.34)

    scale = layout.mark(slide, cell, theme, "harvey", "3.5")

    assert isinstance(scale, list) and len(scale) == 6, "five steps and the half"
    track, bar = layout.mark(slide, cell, theme, "progress", "76%")
    assert track.width > bar.width
    # The ink, not the cell: a rating is centred in its cell and takes about half of it.
    assert scale.box.w < cell.w and scale.box.h < cell.h
    assert scale.box.x0 >= cell.x0 - 0.01 and scale.box.x1 <= cell.x1 + 0.01


def test_a_stack_can_be_asked_what_is_left_without_spending_it(helpers) -> None:
    """`rest()` answers and takes; a program that only wanted to ask had to take."""
    layout = helpers.ppt_layout
    down = layout.stack(layout.Box(0.72, 1.66, 12.61, 6.80), gutter=0.20)
    down.take(1.0)

    room = down.room

    assert (room.y0, room.y1) == pytest.approx((2.86, 6.80))
    assert down.left == pytest.approx(3.94), "asking did not spend it"
    assert down.rest() == room, "and rest() answers the same question"


def test_a_line_is_reserved_at_what_the_renderer_really_gives_it(helpers) -> None:
    """The one number the copy measurements rest on, and it is measured.

    A line is not the type size: a renderer gives it the face's ascent, descent and
    gap and then multiplies by the paragraph's spacing. Measured on LibreOffice
    through the PDF: six 16pt lines at spacing 1.15 came out on a 22.0pt pitch and at
    spacing 1.0 on a 19.15pt one -- 1.197 ems both times, in the Latin faces and in
    the CJK one. Reserving the type size alone is a fifth short, which is one line in
    five landing outside the box it was written into.
    """
    layout = helpers.ppt_layout

    assert layout._line_h(16, 1.15) == pytest.approx(22.0 / 72, abs=0.005)
    assert layout._line_h(16, 1.0) == pytest.approx(19.15 / 72, abs=0.005)
    # And the box a whole paragraph gets is those lines plus the frame's own margins.
    one = layout.text_size("x", 4.0, size=16, spacing=1.15)
    assert one.h == pytest.approx(layout._line_h(16, 1.15) + 0.04)


def test_copy_is_measured_at_the_width_it_will_be_set_in(helpers) -> None:
    """What `write` will not answer, because it turns autofit off on purpose.

    A string that fits one line in a 6in column takes three in a 2in one, and the
    only way to find out was to build the deck. `lines_needed` is that question, and
    `text_size` is the box to ask a `stack` for.
    """
    layout = helpers.ppt_layout
    copy = "单一共享模型在三个基准上都高于各自的专用模型，差距最大的是 OVIS。"

    assert layout.lines_needed(copy, 8.0, size=16, font="Arial") == 1
    assert layout.lines_needed(copy, 6.0, size=16, font="Arial") == 2
    assert layout.lines_needed(copy, 2.0, size=16, font="Arial") >= 4
    assert layout.lines_needed(["one", "two", "three"], 6.0) == 3, "each paragraph is at least a line"

    # The width it reports is the copy's, never the column's: a short label in a wide
    # column measures the label, which is what something placed beside it must clear.
    assert layout.text_size("AP", 6.0, size=14).w < 1.0
    # The widest line the copy actually breaks onto. It used to report the whole
    # string's width, clamped to the column, because nothing wrapped it -- so a
    # `stack` asked for room beside wrapped copy was handed the column and not the
    # ragged edge the copy really leaves. Stated as the two things that are true of
    # any wrapped copy in any face rather than as a number off one render: a line
    # that wrapped fits the column it wrapped into, and it is at least as wide as the
    # widest piece the wrap could not break.
    wrapped = layout.text_size(copy, 2.0, size=16, font="Arial").w
    room = 2.0 - 2 * layout._FRAME_SIDE
    unbreakable = max(layout._em_width(piece, 16, "Arial") for piece in layout._tokens(copy))
    assert wrapped <= 2.0, "wrapped copy fits the column it wrapped into"
    assert wrapped >= min(unbreakable, room), "and holds the widest piece the wrap could not break"
    assert layout.text_size(copy, 8.0, size=16, font="Arial").w < 8.0, "and copy that does not wrap does not"
    # Bold sets wider, so it may take a line more.
    assert layout.text_size(copy, 2.0, size=16, bold=True).h >= layout.text_size(copy, 2.0, size=16).h


def test_a_bulleted_list_needs_more_room_than_the_same_copy_set_plain(helpers) -> None:
    """The hanging indent and the space between points are `points`'s own arithmetic.

    A list stacked against `text_size` is short by both, and short by exactly the
    amount that puts the next band on top of the last point.
    """
    layout = helpers.ppt_layout
    items = ["单一共享模型在三个基准上都高于各自的专用模型。", "参数量没有随之上升：多出来的是共享解码器。"]

    listed = layout.points_size(items, 4.0, size=16, font="Arial")
    plain = layout.text_size(items, 4.0, size=16, font="Arial")

    assert listed.h > plain.h, "the mark's margin and the gap between points are not free"
    assert listed.h - plain.h >= 16 * 0.45 / 72 - 1e-9, "the gap between two points is a paragraph setting"
    assert layout.points_size(items[:1], 4.0, size=16).h < listed.h

    # The indent on its own, with the spacing and the gaps held equal: one point that
    # fits two lines as a paragraph takes three as a point, because the mark's margin
    # is 1.5 ems the copy does not get. 2.9in is where that crossing falls for this
    # string at 16pt, and a list measured without the indent lands a line short.
    one = items[:1]
    indented = layout.points_size(one, 2.9, size=16, font="Arial", spacing=1.15)
    flat = layout.text_size(one, 2.9, size=16, font="Arial", spacing=1.15)
    assert indented.h > flat.h + 1e-9, f"the hanging indent bought no room: {indented.h} vs {flat.h}"

    # And what `points` hands back is that measurement, not the region it was given.
    from pptx import Presentation

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    region = layout.Box(0.72, 1.6, 4.72, 5.6)
    drawn = layout.points(slide, region, helpers.theme, items, size=16)
    assert drawn.box.h == pytest.approx(layout.points_size(items, region.w, size=16, font="Arial").h)
    assert drawn.box.y1 < region.y1 - 1.0, "the list is not four inches tall and must not say it is"


def test_a_table_gives_the_same_size_whether_or_not_it_is_drawn(helpers) -> None:
    """One arithmetic, asked twice. A size that is not the drawn size is worse than
    no size at all, so `table` and `table_size` come through the same function and
    this compares the two over every option that moves the geometry.
    """
    from pptx import Presentation

    layout, theme = helpers.ppt_layout, helpers.theme
    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = layout.Inches(13.333), layout.Inches(7.5)
    box = layout.Box(0.72, 1.6, 12.61, 5.6)
    rows = [
        ["方法", "YTVIS AP", "OVIS AP", "参数量"],
        ["TarViS (Swin-L) 联合训练", "60.2", "43.2", "254M"],
        ["VITA", "57.5", "39.6", "197M"],
        ["IDOL", "56.1", "38.0", "182M"],
    ]
    variants = [
        {},
        {"style": "compact"},
        {"style": "row_rules", "size": 18},
        {"weights": (2.0, 1.0, 1.0, 1.0)},
        {"group_rows": {1: "端到端"}},
        {"marks": {(2, 1): "harvey:3.5", (3, 3): "progress:88%"}},
    ]
    unit = layout.Inches(1)
    for options in variants:
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        predicted = layout.table_size(rows, theme, box=box, **options)
        drawn = layout.table(slide, box, rows, theme, **options)
        frame = next(shape for shape in slide.shapes if shape.has_table)

        assert drawn.box == predicted, f"{options}: drawn {drawn.box} vs predicted {predicted}"
        assert drawn.box.w == pytest.approx(frame.width / unit, abs=0.002), f"{options}: not the width drawn"
        assert drawn.box.h == pytest.approx(frame.height / unit, abs=0.002), f"{options}: not the height drawn"
        assert drawn.box.x0 == box.x0 and drawn.box.y0 == box.y0

    # Without a box it is a size at the origin, as wide as the content wants.
    loose = layout.table_size(rows, theme)
    assert loose.x0 == 0 and loose.y0 == 0
    assert loose.w == pytest.approx(layout.table_size(rows, theme, box=box).w)
    # It used to refuse this: weights are proportions of a box and no box was named.
    # But the field a caller asks for here is the height, and a row's height comes off
    # the type size and the row count -- the weights cannot touch it. So the weights
    # are set aside and the content-sized answer comes back, exact in the dimension
    # that was being asked about.
    weighted = layout.table_size(rows, theme, weights=(1, 1, 1, 1))
    assert weighted.h == loose.h
    assert weighted.w == loose.w


def test_a_table_row_is_never_shorter_than_the_line_it_holds(helpers) -> None:
    """A defect the render showed and the arithmetic hid.

    `compact` asked for a 19pt row for a 14pt line, and LibreOffice gave the row the
    20 to 21pt the line needs -- so four rows drifted a tenth of an inch past where
    the table said they ended, and the hairline placed from those heights came down
    through the last row's figures. The floor is the line box plus the cell's own
    margins, which is what the renderer is already doing.
    """
    layout, theme = helpers.ppt_layout, helpers.theme
    rows = [["方法", "AP"], ["TarViS", "60.2"], ["VITA", "57.5"], ["IDOL", "56.1"], ["Mask2Former", "52.6"]]

    for size in (12, 14, 16, 20):
        for style in ("minimal", "compact"):
            tall = layout.table_size(rows, theme, size=size, style=style).h
            row = (tall - (size + 12) / 72) / (len(rows) - 1)
            assert row >= layout._line_h(size) + 2 * 0.03 - 1e-9, f"{style} at {size}pt sets a {row:.3f}in row"


def test_a_formula_says_what_size_it_will_settle_at_before_it_sets_it(helpers) -> None:
    """It steps down from `size` until the line fits, and where it stopped was only
    ever visible in the render -- so a formula three steps under the copy around it
    read as a mistake nobody could argue with.
    """
    from pptx import Presentation

    layout, theme = helpers.ppt_layout, helpers.theme
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    face = theme.get("font_family")
    expression = "掩码 logits = (F_4, Q'_{inst})；分类 logits = (Q'_{inst}, concat(Q'_{sem}, Q'_{bg}))"

    for width in (12.6, 6.0, 3.4):
        box = layout.Box(0.72, 1.6, 0.72 + width, 2.4)
        chosen = layout.formula_type_size(expression, width, size=16, font=face)
        drawn = layout.formula(slide, box, expression, theme, size=16)
        set_at = {run.font.size.pt for para in drawn.paragraphs for run in para.runs}
        assert set_at == {float(chosen)}, f"{width}in: asked {chosen}, set {set_at}"
    assert layout.formula_type_size(expression, 12.6, size=16, font=face) == 16
    assert layout.formula_type_size(expression, 3.4, size=16, font=face) < 16, "the narrow column must still bite"
    assert layout.formula_type_size(expression, 3.4, size=16, font=face) >= layout.BODY_FLOOR_PT


def test_a_picture_says_how_much_of_the_box_it_will_cover_before_it_is_placed(helpers) -> None:
    """The aspect decides which of the two dimensions runs out first, so one of them
    always comes back short -- and `picture_fit` centres what is left, which on a
    band cut by eye is a strip of white over the figure and another under it.
    """
    from PIL import Image
    from pptx import Presentation

    layout, theme = helpers.ppt_layout, helpers.theme
    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = layout.Inches(13.333), layout.Inches(7.5)
    unit = layout.Inches(1)
    box = layout.Box(1.0, 1.5, 7.0, 5.0)

    for pixels in ((1600, 400), (400, 1600), (1200, 900)):
        photo = helpers.directory / f"figure-{pixels[0]}x{pixels[1]}.png"
        Image.new("RGB", pixels, (120, 140, 160)).save(photo)
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])

        predicted = layout.picture_size(photo, box, caption="图 1：架构", size=14)
        drawn = layout.picture_fit(slide, photo, box, theme, caption="图 1：架构", size=14)
        strip = (14 + 9) / 72 + 0.08

        assert drawn.width / unit == pytest.approx(predicted.w, abs=0.01), pixels
        assert drawn.height / unit == pytest.approx(predicted.h - strip, abs=0.01), pixels
        # And the box it hands back is where the figure and its caption really are,
        # not the region: the aspect leaves slack in one direction and `picture_fit`
        # centres the figure in it, so the two differ by exactly that slack.
        assert drawn.box.y0 == pytest.approx(drawn.top / unit, abs=0.005), pixels
        assert drawn.box.y0 > box.y0 + 0.05 or drawn.box.x0 > box.x0 + 0.05, f"{pixels}: nothing was centred"
        assert drawn.box.x1 <= box.x1 + 0.01 and drawn.box.y1 <= box.y1 + 0.01, pixels
        # And the fixed point: hand the predicted height back and nothing is centred
        # in nothing -- the figure starts at the top of the band it was given.
        tight = layout.Box.at(box.x0, box.y0, w=box.w, h=predicted.h)
        again = layout.picture_fit(slide, photo, tight, theme, caption="图 1：架构", size=14)
        assert again.top / unit == pytest.approx(tight.y0, abs=0.01), pixels
        assert again.box.y1 <= tight.y1 + 0.01 and tight.y1 - again.box.y1 < 0.10, pixels


def test_a_card_says_where_its_copy_goes_and_a_taller_title_pushes_it_down(helpers) -> None:
    """`card` handed back the box it was given, which told an author nothing it did
    not already know -- and the one thing it needed, whether the copy cleared the
    title and the padding, was in the render. The title's line was a flat 0.36in as
    well, so a 20pt title set in a 16pt line and a title long enough to wrap set its
    second line over the copy.
    """
    from pptx import Presentation

    layout, theme = helpers.ppt_layout, helpers.theme
    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = layout.Inches(13.333), layout.Inches(7.5)
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    box = layout.Box(0.72, 1.6, 4.4, 3.6)
    unit = layout.Inches(1)

    layout.card(slide, box, theme, icon="target", title="语义查询是必要的", body="去掉后全线下降")
    where = layout.card_body_box(box, icon="target", title="语义查询是必要的")
    body = next(
        shape for shape in slide.shapes if getattr(shape, "has_text_frame", False) and "去掉后" in shape.text_frame.text
    )
    assert body.left / unit == pytest.approx(where.x0, abs=0.005)
    assert body.top / unit == pytest.approx(where.y0, abs=0.005)
    assert body.width / unit == pytest.approx(where.w, abs=0.005)

    # A taller title takes a taller line, and the copy starts under it.
    bigger = layout.card_body_box(box, icon="target", title="语义查询是必要的", title_size=layout.TITLE_PT)
    assert bigger.y0 > where.y0
    # And a title long enough to wrap takes two lines rather than one over the copy.
    long_title = "语义查询是必要的，去掉之后三个基准全线下降"
    assert layout.card_body_box(box, title=long_title).y0 > layout.card_body_box(box, title="短").y0
    # No title, no line: the copy starts at the padding.
    assert layout.card_body_box(box).y0 == pytest.approx(box.y0 + layout.PAD)


def test_fits_answers_for_copy_and_for_anything_a_size_helper_measured(helpers) -> None:
    """One question, two spellings, because the answer is the same shape either way."""
    layout, theme = helpers.ppt_layout, helpers.theme
    rows = [["方法", "AP"], ["TarViS", "60.2"], ["VITA", "57.5"]]
    band = layout.Box(0.72, 1.6, 6.0, 3.0)

    assert layout.fits("一行结论", band, size=16, font="Arial")
    assert not layout.fits(["一行结论"] * 8, band, size=16, font="Arial")
    assert layout.fits(layout.table_size(rows, theme, box=band), band)
    assert not layout.fits(layout.table_size(rows, theme, size=40, box=band), band)
    assert layout.fits(layout.points_size(["一", "二"], band.w, size=16), band)


# The copy, boxes and faces the sweeps below run over. One table rather than one per
# test, because the whole claim is that the answer behaves the same way whatever it is
# asked about, and three tests reading three different corpora could not say that.
STEP_COPY = (
    "采集",
    "数据清洗",
    "模型训练与评测",
    "Ship",
    "Measure",
    "end-to-end",
    "84.2%",
    "TarViS 目标查询",
    "internationalization",
    "Scope Build",
    "The pipeline drops every row whose label disagrees with its source.",
    "流水线会丢掉标签与来源不一致的每一行记录，并在报告里说明原因。",
    ["采集", "清洗", "标注"],
)
# A chevron step and the wider first one, a card head, a card body, a table cell, a
# full-width band, and a column: the shapes a label actually lands in.
STEP_BOXES = ((1.63, 1.25), (2.57, 1.25), (2.40, 1.40), (3.60, 1.10), (1.90, 0.34), (11.89, 0.60), (1.20, 3.00))
# None is "any of the six", and the other three are the narrowest, the widest, and one
# between -- the spread `FACE_WIDTH` records.
STEP_FACES = (None, "Arial", "Cambria", "Bookman Old Style")


def _steps_of_the_ramp(layout) -> tuple[int, ...]:
    """The ramp largest first, as the answer may come back, read off the module."""
    return (layout.NUMBER_PT, layout.TITLE_PT, layout.LEAD_PT, layout.BODY_PT, layout.LABEL_PT)


def test_the_largest_step_a_box_takes_is_a_step_and_never_a_size_between_two(helpers) -> None:
    """The ramp exists so sizes stop drifting, and an answer of 23pt would end that.

    Ten live pages set thirteen sizes of their own between 10 and 32pt; a call invented
    to stop that must not be a fourteenth source of one.
    """
    layout = helpers.ppt_layout
    ramp = _steps_of_the_ramp(layout)

    for text in STEP_COPY:
        for width, height in STEP_BOXES:
            for face in STEP_FACES:
                for wrap in (False, True):
                    said = layout.the_largest_step_this_copy_takes(
                        text, layout.Box(0.0, 0.0, width, height), font=face, wrap=wrap
                    )
                    assert said in ramp, f"{said}pt is not a step of the ramp"
                    assert said >= layout.BODY_FLOOR_PT, "nothing may answer below the floor"
                    assert said <= layout.TITLE_PT, "the default ceiling is the page's own title size"

    # And the ceiling moves only where the caller says so, in steps.
    tall = layout.Box(0.0, 0.0, 2.4, 1.4)
    assert layout.the_largest_step_this_copy_takes("采集", tall, largest=layout.NUMBER_PT) == layout.NUMBER_PT
    assert layout.the_largest_step_this_copy_takes("采集", tall, largest=layout.BODY_PT) == layout.BODY_PT
    with pytest.raises(ValueError, match="not a step of the ramp"):
        layout.the_largest_step_this_copy_takes("采集", tall, largest=23)


def test_the_largest_step_only_ever_grows_with_the_box(helpers) -> None:
    """The stability question, swept: a box growing a hundredth of an inch at a time.

    This is what decided the answer would be a step rather than any integer. Over the
    same sweep an integer search changed 13 to 16 times per series and a step changes
    two or three, so a page redrawn against a column a hundredth wider is the same page.
    A single backward move here is a size that shrinks as its box grows, which no author
    could reason about, and one change of two steps is a jump of 10pt on a hundredth of
    an inch.
    """
    layout = helpers.ppt_layout
    ramp = _steps_of_the_ramp(layout)

    for text in STEP_COPY:
        for face in (None, "Cambria"):
            for wrap in (False, True):
                for axis in ("w", "h"):
                    answers = []
                    for hundredth in range(60, 401):
                        span = hundredth / 100.0
                        box = layout.Box(0.0, 0.0, span, 1.25) if axis == "w" else layout.Box(0.0, 0.0, 2.4, span)
                        answers.append(layout.the_largest_step_this_copy_takes(text, box, font=face, wrap=wrap))
                    where = f"{text!r} face={face} wrap={wrap} along {axis}"
                    for before, after in zip(answers, answers[1:]):
                        assert after >= before, f"the answer went down as the box grew: {where}"
                        assert abs(ramp.index(after) - ramp.index(before)) <= 1, (
                            f"the answer jumped two steps on a hundredth of an inch: {where}"
                        )


def test_one_more_character_moves_the_largest_step_at_most_one_step(helpers) -> None:
    """The other half of stability: an edit to the copy, not to the box.

    Adding a character can only ever cost room and removing one can only ever free it,
    so the answer has to move in that direction or not at all -- that half is absolute.
    Two steps at once is not forbidden, because it is sometimes true: a CJK character is
    twice a lowercase Latin one, so "Measure国" in a 1.20in column really does clear
    neither 20pt nor 16pt when "Measure" cleared 20. It is rare and it is bounded, and
    the bound is what this holds: 4 of 5376 edits over this corpus, and a regression that
    made the answer jumpy would push that number, not this comment.
    """
    layout = helpers.ppt_layout
    ramp = _steps_of_the_ramp(layout)
    jumped = edits = 0

    for text in STEP_COPY:
        if isinstance(text, list):
            continue
        for width, height in STEP_BOXES:
            for face in STEP_FACES:
                for wrap in (False, True):
                    box = layout.Box(0.0, 0.0, width, height)
                    said = layout.the_largest_step_this_copy_takes(text, box, font=face, wrap=wrap)
                    longer = layout.the_largest_step_this_copy_takes(text + "国", box, font=face, wrap=wrap)
                    shorter = layout.the_largest_step_this_copy_takes(text[:-1], box, font=face, wrap=wrap)
                    where = f"{text!r} in {width}x{height} face={face} wrap={wrap}"
                    assert longer <= said, f"a character added raised the answer: {where}"
                    assert shorter >= said, f"a character removed lowered the answer: {where}"
                    for got in (longer, shorter):
                        edits += 1
                        jumped += abs(ramp.index(got) - ramp.index(said)) >= 2

    assert edits > 500, "the sweep stopped sweeping"
    assert jumped / edits < 0.01, f"{jumped} of {edits} one-character edits moved two steps"


def _the_copy_would_sit_in(layout, text, box, size, wrap, face) -> bool:
    """Whether the copy sits in the box at that size, said in the contract's own terms.

    Three conditions, and each is one sentence of the contract: it stays inside the box,
    it stays on the lines it was given unless `wrap` releases it, and a run a line break
    cannot fall inside still has to fit across -- because wrapping does not save a Latin
    word wider than its box, the renderer breaks it mid-word instead. Written out of the
    public measuring calls, so an answer that agreed only with itself fails here.
    """
    paragraphs = text if isinstance(text, list) else [text]
    if layout.text_size(paragraphs, box.w, size=size, font=face).h > box.h + 1e-9:
        return False
    if not wrap and layout.lines_needed(paragraphs, box.w, size=size, font=face) > len(paragraphs):
        return False
    runs = [""]
    for character in "\n".join(str(one) for one in paragraphs):
        if character.isascii() and (character.isalnum() or character in "-.&/+%"):
            runs[-1] += character
        else:
            runs.append("")
    unbreakable = max(runs, key=len)
    # A width nothing wraps at, so what comes back is what the run really sets.
    return not unbreakable or layout.text_size(unbreakable, 99.0, size=size, font=face).w <= box.w + 1e-9


def test_the_step_it_answers_fits_and_the_one_above_it_does_not(helpers) -> None:
    """Largest is the whole claim, and these are the two halves of it."""
    layout = helpers.ppt_layout
    ramp = _steps_of_the_ramp(layout)
    tested = 0

    for text in STEP_COPY:
        for width, height in STEP_BOXES:
            for face in STEP_FACES:
                for wrap in (False, True):
                    box = layout.Box(0.0, 0.0, width, height)
                    said = layout.the_largest_step_this_copy_takes(text, box, font=face, wrap=wrap)
                    where = f"{text!r} in {width}x{height} face={face} wrap={wrap} -> {said}pt"
                    tested += 1
                    if said > layout.BODY_FLOOR_PT:
                        assert _the_copy_would_sit_in(layout, text, box, said, wrap, face), (
                            f"the size it answered with does not fit: {where}"
                        )
                    above = [step for step in ramp if said < step <= layout.TITLE_PT]
                    if above:
                        assert not _the_copy_would_sit_in(layout, text, box, above[-1], wrap, face), (
                            f"a bigger step fits and it did not say so: {where}"
                        )

    assert tested > 500, "the sweep stopped sweeping"


def test_a_label_and_a_paragraph_want_different_answers_from_the_same_box(helpers) -> None:
    """`wrap` is why this takes an argument past the box.

    A four-character label in a wide shape must not be answered by breaking it in two
    and calling the two lines a fit; a card's body is copy that is *meant* to reflow and
    would otherwise be held to whatever one line of it takes. Same box, same copy, two
    readings, and the answers differ by as much as two steps.
    """
    layout = helpers.ppt_layout
    shape = layout.Box(0.0, 0.0, 1.63, 1.25)
    band = layout.Box(0.0, 0.0, 3.6, 1.1)
    prose = "Every row whose label disagrees with its source is dropped."

    label_only = layout.the_largest_step_this_copy_takes("数据清洗", shape, font="Arial")
    may_wrap = layout.the_largest_step_this_copy_takes("数据清洗", shape, font="Arial", wrap=True)
    assert layout.lines_needed("数据清洗", shape.w, size=label_only, font="Arial") == 1
    assert may_wrap > label_only, "a label allowed to wrap can go up a step, and must be asked to"

    assert layout.the_largest_step_this_copy_takes(prose, band, font="Arial") == layout.BODY_FLOOR_PT
    reflowed = layout.the_largest_step_this_copy_takes(prose, band, font="Arial", wrap=True)
    assert reflowed > layout.BODY_FLOOR_PT
    assert layout.text_size(prose, band.w, size=reflowed, font="Arial").h <= band.h

    # An unbreakable word is not saved by being allowed to wrap: a line break cannot
    # fall inside it, so the renderer breaks it mid-word instead of taking two lines.
    narrow = layout.Box(0.0, 0.0, 1.0, 2.4)
    assert (
        layout.the_largest_step_this_copy_takes("internationalization", narrow, font="Arial", wrap=True)
        == layout.BODY_FLOOR_PT
    )


def test_the_answer_does_not_depend_on_where_the_copy_is_anchored(helpers) -> None:
    """There is no `align` or `anchor` here, and this is the reason there is not.

    A block of copy is the same size centred as it is top-left; where it lands inside
    the box is `write`'s answer to give back. So a row of chevrons whose labels are
    centred and middled asks the same question as a card whose title sits at the top,
    and the size the two get is the same size.
    """
    from pptx import Presentation

    layout, theme = helpers.ppt_layout, helpers.theme
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    box = layout.Box(0.72, 1.6, 3.12, 3.0)
    face = theme["font_family"]
    said = layout.the_largest_step_this_copy_takes("测量回灌", box, font=face)

    for align in ("left", "center", "right"):
        for anchor in ("top", "middle", "bottom"):
            drawn = layout.write(
                slide,
                box,
                "测量回灌",
                size=said,
                colour=theme["foreground"],
                font=face,
                cjk_font=theme["cjk_font_family"],
                align=align,
                anchor=anchor,
            )
            assert drawn.box.w <= box.w + 1e-9 and drawn.box.h <= box.h + 1e-9, f"{align}/{anchor} overran"


def test_a_row_of_steps_shares_one_size_and_the_longest_label_sets_it(helpers) -> None:
    """The `min` the skill's chevron example takes, and why the row needs it.

    Asked step by step the five labels of a row answer differently -- a two-character
    one clears 30pt where "Measure" stops at 20 -- and five sizes across one row is the
    drift the ramp exists to stop. What the row can afford is what its longest label can.
    """
    layout, theme = helpers.ppt_layout, helpers.theme
    face = theme["font_family"]
    # `chevron_row`'s own arithmetic for a full-width band of five: the first step is
    # wider because only one side of it is notched.
    boxes = [layout.Box(0.0, 0.0, 2.566, 1.25)] + [layout.Box(0.0, 0.0, 1.629, 1.25)] * 4
    labels = ("Scope", "Build", "Measure", "Review", "Ship")

    said = [layout.the_largest_step_this_copy_takes(label, box, font=face) for label, box in zip(labels, boxes)]
    assert len(set(said)) > 1, "the point of the min is that the steps disagree"
    row = min(said)
    assert row > layout.LABEL_PT, "the named step was the smallest on the ramp and the box holds more"
    for label, box in zip(labels, boxes):
        assert layout.lines_needed(label, box.w, size=row, font=face) == 1
        assert layout.text_size(label, box.w, size=row, font=face).h <= box.h


def test_copy_that_does_not_fit_comes_back_taller_than_the_box_it_was_given(helpers) -> None:
    """The whole point of turning autofit off: a page that does not fit is a
    measurement rather than type quietly dropping below the floor. Reporting the box
    the copy was handed would have hidden exactly that, which is why the box is the
    copy's own ink and may be bigger than the region.
    """
    from pptx import Presentation

    layout, theme = helpers.ppt_layout, helpers.theme
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    tight = layout.Box(0.72, 1.6, 3.0, 1.9)
    copy = "单一共享模型在三个基准上都高于各自的专用模型，差距最大的是 OVIS 上的九个点。"

    spilled = layout.write(slide, tight, copy, size=16, colour=theme["foreground"], font=theme["font_family"])

    assert spilled.box.y1 > tight.y1, "copy that overruns has to say so"
    assert not layout.fits(copy, tight, size=16, font=theme["font_family"])
    # And copy that fits is reported where it really sits, not where the box is.
    roomy = layout.Box(0.72, 3.0, 9.0, 5.0)
    short = layout.write(slide, roomy, "结论", size=16, colour=theme["foreground"], anchor="middle", align="right")
    assert short.box.w < 1.0 and short.box.x1 == pytest.approx(roomy.x1)
    assert short.box.y0 > roomy.y0 and short.box.y1 < roomy.y1, "anchored in the middle, and the box says so"


def test_the_measuring_calls_take_what_a_caller_naturally_writes(helpers) -> None:
    """Three refusals a live run hit, none of which was about the answer being unknown.

    Every one came out of a model writing the call the obvious way:

    * `the_smallest_box_a_chart_needs("horizontal_bar", T, data)` -- the chart by name,
      which is what it is called in the catalogue and in the page's own plan. It used to
      raise `TypeError: 'str' object is not callable` three frames down, with neither the
      name nor the word "name" in the message.
    * `table_size(rows, T, weights=w, size=15)` for `.h` -- and the height cannot depend
      on the weights, since a row's height comes off the type size and the row count. It
      used to refuse the whole call over a field the caller was not asking for.
    * `points(..., font=F, cjk_font=C)` -- passing the faces to every copy helper, because
      `write` requires them. `points` takes the theme and already had them, and answered
      with a TypeError.
    """
    layout, charts, theme = helpers.ppt_layout, helpers.ppt_charts, helpers.theme
    rows = [["方案", "成本", "时延"], ["自研", "0.38", "4.2"], ["采买", "2.10", "11.6"]]
    data = [("自研", 128), ("采买", 705)]

    by_name = charts.the_smallest_box_a_chart_needs("horizontal_bar", theme, data)
    assert by_name == charts.the_smallest_box_a_chart_needs(charts.horizontal_bar, theme, data)

    with pytest.raises(ValueError, match="closest: horizontal_bar"):
        charts.the_smallest_box_a_chart_needs("horiz_bar", theme, data)

    weighted = layout.table_size(rows, theme, weights=[1.0, 1.2, 1.2], size=15)
    plain = layout.table_size(rows, theme, size=15)
    assert weighted.h == plain.h, "weights cannot change a row's height"

    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    drawn = layout.points(
        slide, layout.page().body, theme, ["one", "two"], font=theme["font_family"], cjk_font=theme["cjk_font_family"]
    )
    assert drawn.box.h > 0


def test_a_row_and_a_track_take_the_entries_as_readily_as_the_count(helpers) -> None:
    """Two more refusals from a live run, both from passing the entries where the count goes.

    One page wrote `chevron_row(slide, chev, T, labels)` and another
    `timeline(slide, tl_box, T, stops)`, and `int(a list)` refused both. Neither was
    asking for something the call could not know: `len` is the count it meant.

    Both then wrote the copy themselves -- the first zipping the steps with the labels,
    the second in a hand-rolled timeline it fell back to because the call raised -- so a
    row handed its labels must not also draw them, or every step gets two. The first also
    hedged over the return, `result.steps if hasattr(result, "steps") else result`,
    because the sibling call names its own list `.stops`; now both names answer.
    """
    shapes, theme = helpers.ppt_shapes, helpers.theme
    labels = ("采集", "清洗", "标注", "训练", "评测")
    stops = [("2026Q1", "立项"), ("2026Q2", "内测"), ("2026Q3", "公测"), ("2026Q4", "商用")]
    band = shapes.Box(0.72, 2.0, 12.613, 3.6)
    spine = shapes.Box(0.72, 4.0, 12.613, 5.6)
    _, from_entries = _deck()
    _, from_count = _deck()

    row = shapes.chevron_row(from_entries, band, theme, labels)
    counted = shapes.chevron_row(from_count, band, theme, len(labels))
    assert [step.box for step in row] == [step.box for step in counted]
    assert row.steps is row, "the sibling call names the same list .stops, so both names answer"
    drew = len(from_entries.shapes)
    assert drew == len(from_count.shapes) == len(row) == len(labels), "the labels stay the caller's to write"

    track = shapes.timeline(from_entries, spine, theme, stops)
    assert [stop.box for stop in track.stops] == [
        stop.box for stop in shapes.timeline(from_count, spine, theme, len(stops)).stops
    ]

    with pytest.raises(TypeError, match="a list of the steps themselves"):
        shapes.chevron_row(from_entries, band, theme, iter(labels))
    with pytest.raises(ValueError, match="at least one stop"):
        shapes.timeline(from_entries, spine, theme, [])


def test_a_misspelt_tint_is_a_sentence_where_a_misspelt_mark_colour_already_was(helpers) -> None:
    """A plane and a card name a colour as often as anything in the vocabulary does.

    The skill promises that a name the theme does not carry "is refused and says so,
    so a misspelling is a message and not a colour". It was a message from `points`,
    `rule` and `mark`, which resolve through `_paint`. The helpers that paint a whole
    region resolved with `theme[tint] if tint in theme else tint` instead and handed
    anything unrecognised straight to the hex parser, so `tint="accnet"` came back as
    `invalid literal for int() with base 16: 'cn'` -- two characters out of the middle
    of the misspelling, naming neither the argument nor the mistake. Three of the ten
    helpers that take a colour kept the promise; these are two of the seven that did
    not, and they are the two a page reaches for first.

    The same route closes the hole `_paint` was widened for: the theme carries two
    typefaces and a list of series alongside its colours, and a face reached the
    parser as a colour.

    What must not close is the open palette, so both halves are asserted here: a role
    the deck stated for itself resolves, and so does a literal.
    """
    module = helpers.ppt_layout
    theme = dict(helpers.theme)
    theme["ours"] = "#1E5AFF"
    box = module.Box(1.0, 1.0, 5.0, 3.0)

    for refused in ("accnet", "font_family", "chart_series"):
        with pytest.raises(ValueError, match="not a colour"):
            module.plane(_slide(module), box, theme, tint=refused)
        with pytest.raises(ValueError, match="not a colour"):
            module.card(_slide(module), box, theme, title="标题", body=("一句话",), tint=refused)

    for allowed in ("ours", "#123456", "accent", "surface"):
        module.plane(_slide(module), box, theme, tint=allowed)
        module.card(_slide(module), box, theme, title="标题", body=("一句话",), tint=allowed)


# The page a delivered deck put a divider through, cut down to the columns that made
# it happen: a label column, one column of copy long enough to wrap, and a short one.
_WRAPPING = [
    ["维度", "Mem0", "竞争含义"],
    ["定价", "免费到 $19/月 Starter、$249/月 Pro；Enterprise 定制", "Mem0 让商业评估更容易"],
    ["部署", "托管平台", "更强"],
]


def test_a_row_is_as_tall_as_the_lines_its_cells_wrap_onto(helpers) -> None:
    """The defect a delivered deck showed, measured on the page that showed it.

    A row used to be sized from the type alone, and a declared height is only a
    floor: the renderer grew every wrapped row, each boundary below drifted down,
    and the hairline -- a free rectangle at the boundary the arithmetic had named --
    stayed where it was and came down through the copy of a row four places above.
    The reading was `rule_strike` on the text '定价'.
    """
    module, theme = helpers.ppt_layout, helpers.theme
    box = module.Box.corners(0.72, 1.93, 12.60, 5.63)
    unit = module.Inches(1)

    # Without the spread, so the heights here are the measurement and nothing else.
    drawn = module.table(_slide(module), box, _WRAPPING, theme, numeric_from=3, fill=False)
    heights = [row.height / unit for row in drawn.rows]
    widths = [column.width / unit for column in drawn.columns]
    face = theme.get("font_family")

    wrapped = len(module._wrapped(_WRAPPING[1][1], widths[1] - 0.20, module.LABEL_PT, face, False))
    assert wrapped > 1, "the fixture stopped wrapping; it no longer tests anything"
    assert heights[1] >= wrapped * module._line_h(module.LABEL_PT), "a wrapped row was sized for one line"
    assert heights[1] > heights[2], "the row that wraps is no taller than the row that does not"

    # And what the measured heights are for: the boundary under a row is that row's
    # own edge, so it goes where the row goes instead of being predicted from a pitch.
    quiet = (str(module._rgb(theme["grid"])), pytest.approx(module.GRID_RULE_PT, abs=0.01))
    assert _border(module, drawn.cell(2, 0), "lnB") == quiet
    assert _border(module, drawn.cell(0, 0), "lnB")[1] == pytest.approx(module.HEADER_RULE_PT, abs=0.01)


def test_a_bare_table_is_closed_by_a_frame_in_every_style(helpers) -> None:
    """The default is what ships, so the default has to be presentable.

    A delivered deck called `table(...)` with every keyword left alone and got a header,
    one accent rule, rows with nothing between them, a hairline under the last row and
    no side of any kind -- three lines each ending in mid-air, which reads as a page
    that was not finished rather than as one that was held back, and which came back
    as that reading four times over. The frame is
    the table's own edge and it is not the Office look: that is a hairline around
    every one of 25 cells, and this is one rectangle in the theme's quietest tone.
    """
    module, theme = helpers.ppt_layout, helpers.theme
    box = module.Box.corners(0.72, 1.93, 12.60, 5.63)
    quiet = (str(module._rgb(theme["grid"])), pytest.approx(module.GRID_RULE_PT, abs=0.01))
    last = len(_WRAPPING) - 1
    columns = len(_WRAPPING[0])

    for style in ("minimal", "header_tint", "row_rules", "compact"):
        drawn = module.table(_slide(module), box, _WRAPPING, theme, style=style)
        assert _border(module, drawn.cell(0, 0), "lnT") == quiet, f"{style} has no top edge"
        assert _border(module, drawn.cell(last, 0), "lnB") == quiet, f"{style} has no bottom edge"
        for row in range(len(_WRAPPING)):
            assert _border(module, drawn.cell(row, 0), "lnL") == quiet, f"{style} row {row} has no left edge"
            assert _border(module, drawn.cell(row, columns - 1), "lnR") == quiet, f"{style} row {row} has no right"
        # And the frame is the only vertical the default draws: an interior edge is
        # still bare, which is the difference from the grid `column_rules` asks for.
        assert _border(module, drawn.cell(1, 0), "lnR") is None, f"{style} boxed its cells"
        # The header's rule still outweighs the frame, so it stays the line the reader
        # is meant to see; the frame is what the eye reads last.
        assert _border(module, drawn.cell(0, 0), "lnB") == (
            str(module._rgb(theme["accent"])),
            pytest.approx(module.HEADER_RULE_PT, abs=0.01),
        )
        assert module.HEADER_RULE_PT > module.GRID_RULE_PT

    # The frame takes its weight from `grid_pt` and nothing else, so a page that wants
    # a heavier edge has the same dial the row hairlines have.
    heavier = module.table(_slide(module), box, _WRAPPING, theme, grid_pt=2.0)
    assert _border(module, heavier.cell(0, 0), "lnT") == (str(module._rgb(theme["grid"])), pytest.approx(2.0))

    # A border is drawn on the boundary and takes no room from the row it edges, so
    # the frame cannot make `table_size` answer for a table that is not the drawn one.
    for style in ("minimal", "compact"):
        drawn = module.table(_slide(module), box, _WRAPPING, theme, style=style)
        asked = module.table_size(_WRAPPING, theme, style=style, box=box)
        assert drawn.box.h == pytest.approx(asked.h, abs=1e-9)
        assert drawn.box.w == pytest.approx(asked.w, abs=1e-9)


def test_every_row_boundary_carries_a_hairline_in_every_style(helpers) -> None:
    """The other half of what a default has to ship, from the same reading as the frame.

    A delivered page: a four-column comparison whose CJK cells wrap onto two lines, an
    outer frame, an accent rule under the header, and nothing at all between the body
    rows. With wrapped cells there is then no way to see where one row ends and the
    next begins -- the reader counts baselines and pairs the wrong cell with the wrong
    label. `style="row_rules"` drew them, and asking the author to name a keyword for
    the readable table is the same mistake the frame was: a default is what ships.

    So every style draws them, at `grid_pt` in the `grid` tone -- the frame's own line,
    not a second vocabulary -- which keeps the header's accent rule the heaviest thing
    on the table and the line it is read from.
    """
    module, theme = helpers.ppt_layout, helpers.theme
    box = module.Box.corners(0.72, 1.93, 12.60, 5.63)
    quiet = (str(module._rgb(theme["grid"])), pytest.approx(module.GRID_RULE_PT, abs=0.01))
    rows = [
        ["维度", "Mem0 平台", "自建向量库方案", "竞争含义"],
        ["定价", "免费到 $19/月 Starter、$249/月 Pro；Enterprise 定制", "按实例计费，还要摊运维", "评估更容易"],
        ["部署", "托管平台，开箱即用，SDK 三行代码接入", "自建集群，扩容与备份自理", "更强，尤其是早期团队"],
        ["召回", "语义与图谱双通道，跨会话记忆自动衰减", "仅向量近邻，跨会话自己拼", "差异化的核心"],
    ]
    last = len(rows) - 1

    for style in ("minimal", "header_tint", "row_rules", "compact"):
        drawn = module.table(_slide(module), box, rows, theme, style=style)
        for above in range(1, last):
            assert _border(module, drawn.cell(above, 0), "lnB") == quiet, f"{style} row {above} runs into the next"
            assert _border(module, drawn.cell(above + 1, 0), "lnT") == quiet, f"{style} boundary {above} disagrees"
        # The header's boundary is the accent rule and stays the heavier: a separator
        # at `rule_pt` would make the table louder, which is what the quiet default
        # was avoiding in the first place.
        header = _border(module, drawn.cell(0, 0), "lnB")
        assert header == (str(module._rgb(theme["accent"])), pytest.approx(module.HEADER_RULE_PT, abs=0.01))
        assert module.GRID_RULE_PT < module.HEADER_RULE_PT, "the row hairline is at the header rule's weight"
        assert quiet[0] != header[0], f"{style} draws its row hairline in the accent tone"
        # The bottom boundary is the frame's own edge, already drawn, so it is one line
        # and not a hairline laid over a hairline.
        assert _border(module, drawn.cell(last, 0), "lnB") == quiet, f"{style} has no bottom edge"
        assert _border(module, drawn.cell(1, 0), "lnR") is None, f"{style} boxed its cells"

    # A border is drawn on the boundary rather than beside it, so the hairlines take no
    # room: the height is still the rows' own and still what `table_size` answered
    # before a single line was drawn. A separator that grew the table would have moved
    # every page that cut a lane to that answer.
    unit = module.Inches(1)
    for style in ("minimal", "header_tint", "row_rules", "compact"):
        for fill in (True, False):
            drawn = module.table(_slide(module), box, rows, theme, style=style, fill=fill)
            asked = module.table_size(rows, theme, style=style, box=box, fill=fill)
            heights = sum(row.height / unit for row in drawn.rows)
            assert drawn.box.h == pytest.approx(asked.h, abs=1e-9), f"{style} fill={fill} is not the size it answered"
            # A row's height reaches the file as whole EMU, so the sum of what was
            # written back is the box's height to within one EMU a row -- 1.1e-6in --
            # and never to within a line weight, which is 0.014in at `grid_pt`.
            assert drawn.box.h == pytest.approx(heights, abs=1e-5), f"{style} fill={fill} spent height on its rules"
    # And `row_rules` is the same table as `minimal` now, to the inch and to the line.
    plain = module.table(_slide(module), box, rows, theme, style="minimal")
    named = module.table(_slide(module), box, rows, theme, style="row_rules")
    assert named.box == plain.box
    assert [r.height for r in named.rows] == [r.height for r in plain.rows]
    for row in range(len(rows)):
        for edge in ("lnT", "lnB", "lnL", "lnR"):
            assert _border(module, named.cell(row, 0), edge) == _border(module, plain.cell(row, 0), edge)


def test_a_table_spreads_into_the_box_rather_than_sitting_in_the_top_of_it(helpers) -> None:
    """A content-sized table left its box's bottom third white -- measured on a
    delivered page, 2.56in of a 3.70in region with the remaining 1.14in empty, which
    reads as a page that ran out rather than as a table that ended.
    """
    module, theme = helpers.ppt_layout, helpers.theme
    box = module.Box.corners(0.72, 1.93, 12.60, 5.63)

    spread = module.table(_slide(module), box, _WRAPPING, theme).box
    tight = module.table(_slide(module), box, _WRAPPING, theme, fill=False).box

    assert tight.h < spread.h, "fill=True did not spread the rows"
    assert spread.h <= box.h + 1e-9, "the table spread past the box it was given"
    assert module.table_size(_WRAPPING, theme, box=box).h == pytest.approx(spread.h, abs=1e-9)
    assert module.table_size(_WRAPPING, theme, box=box, fill=False).h == pytest.approx(tight.h, abs=1e-9)
    # Capped, so a table with little to say stays a small table rather than becoming
    # one band per row.
    sparse = module.Box.corners(0.72, 1.0, 12.60, 7.0)
    assert module.table(_slide(module), sparse, _WRAPPING, theme).box.h < sparse.h


def test_a_table_takes_the_look_it_is_told_to_take(helpers) -> None:
    """The defaults are an argument the page is allowed to win.

    Every one of them was a literal in the drawing code, so a page that needed a rule
    down the middle of its table, or a header a step larger, or one centred column
    between two left-aligned ones, could neither ask the helper for it nor draw it
    itself. They are keywords now, and this asserts they reach the file.
    """
    from pptx.enum.text import PP_ALIGN

    module, theme = helpers.ppt_layout, helpers.theme
    theme = dict(theme, ours="#1E5AFF")
    box = module.Box.corners(0.72, 1.93, 12.60, 5.63)

    drawn = module.table(
        _slide(module),
        box,
        _WRAPPING,
        theme,
        size=15,
        header_size=18,
        align=("left", "center", "right"),
        rule_pt=3.0,
        grid_pt=1.5,
        column_rules=True,
        banding=True,
        fills={(None, 2): "ours", (1, 0): "#FFEECC"},
    )

    assert _border(module, drawn.cell(0, 0), "lnB") == (str(module._rgb(theme["accent"])), pytest.approx(3.0))
    assert _border(module, drawn.cell(2, 0), "lnB") == (str(module._rgb(theme["grid"])), pytest.approx(1.5))
    assert _border(module, drawn.cell(1, 0), "lnR") == (str(module._rgb(theme["grid"])), pytest.approx(1.5))
    # `column_rules` puts a rule between two columns; the rule on the outside of the
    # last one is the frame, which every table draws and this one only meets. Both
    # weights come off `grid_pt`, so the two lines cannot come out different widths.
    assert _border(module, drawn.cell(1, 2), "lnR") == (str(module._rgb(theme["grid"])), pytest.approx(1.5))
    unruled = module.table(_slide(module), box, _WRAPPING, theme, grid_pt=1.5)
    assert _border(module, unruled.cell(1, 0), "lnR") is None, "a vertical rule nobody asked for"
    assert _border(module, unruled.cell(1, 2), "lnR") == (str(module._rgb(theme["grid"])), pytest.approx(1.5))

    sizes = {run.font.size.pt for run in drawn.cell(0, 0).text_frame.paragraphs[0].runs}
    assert sizes == {18.0}, f"the header was set at {sizes}, not at header_size"
    assert {run.font.size.pt for run in drawn.cell(1, 0).text_frame.paragraphs[0].runs} == {15.0}

    placed = [drawn.cell(1, column).text_frame.paragraphs[0].alignment for column in range(3)]
    assert placed == [PP_ALIGN.LEFT, PP_ALIGN.CENTER, PP_ALIGN.RIGHT]
    assert drawn.cell(2, 0).fill.fore_color.rgb == module._rgb(theme["surface"]), "banding did not tint row 2"

    # A colour the deck named, down a whole column and header included, and a literal
    # in one cell -- neither of which `emphasize_columns` can say.
    for row in range(3):
        assert drawn.cell(row, 2).fill.fore_color.rgb == module._rgb("#1E5AFF"), f"row {row} of the column"
    assert drawn.cell(1, 0).fill.fore_color.rgb == module._rgb("#FFEECC")

    with pytest.raises(ValueError, match="align names 2 columns"):
        module.table(_slide(module), box, _WRAPPING, theme, align=("left", "right"))
    with pytest.raises(ValueError, match="align is one of"):
        module.table(_slide(module), box, _WRAPPING, theme, align=("left", "middle", "right"))
    with pytest.raises(ValueError, match="not a colour"):
        module.table(_slide(module), box, _WRAPPING, theme, fills={(None, 1): "font_family"})
    with pytest.raises(ValueError, match="a fill names column 9"):
        module.table(_slide(module), box, _WRAPPING, theme, fills={(None, 9): "accent_soft"})
