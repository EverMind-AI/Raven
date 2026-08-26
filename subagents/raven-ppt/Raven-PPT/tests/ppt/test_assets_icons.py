"""The icon set as a data file: it loads, it is complete, and a miss answers.

The geometry moved out of a 4197-line Python literal into JSON, which makes two
things worth testing that were not before. The file has to actually load and
validate as part of a normal import path, since a packaging mistake now shows up
as a missing asset rather than a missing module. And nothing that was there may
go: the count is a floor with a whitelist under it, because the set grows and an
icon that quietly stops resolving breaks a deck that already used it.

The rest is about lookup. An author on the script route does not get a schema
enum, so the only thing standing between it and an unusable icon field is what
happens when a name misses -- and at thirteen hundred names, what happens when
the word it wants is in no name at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.ppt.services.assets import icons
from raven.ppt.services.assets.icons import (
    ICON_GRID,
    UnknownIconError,
    catalog_json,
    icon_candidates,
    icon_ink,
    icon_keywords,
    icon_names,
    icon_paths,
    icon_provenance,
    keyword_catalog,
    resolve_icon_name,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# The set the schema route exposed by name, so a data edit that quietly drops a
# staple is caught rather than discovered by a deck that stops drawing.
FOUNDATIONAL_ICON_NAMES = {
    "target",
    "lightbulb",
    "layers",
    "database",
    "server",
    "cloud",
    "shield",
    "lock",
    "users",
    "globe",
    "search",
    "trend_up",
    "cpu",
    "workflow",
    "rocket",
    "check",
    "warning",
    "clock",
    "calendar",
    "code",
    "document",
    "image",
    "eye",
    "settings",
    "check_circle",
    "x_circle",
    "info",
    "arrow_right",
    "arrow_up",
    "download",
    "upload",
    "link",
    "folder",
    "mail",
    "monitor",
    "terminal",
    "filter",
    "chart",
    "table",
    "sparkles",
    "bell",
    "map_pin",
    "play",
    "refresh",
}


def test_the_data_file_loads_and_carries_its_provenance() -> None:
    provenance = icon_provenance()
    assert provenance["version"] == 1
    assert provenance["grid"] == int(ICON_GRID)
    assert provenance["license_file"] == "LICENSES/MIT-tabler-icons.txt"
    assert sorted(provenance["commands"]) == ["C", "L", "M", "Z"]
    assert provenance["upstream"]["package"] == "tabler-icons"
    assert provenance["upstream"]["version"] == "3.46.0"
    assert provenance["upstream"]["variant"] == "outline"


def test_the_pin_names_a_commit_and_an_artifact_anybody_can_check() -> None:
    """A version string on its own is a claim; this is the part that can be falsified.

    `3.46.0` used to be the whole record, and a release name is something a human
    typed: nothing in the repository could tell it apart from a guess. The geometry
    beside it in `preset_shapes.json` had carried a commit and a hash from the start,
    so the icons were the one third-party import whose story could not be checked.

    What makes the pin checkable is that the archive is named as a URL and the hash is
    of that archive as served. `curl -sL <archive> | sha256sum` reproduces it, and
    `build_tabler_icons.py --upstream <that archive>` hashes what it is handed and
    refuses to convert a tree the pin does not name -- so the pin stays true across a
    regeneration rather than only at the moment it was written.
    """
    upstream = icon_provenance()["upstream"]
    commit = upstream["commit"]
    assert commit == "8ac7d81b72ece11072ef25ea9fd92e80c6f3c9fc"
    assert len(commit) == 40 and set(commit) <= set("0123456789abcdef"), commit
    assert upstream["sha256"] == "6d7ecda12c53a543f305859c247b8beb4266205e40c58487b8469aeaeb7797b1"
    assert len(upstream["sha256"]) == 64
    # The archive has to be the one the commit names, or the hash is of something else.
    assert upstream["archive"] == f"https://codeload.github.com/tabler/tabler-icons/tar.gz/{commit}"
    # And the pin has to say which part of that archive was taken, since it holds the
    # filled set and a website too and neither is in here.
    assert upstream["files"] == "icons/outline/*.svg"


def test_the_licence_travelled_with_the_data() -> None:
    """Third-party data is only shippable with its licence and its notice."""
    license_text = (REPO_ROOT / str(icon_provenance()["license_file"])).read_text(encoding="utf-8")
    notices = (REPO_ROOT / "NOTICES.md").read_text(encoding="utf-8")

    assert "MIT License" in license_text
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in license_text
    assert "Tabler Icons" in notices
    assert "LICENSES/MIT-tabler-icons.txt" in notices
    # The notice has to point at where the data actually lives now.
    assert "raven/ppt/services/assets/data/tabler_outline.json" in notices
    # And at the same commit and hash the data file carries, so a reader who checks
    # one of the two is not being told a different story by the other.
    assert str(icon_provenance()["upstream"]["commit"]) in notices
    assert str(icon_provenance()["upstream"]["sha256"]) in notices


def test_the_copyright_line_is_spelled_the_way_the_licence_spells_it() -> None:
    """MIT asks for the notice to be retained, and a name is not retained if it is respelled.

    The data file had folded the barred l of the holder's given name to a plain ASCII
    l, while the licence beside it, upstream's own file and `NOTICES.md` had all kept
    it. That is the kind of change that happens on the way through a terminal and is
    never noticed, because every reader still knows who is meant -- which is exactly
    why nothing but a test catches it. The expected line is read out of the licence
    rather than written here again, so the repository holds one spelling and not two.
    """
    licence = (REPO_ROOT / str(icon_provenance()["license_file"])).read_text(encoding="utf-8")
    holder = next(line for line in licence.splitlines() if line.startswith("Copyright"))
    notices = [str(line) for line in icon_provenance()["notices"]]
    assert holder in notices, (holder, notices)
    assert holder in (REPO_ROOT / "NOTICES.md").read_text(encoding="utf-8")


def test_the_whole_curated_set_survived_the_move_to_a_data_file() -> None:
    names = icon_names()
    assert len(names) == 1304
    assert len(set(names)) == len(names)
    assert list(names) == sorted(names)
    assert FOUNDATIONAL_ICON_NAMES <= set(names)


def test_every_icon_carries_words_to_be_found_by() -> None:
    """A name is a poor index into thirteen hundred names.

    `kpi` is nobody's filename, so the keywords are what a search has to work
    with -- and an icon that has none is unreachable by anything except its own
    spelling, silently.
    """
    keywords = icon_keywords()
    assert set(keywords) == set(icon_names())
    assert all(words for words in keywords.values())
    # The upstream category is in there, so a whole shelf can be asked for at once.
    assert "charts" in keywords["chart_bar"]
    assert "buildings" in keywords["building_factory"]
    # And the sixteen names that are ours rather than upstream's are covered too.
    assert "idea" in keywords["lightbulb"]
    assert "risk" in keywords["warning"]


@pytest.mark.parametrize("name", icon_names())
def test_every_icon_is_strokes_on_the_unit_grid_and_nothing_else(name: str) -> None:
    """No physical units, no external references, no text.

    An icon that reached for a font or a URL would stop being a vector the export
    can keep editable, and a coordinate outside the grid would land outside
    whatever box a consumer scales it into.
    """
    paths = icon_paths(name)
    assert paths
    for path in paths:
        assert path
        assert path[0][0] == "M", f"{name} has a path that does not start with a move"
        for op, coords in path:
            assert op in {"M", "L", "C", "Z"}
            assert len(coords) == {"M": 2, "L": 2, "C": 6, "Z": 0}[op]
            assert all(0.0 <= value <= ICON_GRID for value in coords), f"{name} leaves the grid: {coords}"


def test_paths_are_immutable_so_one_consumer_cannot_edit_another_s_icon() -> None:
    paths = icon_paths("target")
    assert isinstance(paths, tuple)
    assert all(isinstance(path, tuple) for path in paths)
    assert icon_paths("target") is paths


def test_lookup_accepts_the_upstream_spelling() -> None:
    """Tabler publishes `map-pin`; the data stores `map_pin`.

    Refusing over the separator punishes an author for writing what it knows.
    """
    assert resolve_icon_name("map_pin") == "map_pin"
    assert resolve_icon_name("map-pin") == "map_pin"
    assert resolve_icon_name("Map Pin") == "map_pin"
    assert resolve_icon_name("  TREND-UP  ") == "trend_up"
    assert icon_paths("map-pin") == icon_paths("map_pin")


@pytest.mark.parametrize(
    "attempt",
    # The four names a live author tried on the script route and gave up on, then
    # wrote a script to bulk-replace. All four exist now: the set they missed was
    # 180 of an upstream five thousand, and the cheapest fix for a miss is for the
    # name not to miss.
    ["category", "click", "arrows-move", "switch-horizontal"],
)
def test_the_names_a_live_author_gave_up_on_now_resolve(attempt: str) -> None:
    assert icon_paths(attempt)


@pytest.mark.parametrize(
    ("attempt", "expected"),
    [
        # A plural or a near-spelling of a real name.
        ("task-check", "check"),
        ("chart_bars", "chart_bar"),
        ("users-groups", "users_group"),
        ("light_bulb", "lightbulb"),
        # A word that is in no name at all, and is in the keywords of the icon that
        # draws it. This is the pass the tag header bought.
        ("deadline", "calendar_due"),
        ("risk", "warning"),
        ("funnel", "chart_funnel"),
        ("warehouse", "building_warehouse"),
    ],
)
def test_a_miss_names_the_icon_that_was_probably_meant(attempt: str, expected: str) -> None:
    with pytest.raises(UnknownIconError) as caught:
        resolve_icon_name(attempt)
    assert expected in caught.value.candidates
    assert expected in str(caught.value)
    assert caught.value.name == attempt


def test_a_miss_with_nothing_close_still_says_where_to_look() -> None:
    """`throughput` has no near neighbour and is in nobody's tags, and pretending
    otherwise is worse than saying so -- but the message still has to end
    somewhere useful."""
    with pytest.raises(UnknownIconError, match="ICON_NAMES") as caught:
        resolve_icon_name("throughput")
    assert caught.value.candidates == []
    assert "1304" in str(caught.value)


def test_a_short_name_inside_a_long_word_is_not_a_candidate() -> None:
    """`ad`, `at`, `id` and `line` are all inside `deadline`.

    A bare substring test was fine over 180 names and is noise over thirteen
    hundred, where it spends the whole eight-name budget before the search that
    knows what a deadline is gets a turn.
    """
    candidates = icon_candidates("deadline")
    assert candidates[0] == "calendar_due"
    assert "ad" not in candidates


def test_candidates_are_never_junk_and_never_the_whole_set() -> None:
    names = set(icon_names())
    for attempt in ("category", "wand", "sparkle", "chart", "zzzzzzzz", ""):
        candidates = icon_candidates(attempt)
        assert len(candidates) <= 8
        assert len(set(candidates)) == len(candidates)
        assert set(candidates) <= names


def test_a_miss_is_a_lookup_error_not_a_key_error() -> None:
    """KeyError's str() quotes the whole message, and this message is a sentence
    the author is meant to read."""
    with pytest.raises(LookupError) as caught:
        resolve_icon_name("definitely-not-an-icon")
    assert not isinstance(caught.value, KeyError)
    assert str(caught.value).startswith("unknown icon 'definitely-not-an-icon'")


def test_the_keyword_catalog_is_the_same_words_as_flat_text() -> None:
    """What travels into a build directory has to search the same as the service."""
    catalog = keyword_catalog()
    assert set(catalog) == set(icon_names())
    for name, words in catalog.items():
        assert frozenset(words.split()) == icon_keywords()[name]


def test_catalog_json_round_trips_to_the_loaded_geometry() -> None:
    """What travels into a build directory has to be the same icons."""
    catalog = json.loads(catalog_json())
    assert set(catalog) == set(icon_names())
    for name, paths in catalog.items():
        rebuilt = tuple(tuple((op, tuple(float(v) for v in args)) for op, args in commands) for _, commands in paths)
        assert rebuilt == icon_paths(name)
        assert all(kind == "path" for kind, _ in paths)


def test_malformed_data_is_refused_rather_than_half_drawn() -> None:
    with pytest.raises(icons.IconDataError, match="unsupported path command"):
        icons._validate_paths("bogus", [["path", [["Q", [1, 2]]]]])
    with pytest.raises(icons.IconDataError, match="needs 2 coordinates"):
        icons._validate_paths("bogus", [["path", [["M", [1]]]]])
    with pytest.raises(icons.IconDataError, match="non-finite"):
        icons._validate_paths("bogus", [["path", [["M", [float("nan"), 2]]]]])
    with pytest.raises(icons.IconDataError, match="at least one path"):
        icons._validate_paths("bogus", [])
    with pytest.raises(icons.IconDataError, match="not a \\['path', commands\\] pair"):
        icons._validate_paths("bogus", [["circle", []]])


def test_keywords_that_do_not_cover_the_set_are_refused() -> None:
    """An icon with no keywords fails silently -- it is simply never returned."""
    with pytest.raises(icons.IconDataError, match="must carry a keywords object"):
        icons._validate_keywords({"target": ()}, None)
    with pytest.raises(icons.IconDataError, match="is not an icon"):
        icons._validate_keywords({"target": ()}, {"nope": "words"})
    with pytest.raises(icons.IconDataError, match="must be a string"):
        icons._validate_keywords({"target": ()}, {"target": ["words"]})
    with pytest.raises(icons.IconDataError, match="carry no keywords"):
        icons._validate_keywords({"target": (), "clock": ()}, {"target": "goal"})


def test_categories_that_do_not_cover_the_set_are_refused() -> None:
    """The shelf is what a caller groups by, so a gap in it is a gap in the list."""
    with pytest.raises(icons.IconDataError, match="must carry a categories object"):
        icons._validate_categories({"target": ()}, None)
    with pytest.raises(icons.IconDataError, match="is not an icon"):
        icons._validate_categories({"target": ()}, {"nope": "System"})
    with pytest.raises(icons.IconDataError, match="must be a non-empty string"):
        icons._validate_categories({"target": ()}, {"target": ""})
    with pytest.raises(icons.IconDataError, match="carry no category"):
        icons._validate_categories({"target": (), "clock": ()}, {"target": "System"})


# What the ink actually measured, as a share of the square the icon is given. The
# table `icon_ink` was written for: same box, six different marks in it.
MEASURED_INK = {
    "target": (0.125, 0.125, 0.875, 0.875),
    "clock": (0.125, 0.125, 0.875, 0.875),
    "circle": (0.125, 0.125, 0.875, 0.875),
    "chart_bar": (0.125, 1 / 6, 0.875, 5 / 6),
    "check": (5 / 24, 7 / 24, 20 / 24, 17 / 24),
    "minus": (5 / 24, 0.5, 19 / 24, 0.5),
}


@pytest.mark.parametrize("name", sorted(MEASURED_INK))
def test_an_icon_says_how_much_of_its_square_its_ink_covers(name: str) -> None:
    """The spread that makes the measurement worth having.

    Three icons fill three quarters of the box, one is two thirds tall, one is 42%
    tall, and `minus` is a rule with no height at all -- so the square an author asks
    for says almost nothing about where the mark inside it lands. Written out rather
    than derived, because a derived expectation would agree with any answer the code
    gave, including a wrong one.
    """
    assert icon_ink(name) == pytest.approx(MEASURED_INK[name], abs=1e-12)


def test_the_ink_box_carries_its_own_width_and_height() -> None:
    """`w` and `h` are what a consumer aligns with, and `minus` is why `h` can be 0."""
    assert icon_ink("check").w == pytest.approx(0.625, abs=1e-12)
    assert icon_ink("check").h == pytest.approx(0.41666, abs=1e-4)
    assert icon_ink("minus").h == 0.0
    assert icon_ink("exclamation_mark").w == 0.0


def test_every_icon_s_ink_stays_inside_the_square_it_is_given() -> None:
    """A box reporting ink outside the square would misplace every icon that used it.

    One test over the whole set rather than a case per icon: the grid check next door
    is already parametrized over all thirteen hundred names, and what this adds is
    about the measurement rather than about any one icon -- that it is ordered, and
    that it is a fraction of the box and not grid units.
    """
    for name in icon_names():
        ink = icon_ink(name)
        assert 0.0 <= ink.x0 <= ink.x1 <= 1.0, (name, ink)
        assert 0.0 <= ink.y0 <= ink.y1 <= 1.0, (name, ink)
        assert ink.w >= 0.0 and ink.h >= 0.0, (name, ink)


def _raw_span(name: str) -> tuple[float, float, float, float]:
    """The box over an icon's numbers as written, control points and all."""
    xs = [value for path in icon_paths(name) for _, coords in path for value in coords[0::2]]
    ys = [value for path in icon_paths(name) for _, coords in path for value in coords[1::2]]
    return min(xs) / ICON_GRID, min(ys) / ICON_GRID, max(xs) / ICON_GRID, max(ys) / ICON_GRID


