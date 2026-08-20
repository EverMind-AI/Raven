"""The icon set as a data file: it loads, it is complete, and a miss answers.

The geometry moved out of a 4197-line Python literal into JSON, which makes two
things worth testing that were not before. The file has to actually load and
validate as part of a normal import path, since a packaging mistake now shows up
as a missing asset rather than a missing module. And every icon has to still be
there: 180 was the count before the move, so the count is the round-trip check.

The rest is about lookup. An author on the script route does not get a schema
enum, so the only thing standing between it and an unusable icon field is what
happens when a name misses.
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
    icon_names,
    icon_paths,
    icon_provenance,
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
    assert provenance["upstream"] == {"package": "tabler-icons", "version": "3.46.0", "variant": "outline"}
    assert provenance["license_file"] == "LICENSES/MIT-tabler-icons.txt"
    assert sorted(provenance["commands"]) == ["C", "L", "M", "Z"]


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


def test_the_whole_curated_set_survived_the_move_to_a_data_file() -> None:
    names = icon_names()
    assert len(names) == 180
    assert len(set(names)) == len(names)
    assert list(names) == sorted(names)
    assert FOUNDATIONAL_ICON_NAMES <= set(names)


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
    ("attempt", "expected"),
    [
        # Names an author actually tried on the script route, none of which exist.
        ("arrows-move", "arrows_exchange"),
        ("click", "clock"),
        ("task-check", "check"),
        # A plural or a near-spelling of a real name.
        ("chart_bars", "chart_bar"),
        ("users-groups", "users_group"),
        ("light_bulb", "lightbulb"),
    ],
)
def test_a_miss_names_the_icon_that_was_probably_meant(attempt: str, expected: str) -> None:
    with pytest.raises(UnknownIconError) as caught:
        resolve_icon_name(attempt)
    assert expected in caught.value.candidates
    assert expected in str(caught.value)
    assert caught.value.name == attempt


def test_a_miss_with_nothing_close_still_says_where_to_look() -> None:
    """`switch-horizontal` has no near neighbour, and pretending otherwise is
    worse than saying so -- but the message still has to end somewhere useful."""
    with pytest.raises(UnknownIconError, match="ICON_NAMES") as caught:
        resolve_icon_name("switch-horizontal")
    assert caught.value.candidates == []
    assert "180" in str(caught.value)


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
