"""The preset catalogue as a data file: it loads, it is complete, and it evaluates.

The route could draw two rectangles before this existed, so every process page was
a row of boxes and the arrows were whitespace. What makes the fix real is not that
`MSO_SHAPE` has 177 members -- python-pptx always had those -- but that something
can answer *where the point lands*, and python-pptx cannot: it carries the names
and the default `avLst` and no `gdLst` or `pathLst` at all. The guide formulas are
here, so the questions worth testing are whether the packaged copy is the whole
standard table, whether every one of them evaluates, and whether the numbers that
come out are the numbers Office draws.

The lookup half is the same argument the icon set already paid for: an author on
the script route gets no schema enum, so a name that misses has to answer with
what was probably meant rather than with "no".
"""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

import pytest

from raven.ppt.services.assets import shapes
from raven.ppt.services.assets.shapes import (
    ADJUSTMENT_SCALE,
    CONNECTOR_PRESETS,
    PRESET_COUNT,
    ShapeDataError,
    ShapeFormulaError,
    UnknownPresetError,
    angle_adjustments,
    catalog_json,
    drawable_presets,
    intent_provenance,
    preset_adjustments,
    preset_candidates,
    preset_geometry,
    preset_groups,
    preset_intent,
    preset_names,
    preset_provenance,
    resolve_preset_name,
    unscalable_adjustments,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# The shapes a deck actually reaches for, so a data edit that drops one is caught
# here rather than by a page that stops drawing its process.
FOUNDATIONAL_PRESET_NAMES = {
    "chevron",
    "homePlate",
    "rightArrow",
    "leftRightArrow",
    "bentArrow",
    "circularArrow",
    "roundRect",
    "rect",
    "ellipse",
    "triangle",
    "diamond",
    "hexagon",
    "flowChartProcess",
    "flowChartDecision",
    "flowChartTerminator",
    "flowChartDocument",
    "flowChartInputOutput",
    "plus",
    "star5",
    "wedgeRectCallout",
}

# Wide, tall, square and a sliver -- the four `test_every_preset_evaluates_in_any_
# frame_it_is_given` uses, because a guide that branches on the short side can be
# right in one of them and wrong in another.
FRAMES = ((4.0, 2.0), (2.0, 4.0), (3.0, 3.0), (6.0, 0.2))

# A hundred EMU, which is python-pptx's own unit and about a ten-thousandth of an
# inch. `heptagon`, `star7` and `star10` need it: the standard scales their frame up
# by `hf`/`vf` before laying the vertices out on five-digit cosines, and the rounding
# in those constants puts the bottom vertex 2e-5in past the edge. That is a point on
# the edge, not a point outside the shape.
EDGE_SLACK = 100 / 914400


def _polygonal() -> tuple[str, ...]:
    """Every preset whose outline is straight lines only, decided from the data.

    Hand-listing them covered 17 of the 112 there are. The rule is what the sentence
    below the list always meant -- `points()` describes the whole outline exactly
    when nothing in it curves -- so reading it off the path commands is both the
    honest definition and four times the coverage for nothing.
    """
    straight = {"moveTo", "lnTo", "close"}
    return tuple(
        name
        for name in preset_names()
        if {command for path in preset_geometry(name, 4.0, 2.0).paths for command, _ in path} <= straight
    )


POLYGONAL = _polygonal()

# The straight-line presets that really do put a point outside their frame, each
# because the standard draws them that way. Named one at a time rather than excluded
# by a rule, so a shape that starts leaving its frame for some new reason fails.
LEAVES_ITS_FRAME = {
    # A callout points at something outside itself, so its leader ends outside the
    # box by design: the default adjustments put the tip up to 2.8in clear of a
    # 6.0x0.2 frame. Drawing one inside its own box would be the defect.
    "callout1",
    "callout2",
    "callout3",
    "accentCallout1",
    "accentCallout2",
    "accentCallout3",
    "borderCallout1",
    "borderCallout2",
    "borderCallout3",
    "accentBorderCallout1",
    "accentBorderCallout2",
    "accentBorderCallout3",
    "wedgeRectCallout",
    # Both tab shapes size their marks off `mod w h 0` -- the frame's diagonal -- so
    # in the 6.0x0.2 sliver each corner mark is half again as tall as the frame is.
    "cornerTabs",
    "squareTabs",
    # The crossing bar is struck at a fixed angle and its horizontal reach scales off
    # half the height, so in the 2.0x4.0 portrait frame it runs 0.11in past both sides.
    "mathNotEqual",
}


def test_the_data_file_loads_and_carries_its_provenance() -> None:
    provenance = preset_provenance()
    assert provenance["version"] == 1
    assert provenance["upstream"]["package"] == "apache-poi"
    assert provenance["upstream"]["version"] == "5.4.1"
    assert provenance["license_file"] == "LICENSES/APACHE-2.0-apache-poi.txt"

    intents = intent_provenance()
    assert intents["version"] == 1
    assert intents["upstream"]["package"] == "ppt-master"
    assert intents["license_file"] == "LICENSES/MIT-ppt-master.txt"


def test_sha256_means_the_bytes_as_served_in_every_data_file_that_has_one() -> None:
    """One field name had two meanings, and the one that reads as the obvious check failed.

    `preset_shapes.json` recorded the POI file with its CRLF folded to LF, because
    that is the form the converter reads; `preset_shape_intents.json` recorded raw
    bytes. Both fields were called `sha256`, so `curl <file> | sha256sum` -- the check
    anyone would run first -- reproduced the intents hash and disagreed with the
    geometry one, which looks exactly like a tampered file and is not.

    The normalised hash is still worth keeping: a git checkout on Windows hands the
    converter CRLF, and then LF is the only form the two ends agree on. So it kept its
    value and lost the ambiguous name. `sha256` now means the same thing in all three
    data files -- the artifact's bytes as served -- and `sha256_lf` says when it is
    something else.
    """
    geometry = preset_provenance()["upstream"]
    assert geometry["sha256"] == "a7dad593d27bd70536b41da9b761fa16409536cc0c25ef2b6c7a61c5d9b3e738"
    assert geometry["sha256_lf"] == "4a762444d8d85876881c02a5b1dedf6f73006fcd8acb7b4e393435615b37c780"
    assert geometry["sha256"] != geometry["sha256_lf"], "the file has CRLF in it, so these cannot be equal"

    # Every `sha256` across the packaged data files is a hash of bytes as served, so a
    # new one that quietly means something else has to rename itself the way this did.
    data = Path(str(files("raven.ppt.services.assets.data")))
    for path in sorted(data.glob("*.json")):
        upstream = json.loads(path.read_text(encoding="utf-8")).get("upstream") or {}
        for field, value in upstream.items():
            if field.startswith("sha256"):
                assert len(value) == 64 and set(value) <= set("0123456789abcdef"), f"{path.name}.{field}={value!r}"
        if "sha256" in upstream:
            assert "sha256_lf" not in upstream or upstream["sha256"] != upstream["sha256_lf"], path.name


def test_the_licences_travelled_with_the_data() -> None:
    """Third-party data is only shippable with its licence and its notice."""
    geometry_licence = (REPO_ROOT / str(preset_provenance()["license_file"])).read_text(encoding="utf-8")
    intent_licence = (REPO_ROOT / str(intent_provenance()["license_file"])).read_text(encoding="utf-8")
    notices = (REPO_ROOT / "NOTICES.md").read_text(encoding="utf-8")

    assert "Apache License" in geometry_licence
    assert "Version 2.0" in geometry_licence
    assert "MIT License" in intent_licence
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in intent_licence

    assert "Apache POI" in notices
    assert "ppt-master" in notices
    assert "LICENSES/APACHE-2.0-apache-poi.txt" in notices
    assert "LICENSES/MIT-ppt-master.txt" in notices
    # The notices have to point at where the data actually lives.
    assert "raven/ppt/services/assets/data/preset_shapes.json" in notices
    assert "raven/ppt/services/assets/data/preset_shape_intents.json" in notices


def test_the_whole_standard_table_is_here_and_nothing_else_is() -> None:
    names = preset_names()
    assert len(names) == PRESET_COUNT
    assert len(set(names)) == len(names)
    assert list(names) == sorted(names)
    assert FOUNDATIONAL_PRESET_NAMES <= set(names)


def test_the_drawable_set_is_exactly_what_python_pptx_can_add() -> None:
    """The one boundary this module does not get to decide.

    Ten of the 187 are connectors, placed through `add_connector` rather than
    `add_shape`, so a catalogue that offered them by name would hand an author a
    `KeyError` on a name the standard does define. The constant naming them is
    asserted against python-pptx rather than trusted, because it is python-pptx's
    coverage that decides it and that can move under us.
    """
    pytest.importorskip("pptx", reason="ppt extra not installed")
    from pptx.enum.shapes import MSO_SHAPE

    addable = {member.xml_value for member in MSO_SHAPE.__members__.values()}
    assert set(drawable_presets()) == addable
    assert set(preset_names()) - addable == set(CONNECTOR_PRESETS)
    assert len(drawable_presets()) == 177


@pytest.mark.parametrize("name", preset_names())
def test_every_preset_evaluates_in_any_frame_it_is_given(name: str) -> None:
    """Wide, tall, square and a sliver, because the guides branch on which is which.

    `ss` is the short side and half the catalogue is built off it, so a formula that
    is right for a landscape frame can still divide by zero in a portrait one. The
    sliver is there because a preset placed in a table cell really is that shape.
    """
    for width, height in ((4.0, 2.0), (2.0, 4.0), (3.0, 3.0), (6.0, 0.2)):
        geometry = preset_geometry(name, width, height)
        assert geometry.name == name
        assert (geometry.width, geometry.height) == (width, height)
        assert geometry.paths
        for path in geometry.paths:
            for command, parameters in path:
                assert command in {"moveTo", "lnTo", "quadBezTo", "cubicBezTo", "arcTo", "close"}
                assert all(isinstance(value, float) for value in parameters)


@pytest.mark.parametrize("name", sorted(set(POLYGONAL) - LEAVES_ITS_FRAME))
def test_a_shape_made_of_lines_stays_inside_the_frame_it_was_given(name: str) -> None:
    """A point outside the box is a shape that will not sit where the page put it."""
    for width, height in FRAMES:
        geometry = preset_geometry(name, width, height)
        points = geometry.points()
        assert points
        for x, y in points:
            assert -EDGE_SLACK <= x <= width + EDGE_SLACK, f"{name} leaves the {width}x{height} frame at x={x}"
            assert -EDGE_SLACK <= y <= height + EDGE_SLACK, f"{name} leaves the {width}x{height} frame at y={y}"


def test_the_shapes_excused_from_the_frame_check_are_the_ones_that_need_excusing() -> None:
    """The exception list is the part that rots: a name kept in it after the geometry
    stopped needing it is coverage silently given away, which is how the hand-written
    list this replaced came to cover 17 of 112. So the list is checked against the
    geometry rather than trusted, in both directions.
    """
    outside = set()
    for name in POLYGONAL:
        for width, height in FRAMES:
            escapes = (
                not (-EDGE_SLACK <= x <= width + EDGE_SLACK) or not (-EDGE_SLACK <= y <= height + EDGE_SLACK)
                for x, y in preset_geometry(name, width, height).points()
            )
            if any(escapes):
                outside.add(name)
                break

    assert outside == LEAVES_ITS_FRAME, {
        "left the frame but is not excused": sorted(outside - LEAVES_ITS_FRAME),
        "excused but stays in the frame": sorted(LEAVES_ITS_FRAME - outside),
    }
    assert len(POLYGONAL) == 112, f"{len(POLYGONAL)} presets are straight lines only, not 112"
    assert len(set(POLYGONAL) - LEAVES_ITS_FRAME) == 96


def test_the_chevron_resolves_to_the_numbers_office_draws() -> None:
    """The one shape a five-step process is built out of, checked point by point.

    A 217x84 frame with the default half adjustment: the notch is 42 in from the
    left, the body ends at 175, and the point is at (217, 42). Everything
    `chevron_row` computes is a projection of these six numbers, so they are the
    thing worth writing down.
    """
    geometry = preset_geometry("chevron", 217, 84)

    assert geometry.adjustments == {"adj": 50000.0}
    assert geometry.guides["x1"] == 42.0
    assert geometry.guides["x2"] == 175.0
    assert geometry.text_rect == (42.0, 0.0, 175.0, 84.0)
    assert geometry.points() == ((0.0, 0.0), (175.0, 0.0), (217.0, 42.0), (175.0, 84.0), (0.0, 84.0), (42.0, 42.0))

    # The notch is `min(width, height) * adj`, which is the closed form the row
    # helper lays its steps out on -- in both orientations, since `ss` swaps.
    assert preset_geometry("chevron", 217, 84, {"adj": 25000}).guides["x1"] == 21.0
    assert preset_geometry("chevron", 84, 217).guides["x1"] == 42.0


def test_an_adjustment_moves_the_geometry_and_an_unknown_one_says_so() -> None:
    tight = preset_geometry("roundRect", 4.0, 2.0, {"adj": 0})
    round_ = preset_geometry("roundRect", 4.0, 2.0, {"adj": 50000})

    assert tight.guides["x1"] == 0.0
    assert round_.guides["x1"] == 1.0  # half the short side
    assert preset_geometry("roundRect", 4.0, 2.0).guides["x1"] == pytest.approx(0.33334)

    with pytest.raises(ShapeFormulaError, match="no adjustment"):
        preset_geometry("roundRect", 4.0, 2.0, {"radius": 10})
    with pytest.raises(ShapeFormulaError, match="it takes none"):
        preset_geometry("rect", 4.0, 2.0, {"adj": 10})


def test_the_defaults_are_the_standard_s_where_python_pptx_disagrees_with_it() -> None:
    """Two of python-pptx's 117 adjustment tables are wrong, and this is the record.

    Compared shape by shape against `shape.adjustments`, 115 agree exactly. The two
    that do not are `foldedCorner`, which python-pptx gives no adjustment at all
    where the standard gives it one, and `upDownArrow`, where its table lists
    `adj1` and `adj2` twice each. Both are silent: the first makes the knob
    unreachable and the second writes the same `a:gd` name twice. It is why the
    script helper writes adjustments by name from this table instead.
    """
    pytest.importorskip("pptx", reason="ppt extra not installed")
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    disagreed, compared = {}, 0
    for member in MSO_SHAPE.__members__.values():
        ours = preset_adjustments(member.xml_value)
        shape = slide.shapes.add_shape(member, Inches(1), Inches(1), Inches(2), Inches(1))
        theirs = [float(value) for value in shape.adjustments]
        if len(theirs) != len(ours):
            disagreed[member.xml_value] = (len(ours), len(theirs))
            continue
        for value, effective in zip(ours.values(), theirs):
            compared += 1
            assert value / ADJUSTMENT_SCALE == pytest.approx(effective), member.xml_value

    assert compared > 250
    assert disagreed == {"foldedCorner": (1, 0), "upDownArrow": (2, 4)}


def test_a_preset_that_takes_no_adjustment_says_so_rather_than_guessing() -> None:
    assert preset_adjustments("rect") == {}
    assert dict(preset_adjustments("chevron")) == {"adj": 50000.0}
    assert dict(preset_adjustments("gear6")) == {"adj1": 15000.0, "adj2": 3526.0}
    # An angle is not a proportion, and forcing one scale on both would be a lie.
    assert dict(preset_adjustments("arc")) == {"adj1": 16200000.0, "adj2": 0.0}


def test_lookup_accepts_any_spelling_of_a_name() -> None:
    """The catalogue writes `rightArrow`; an author writes what it remembers."""
    assert resolve_preset_name("rightArrow") == "rightArrow"
    assert resolve_preset_name("right_arrow") == "rightArrow"
    assert resolve_preset_name("  RIGHT ARROW  ") == "rightArrow"
    assert resolve_preset_name("flowchart-decision") == "flowChartDecision"
    assert preset_geometry("right arrow", 4, 2).name == "rightArrow"


@pytest.mark.parametrize(
    ("attempt", "expected"),
    [
        # Plurals and separators, which is how a name usually misses.
        ("chevrons", "chevron"),
        ("flowchart_decison", "flowChartDecision"),
        ("round_rectangle", "roundRect"),
        # A word the catalogue does use, in a name it does not have.
        ("arrowRight", "rightArrow"),
        ("star", "star5"),
    ],
)
def test_a_miss_names_the_preset_that_was_probably_meant(attempt: str, expected: str) -> None:
    with pytest.raises(UnknownPresetError) as caught:
        resolve_preset_name(attempt)
    assert expected in caught.value.candidates
    assert expected in str(caught.value)
    assert caught.value.name == attempt


def test_a_miss_with_nothing_close_still_says_where_to_look() -> None:
    with pytest.raises(UnknownPresetError, match="PRESET_NAMES") as caught:
        resolve_preset_name("zzzzzzzz")
    assert caught.value.candidates == []
    assert str(PRESET_COUNT) in str(caught.value)


def test_a_miss_is_a_lookup_error_not_a_key_error() -> None:
    """KeyError's str() quotes the whole message, and this one is a sentence."""
    with pytest.raises(LookupError) as caught:
        resolve_preset_name("definitely-not-a-shape")
    assert not isinstance(caught.value, KeyError)
    assert str(caught.value).startswith("unknown preset shape 'definitely-not-a-shape'")


def test_candidates_are_never_junk_and_never_the_whole_set() -> None:
    names = set(preset_names())
    for attempt in ("arrow", "box", "flow", "circle", "zzzzzzzz", "", "   "):
        candidates = preset_candidates(attempt)
        assert len(candidates) <= 8
        assert len(set(candidates)) == len(candidates)
        assert set(candidates) <= names


def test_every_preset_has_one_line_saying_what_it_is_for() -> None:
    for name in preset_names():
        intent = preset_intent(name)
        assert intent and intent[0].isupper() and intent.endswith(".")

    groups = preset_groups()
    assert len(groups) == 43
    assert sum(len(group["presets"]) for group in groups) == PRESET_COUNT
    with pytest.raises(TypeError):
        groups[0]["label"] = "edited"  # type: ignore[index]


def test_the_catalogue_that_travels_into_a_build_directory_is_names_and_intents() -> None:
    catalog = json.loads(catalog_json())
    assert set(catalog) == set(drawable_presets())
    for name, entry in catalog.items():
        assert entry["adj"] == list(preset_adjustments(name))
        assert entry["for"] == preset_intent(name)


def test_what_comes_back_cannot_be_edited_under_another_caller() -> None:
    geometry = preset_geometry("chevron", 4, 2)
    with pytest.raises(TypeError):
        geometry.guides["x1"] = 0  # type: ignore[index]
    with pytest.raises(TypeError):
        preset_adjustments("chevron")["adj"] = 0  # type: ignore[index]


def test_a_formula_that_cannot_be_evaluated_says_which_one() -> None:
    frame = shapes._Frame(4.0, 2.0)

    assert frame.evaluate("*/ w 1 2") == 2.0
    assert frame.evaluate("*/ w 1 0") == 0.0  # the standard's own divide-by-zero
    assert frame.evaluate("+/ w h 0") == 0.0
    assert frame.evaluate("pin 0 5 3") == 3.0
    assert frame.resolve("wd8") == 0.5
    assert frame.resolve("3cd4") == pytest.approx(shapes.FULL_CIRCLE * 3 / 4)

    with pytest.raises(ShapeFormulaError, match="unsupported guide operator"):
        frame.evaluate("nope w h")
    with pytest.raises(ShapeFormulaError, match="takes 3 operands"):
        frame.evaluate("*/ w 1")
    with pytest.raises(ShapeFormulaError, match="no such guide"):
        frame.resolve("nosuchguide")
    with pytest.raises(ShapeFormulaError, match="not a guide value"):
        frame.value("w h")
    with pytest.raises(ShapeFormulaError, match="must be finite"):
        frame.value(float("nan"))
    with pytest.raises(ShapeFormulaError, match="must be a number"):
        frame.value(None)  # type: ignore[arg-type]
    with pytest.raises(ShapeFormulaError, match="no negative side"):
        shapes._Frame(-1.0, 2.0)
    # No preset in the table asks for the root of a negative, and a regenerated one
    # that did would produce a shape rather than an exception without this.
    with pytest.raises(ShapeFormulaError, match="cannot evaluate"):
        frame.evaluate("sqrt -1")


def test_malformed_data_is_refused_rather_than_half_drawn() -> None:
    with pytest.raises(ShapeDataError, match="must be an object"):
        shapes._validate("bogus", [])
    with pytest.raises(ShapeDataError, match="not a \\[name, formula\\] pair"):
        shapes._validate("bogus", {"gd": [["x1"]], "paths": []})
    with pytest.raises(ShapeDataError, match="four expressions"):
        shapes._validate("bogus", {"rect": ["l", "t"], "paths": []})
    with pytest.raises(ShapeDataError, match="no path list"):
        shapes._validate("bogus", {})
    with pytest.raises(ShapeDataError, match="unsupported path command"):
        shapes._validate("bogus", {"paths": [{"c": [["swirlTo", "l", "t"]]}]})
    with pytest.raises(ShapeDataError, match="needs 2 parameters"):
        shapes._validate("bogus", {"paths": [{"c": [["moveTo", "l"]]}]})


def test_a_data_file_that_is_not_the_catalogue_is_refused_at_load(monkeypatch) -> None:
    """A packaging mistake now shows up as a missing asset, not a missing module."""
    monkeypatch.setattr(shapes, "_read", lambda _: {"version": 2})
    shapes._load.cache_clear()
    try:
        with pytest.raises(ShapeDataError, match="version 1"):
            shapes._load()
        monkeypatch.setattr(shapes, "_read", lambda _: {"version": 1, "shapes": {}, "order": []})
        shapes._load.cache_clear()
        with pytest.raises(ShapeDataError, match=f"{PRESET_COUNT} names"):
            shapes._load()
        monkeypatch.setattr(shapes, "_read", lambda _: {"version": 1, "shapes": {}})
        shapes._load.cache_clear()
        with pytest.raises(ShapeDataError, match="shape table and the enum order"):
            shapes._load()
    finally:
        shapes._load.cache_clear()
        shapes._by_key.cache_clear()
        shapes._intents.cache_clear()


def test_an_intent_table_that_does_not_match_the_catalogue_is_refused(monkeypatch) -> None:
    """Two files that have to agree, so nothing silently loses its description."""
    real = shapes._read

    def only_the_intents(payload):
        # The geometry file still has to load, since that is what the intents are
        # checked against.
        return lambda name: payload if name == shapes._INTENT_FILE else real(name)

    monkeypatch.setattr(shapes, "_read", only_the_intents({"version": 2, "groups": []}))
    shapes._intents.cache_clear()
    try:
        with pytest.raises(ShapeDataError, match="version 1 and carry groups"):
            shapes._intents()
        monkeypatch.setattr(
            shapes, "_read", only_the_intents({"version": 1, "groups": [{"presets": {"chevron": "A step."}}]})
        )
        shapes._intents.cache_clear()
        with pytest.raises(ShapeDataError, match="every preset needs an intent"):
            shapes._intents()
    finally:
        shapes._intents.cache_clear()


def test_a_missing_data_file_is_refused_at_load(monkeypatch) -> None:
    monkeypatch.setattr(shapes, "files", lambda _: Path("/nonexistent-package-dir"))
    shapes._load.cache_clear()
    try:
        with pytest.raises(ShapeDataError, match="cannot load packaged shape data"):
            shapes._load()
    finally:
        shapes._load.cache_clear()
        shapes._by_key.cache_clear()
        shapes._intents.cache_clear()


def test_an_angle_is_named_as_one_because_the_field_it_shares_holds_two_units() -> None:
    """The standard writes proportions and angles into the same `a:gd`, in different units.

    A hundred-thousandth of the shape's width for `chevron`'s notch, a sixtieth-thousandth
    of a degree for `pie`'s sweep. Nothing in the field says which, so a helper that scales
    every adjustment alike sent a three-quarter turn in as 1.25 degrees and drew a hairline
    without a word. The mark that separates them is in the data: a guide that clamps the
    adjustment to 21599999 is clamping it to just under a full turn.
    """
    assert angle_adjustments("pie") == frozenset({"adj1", "adj2"})
    assert angle_adjustments("arc") == frozenset({"adj1", "adj2"})
    assert angle_adjustments("chord") == frozenset({"adj1", "adj2"})
    assert angle_adjustments("blockArc") == frozenset({"adj1", "adj2"})
    assert angle_adjustments("circularArrow") == frozenset({"adj3", "adj4"})
    # A proportion, and the shape this module is named for: not an angle.
    assert angle_adjustments("chevron") == frozenset()
    # `blockArc`'s third is its thickness, a proportion sitting beside two angles.
    assert "adj3" not in angle_adjustments("blockArc")


def test_an_adjustment_this_module_cannot_name_is_refused_rather_than_scaled() -> None:
    """Four are angles by size and carry no mark saying so.

    19 degrees on the circular arrows' tail, 110 on `mathNotEqual`'s slash. Scaling them
    as proportions is what a caller gets in silence, and the whole point of naming the
    other fourteen is that silence is the failure.
    """
    assert unscalable_adjustments("mathNotEqual") == frozenset({"adj2"})
    assert unscalable_adjustments("circularArrow") == frozenset({"adj2"})
    assert unscalable_adjustments("pie") == frozenset()
    assert unscalable_adjustments("chevron") == frozenset()
    # A callout's tail reaches outside its own box and a pentagon stretches past square,
    # so a default over one whole is not on its own an angle.
    assert unscalable_adjustments("callout1") == frozenset()
    assert unscalable_adjustments("pentagon") == frozenset()