def test_the_ink_of_a_curve_is_where_it_bends_and_not_where_its_controls_are() -> None:
    """A bezier's control points stand off the curve they pull.

    So a box read straight off the coordinates claims ink the icon does not cover:
    `shield_check`'s numbers run from 0.069 to 0.882 across the grid and its strokes
    run 0.127 to 0.875. Six hundredths of the box on one side -- the difference
    between an icon flush with a title and one that reads as indented.

    The property under the example is the one that has to hold everywhere: flattening
    a curve can only land inside the hull its controls describe, so no icon's ink may
    fall outside its own coordinates.
    """
    raw = _raw_span("shield_check")
    ink = icon_ink("shield_check")
    assert (raw[0], raw[2]) == pytest.approx((0.0688, 0.8824), abs=5e-4)
    assert (ink.x0, ink.x1) == pytest.approx((0.1273, 0.875), abs=5e-4)

    for name in icon_names():
        raw = _raw_span(name)
        ink = icon_ink(name)
        assert raw[0] <= ink.x0 + 1e-12 and ink.x1 <= raw[2] + 1e-12, name
        assert raw[1] <= ink.y0 + 1e-12 and ink.y1 <= raw[3] + 1e-12, name


def test_a_stray_move_that_strokes_nothing_is_not_ink(monkeypatch) -> None:
    """`marquee` and `new_section` each end a path on an `M` with nothing after it.

    A consumer strokes nothing for a run of one point -- there is no line there -- so
    the measurement cannot count it either. On those two the stray move happens to
    land inside the strokes, so the icons themselves cannot show the difference; the
    third case is the same shape with the move outside, where counting it would grow
    the box by a corner the page shows white at.
    """
    for name in ("marquee", "new_section"):
        assert [path for path in icon_paths(name) if path[-1][0] == "M"], f"{name} lost its trailing move"
    assert icons._polylines(((("M", (4.0, 6.0)),),)) == []
    assert icons._polylines(((("M", (4.0, 6.0)), ("L", (8.0, 6.0))),)) == [[(4.0, 6.0), (8.0, 6.0)]]

    monkeypatch.setattr(
        icons, "icon_paths", lambda name: ((("M", (6.0, 6.0)), ("L", (18.0, 6.0)), ("M", (0.0, 24.0))),)
    )
    assert icon_ink("a_rule_and_a_stray_move") == pytest.approx((0.25, 0.25, 0.75, 0.25), abs=1e-12)


def test_the_ink_is_asked_for_the_way_every_other_lookup_is() -> None:
    """One name, however it is spelled, and a miss that names the near ones.

    The measurement goes through `icon_paths`, so it inherits the forgiving spelling
    and the loud miss rather than growing a second lookup with its own rules.
    """
    assert icon_ink("map-pin") == icon_ink("map_pin")
    assert icon_ink("TREND UP") == icon_ink("trend_up")
    with pytest.raises(UnknownIconError, match="closest"):
        icon_ink("stakeholder")


def test_an_icon_with_nothing_to_stroke_is_refused_rather_than_measured(monkeypatch) -> None:
    """A data file whose icon is all stray moves has no ink, and no centre either.

    Reported as malformed data, because that is what it is: `min()` over no points
    raises where the caller cannot read it, and a box of zeros at the origin would
    quietly align things against the corner of the square.
    """
    monkeypatch.setattr(icons, "icon_paths", lambda name: ((("M", (4.0, 6.0)),),))
    with pytest.raises(icons.IconDataError, match="no strokes to measure"):
        icon_ink("all_moves_and_no_strokes")
