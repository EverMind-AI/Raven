"""The packaged outline icons, and answering for a name that is not one.

The geometry is a data file, not a Python literal. Its predecessor was a
4197-line module holding one dict, which meant every icon change showed up as a
source diff, the import cost parsing all of it, and the licence scope pointed at
a ``.py`` file that was really third-party data. ``data/tabler_outline.json``
carries its own provenance and licence pointer; this module is the loader and
the lookup.

Lookup is deliberately forgiving about spelling and deliberately loud about a
miss. Upstream publishes these as ``map-pin`` and they are stored as
``map_pin``, so an author writing what it knows would otherwise be punished for
a separator. And a name outside the set entirely gets the nearest few back:
measured behaviour on the script route was an author trying ``category``,
``click``, ``arrows-move`` and ``switch-horizontal``, none of which exist, then
writing a script to bulk-replace them -- work that a list of candidates in the
error would have saved. An error that only says "no" costs a round; an error
that says "did you mean" costs nothing.

All four of those names now resolve, because the set they missed was a curated
180 out of an upstream five thousand. It is a curated thirteen hundred now, and
the wider set changed what a miss needs. A name is a poor index into thirteen
hundred names -- ``kpi`` is nobody's filename -- so each icon also carries the
upstream ``tags`` and ``category`` header as ``keywords``, and the search reads
them. That is what lets ``deadline`` reach ``calendar_due`` and ``inventory``
reach ``building_warehouse``, neither of which shares a word with what was asked
for.

Loader, lookup, and one measurement: ``icon_ink`` says how much of its square an
icon's strokes actually cover. That is geometry the data has always carried and
nobody could read -- see the function for what the spread turned out to be.
"""

from __future__ import annotations

import difflib
import json
import math
from collections.abc import Mapping, Sequence
from functools import lru_cache
from importlib.resources import files
from types import MappingProxyType
from typing import NamedTuple

# Icons are strokes on this grid, in the vendor's editable M/L/C/Z path grammar.
# Nothing here is in physical units: a consumer scales the grid to whatever box
# it has.
ICON_GRID = 24.0

# How finely a curve is flattened into the straight segments a consumer draws.
# Eight, because that is what the projected ``ppt_icons`` strokes, and a
# measurement of the ink has to measure the polyline that lands on the page rather
# than the ideal curve behind it.
_CURVE_STEPS = 8

_DATA_PACKAGE = "raven.ppt.services.assets"
_DATA_FILE = "data/tabler_outline.json"

# Command name -> how many coordinates it carries.
_COMMAND_ARITY = {"M": 2, "L": 2, "C": 6, "Z": 0}

# One path is a run of commands; one icon is a list of paths.
IconPath = tuple[tuple[str, tuple[float, ...]], ...]

# One icon's searchable words: its upstream category and tags, minus whatever its
# own name already spells.
IconKeywords = frozenset[str]


class IconDataError(RuntimeError):
    """The packaged icon data is missing, unparsable or malformed."""


def icon_miss_hint(name: str, candidates: Sequence[str], total: int) -> str:
    """What to say after an icon name that is not in the set.

    One text for both sides of the fence. The author's script raises this from its
    own copy of the resolver in the build directory and the service raises it here,
    and the same miss reading two different ways is how one of them goes stale.

    Candidates are named as candidates and not as the answer: the search ranks by
    name, then by the upstream keywords, then by spelling, so its last resort is a
    string that looks like what was typed and means nothing like it -- `trophy` came
    back as `typography`, on a set that does hold `award`. And the miss names the
    search, because that is where the author is standing when it needs it: one live
    run spent two build rounds guessing icon names, with `find_icons` named four
    times in the skill it had loaded and never once called.

    And it names the file, because that is the lookup that costs nothing. Every name
    is a key in `icons.json`, written into the build directory on every build, so a
    shell answers "is this a name" without a build round -- while this raise ends the
    script, leaving every page after it unwritten. A second run guessed `wave-sine`
    and spent a round on it with that file already on disk beside the script.
    """
    if candidates:
        return (
            "; nearest: " + ", ".join(candidates) + f". Ranked by name, then by the upstream keywords, "
            f"then by spelling -- one that means nothing like {name!r} is a spelling match and not an "
            "answer. This raise ended the script, so the pages after it went unwritten. Every name is a "
            "key in deck/build/icons.json -- grep it from a shell to check a name for nothing -- and "
            "find_icons searches meanings ('deadline' finds calendar_due), or read "
            "deck/build/references/icons.md"
        )
    return (
        "; nothing close -- find_icons takes a meaning rather than a name, so try two or three words "
        f"for the thing itself; all {total} names are in ICON_NAMES and deck/build/references/icons.md"
    )


class UnknownIconError(LookupError):
    """A name is not in the set, with the nearest candidates attached.

    ``LookupError`` rather than ``KeyError`` because ``KeyError``'s ``str()``
    wraps the whole message in quotes, and the message here is a sentence the
    author is meant to read and act on.
    """

    def __init__(self, name: str, candidates: list[str], total: int):
        self.name = name
        self.candidates = candidates
        super().__init__(f"unknown icon {name!r}{icon_miss_hint(name, candidates, total)}")


def _validate_paths(name: str, raw: object) -> tuple[IconPath, ...]:
    if not isinstance(raw, list) or not raw:
        raise IconDataError(f"icon {name!r} must carry at least one path")
    paths: list[IconPath] = []
    for entry in raw:
        if not isinstance(entry, list) or len(entry) != 2 or entry[0] != "path":
            raise IconDataError(f"icon {name!r} has an entry that is not a ['path', commands] pair")
        commands: list[tuple[str, tuple[float, ...]]] = []
        for command in entry[1]:
            if not isinstance(command, list) or len(command) != 2:
                raise IconDataError(f"icon {name!r} has a command that is not an [op, coords] pair")
            op, coords = command
            arity = _COMMAND_ARITY.get(op)
            if arity is None:
                raise IconDataError(f"icon {name!r} uses unsupported path command {op!r}")
            if not isinstance(coords, list) or len(coords) != arity:
                raise IconDataError(f"icon {name!r} command {op!r} needs {arity} coordinates")
            values = tuple(float(value) for value in coords)
            if not all(math.isfinite(value) for value in values):
                raise IconDataError(f"icon {name!r} command {op!r} has a non-finite coordinate")
            commands.append((op, values))
        if not commands:
            raise IconDataError(f"icon {name!r} has an empty path")
        paths.append(tuple(commands))
    return tuple(paths)


def _validate_keywords(names: Mapping[str, object], raw: object) -> Mapping[str, IconKeywords]:
    """Every icon's searchable words, refusing a set that does not cover them all.

    Covering every name is the invariant worth holding: an icon with no keywords
    is reachable only by spelling its name, which is the failure the keywords
    exist to fix, and it fails silently -- the search simply never returns it.
    """
    if not isinstance(raw, dict):
        raise IconDataError("icon data must carry a keywords object")
    parsed: dict[str, IconKeywords] = {}
    for name, value in raw.items():
        if name not in names:
            raise IconDataError(f"keywords name {name!r} is not an icon")
        if not isinstance(value, str):
            raise IconDataError(f"keywords for {name!r} must be a string")
        parsed[name] = frozenset(word for word in value.split() if word)
    missing = sorted(set(names) - set(parsed))
    if missing:
        raise IconDataError(f"{len(missing)} icons carry no keywords, first {missing[0]!r}")
    return MappingProxyType(parsed)


def _validate_categories(names: Mapping[str, object], raw: object) -> Mapping[str, str]:
    """The upstream shelf each icon is filed under, over the whole set."""
    if not isinstance(raw, dict):
        raise IconDataError("icon data must carry a categories object")
    parsed: dict[str, str] = {}
    for name, value in raw.items():
        if name not in names:
            raise IconDataError(f"category name {name!r} is not an icon")
        if not isinstance(value, str) or not value:
            raise IconDataError(f"category for {name!r} must be a non-empty string")
        parsed[name] = value
    missing = sorted(set(names) - set(parsed))
    if missing:
        raise IconDataError(f"{len(missing)} icons carry no category, first {missing[0]!r}")
    return MappingProxyType(parsed)


@lru_cache(maxsize=1)
def _load() -> tuple[
    Mapping[str, tuple[IconPath, ...]],
    Mapping[str, object],
    Mapping[str, list],
    Mapping[str, IconKeywords],
    Mapping[str, str],
]:
    try:
        payload = json.loads(files(_DATA_PACKAGE).joinpath(_DATA_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IconDataError(f"cannot load packaged icon data: {exc}") from exc
    if payload.get("version") != 1:
        raise IconDataError("icon data must declare version 1")
    if payload.get("grid") != int(ICON_GRID):
        raise IconDataError(f"icon data grid must be {int(ICON_GRID)}, got {payload.get('grid')!r}")
    raw_icons = payload.get("icons")
    if not isinstance(raw_icons, dict) or not raw_icons:
        raise IconDataError("icon data must carry a non-empty icons object")
    parsed = {name: _validate_paths(name, raw) for name, raw in sorted(raw_icons.items())}
    keywords = _validate_keywords(parsed, payload.get("keywords"))
    categories = _validate_categories(parsed, payload.get("categories"))
    header = {key: value for key, value in payload.items() if key not in {"icons", "keywords", "categories"}}
    # The raw form is kept alongside the parsed one because it is what travels
    # into a build directory verbatim; re-serializing the parsed tuples would
    # produce the same bytes only by accident.
    return (
        MappingProxyType(parsed),
        MappingProxyType(header),
        MappingProxyType(dict(sorted(raw_icons.items()))),
        keywords,
        categories,
    )


def icon_names() -> tuple[str, ...]:
    """Every icon name, sorted. Load errors surface here rather than at import."""
    return tuple(_load()[0])


def icon_provenance() -> Mapping[str, object]:
    """Source, upstream version and licence pointer, straight from the data file."""
    return _load()[1]


def icon_keywords() -> Mapping[str, IconKeywords]:
    """Each icon's upstream category and tags, as the words a search can match."""
    return _load()[3]


def icon_categories() -> Mapping[str, str]:
    """The upstream shelf each icon sits on, for a caller that has to group them."""
    return _load()[4]


def keyword_catalog() -> Mapping[str, str]:
    """The same words as flat strings, for injecting into a build directory."""
    return MappingProxyType({name: " ".join(sorted(words)) for name, words in _load()[3].items()})


def catalog_json() -> str:
    """The geometry as JSON text, for injecting into a build directory.

    Text rather than a dict because that is what the caller writes: handing back
    the parsed structure would mean a deep copy to keep the cached one safe, and
    a re-serialization to get here anyway.
    """
    return json.dumps(dict(_load()[2]), separators=(",", ":"))


def _normalize(name: str) -> str:
    return str(name).strip().lower().replace("-", "_").replace(" ", "_").replace(".", "_")


def _contained(key: str, names: tuple[str, ...]) -> list[str]:
    """Names that are most of the asked-for word, or of which it is most.

    A bare substring test was fine over 180 names and is noise over thirteen
    hundred: ``ad``, ``at``, ``id`` and ``line`` are all inside ``deadline`` and
    none of them is what was meant. Requiring the shorter of the two to be most
    of the longer keeps the case this pass exists for -- ``chart_bars`` reaching
    ``chart_bar`` -- and drops the accidents.
    """
    found = []
    for candidate in names:
        if key not in candidate and candidate not in key:
            continue
        overlap = min(len(key), len(candidate)) / max(len(key), len(candidate))
        if overlap >= 0.6:
            found.append((-overlap, len(candidate), candidate))
    return [candidate for _, _, candidate in sorted(found)]


def icon_candidates(name: str, limit: int = 8) -> list[str]:
    """The nearest names to a miss, best guess first.

    Four passes, because the ways a name misses are different. A shared word
    (``task-check`` -> ``check``) is the strongest signal and comes first. A
    containment (``chart_bars`` -> ``chart_bar``) is next. A shared word in the
    *keywords* is third, and it is the pass that earns its keep now the set is
    thirteen hundred: ``deadline`` is in no name and in exactly one tag header,
    ``calendar_due``'s. Character similarity (``clik`` -> ``click``) catches the
    typos the first three cannot, and runs last because it is also the loosest.

    Inside a pass the order is by how many of the asked-for words matched, then by
    the shorter name. That mattered less at 180 names; at thirteen hundred, a word
    like ``file`` is a whole word in forty of them, and alphabetical order would
    spend the whole budget on ``file_arrow_left`` before reaching ``file``.
    """
    key = _normalize(name)
    if not key:
        return []
    names = icon_names()
    keywords = icon_keywords()
    tokens = [part for part in key.split("_") if part]
    ranked: list[str] = []

    def add(candidate: str) -> None:
        if candidate not in ranked:
            ranked.append(candidate)

    def by_hits(scored: list[tuple[int, str]]) -> list[str]:
        ordered = sorted(((-hits, len(item), item) for hits, item in scored if hits))
        return [item for _, _, item in ordered]

    for candidate in by_hits([(sum(t in c.split("_") for t in tokens), c) for c in names]):
        add(candidate)
    for candidate in _contained(key, names):
        add(candidate)
    for candidate in by_hits([(sum(t in keywords[c] for t in tokens), c) for c in names]):
        add(candidate)
    for candidate in difflib.get_close_matches(key, names, n=limit, cutoff=0.62):
        add(candidate)
    for token in tokens:
        for candidate in difflib.get_close_matches(token, names, n=3, cutoff=0.7):
            add(candidate)
    return ranked[:limit]


def resolve_icon_name(name: str) -> str:
    """The stored name for what an author wrote, or a miss naming the near ones."""
    key = _normalize(name)
    icons = _load()[0]
    if key in icons:
        return key
    raise UnknownIconError(str(name), icon_candidates(key), len(icons))


def icon_paths(name: str) -> tuple[IconPath, ...]:
    """One icon's paths on the 24x24 grid, in M/L/C/Z commands."""
    return _load()[0][resolve_icon_name(name)]


class IconInk(NamedTuple):
    """The rectangle one icon's strokes cover, as fractions of the square it is given.

    Fractions rather than a size, because nothing in this package is physical: the
    four numbers hold at every side length the icon is drawn at, and a consumer that
    has inches multiplies by the side it is about to use.
    """

    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.y1 - self.y0


def _cubic(start: tuple[float, float], one: tuple[float, float], two: tuple[float, float], end: tuple[float, float]):
    """``_CURVE_STEPS`` points along one cubic, the last of them its endpoint."""
    points = []
    for step in range(1, _CURVE_STEPS + 1):
        t = step / _CURVE_STEPS
        u = 1 - t
        points.append(
            (
                u * u * u * start[0] + 3 * u * u * t * one[0] + 3 * u * t * t * two[0] + t * t * t * end[0],
                u * u * u * start[1] + 3 * u * u * t * one[1] + 3 * u * t * t * two[1] + t * t * t * end[1],
            )
        )
    return points


def _polylines(paths: tuple[IconPath, ...]) -> list[list[tuple[float, float]]]:
    """One icon's paths as the runs a consumer will stroke, curves flattened.

    Deliberately the same walk the projected module does, down to the two rules that
    decide what is ink: a run of one point strokes nothing -- ``marquee`` and
    ``new_section`` each carry a stray ``M`` -- and a curve contributes the points it
    is sampled at rather than its control points, which sit outside the ink they bend.
    """
    runs: list[list[tuple[float, float]]] = []
    for path in paths:
        current: tuple[float, float] | None = None
        run: list[tuple[float, float]] = []
        for op, coords in path:
            if op == "M":
                if len(run) > 1:
                    runs.append(run)
                current = (coords[0], coords[1])
                run = [current]
            elif op == "L":
                current = (coords[0], coords[1])
                run.append(current)
            elif op == "C" and current is not None:
                end = (coords[4], coords[5])
                run.extend(_cubic(current, (coords[0], coords[1]), (coords[2], coords[3]), end))
                current = end
            elif op == "Z" and run:
                run.append(run[0])
        if len(run) > 1:
            runs.append(run)
    return runs


@lru_cache(maxsize=None)
def icon_ink(name: str) -> IconInk:
    """How much of its square this icon's strokes cover, and where in it they sit.

    The measurement the data always held and no consumer could read. As a share of
    the box the icon is drawn in: ``target``, ``clock`` and ``circle`` cover
    75% x 75%, ``chart_bar`` 75% x 67%, ``check`` 62% x 42%, and ``minus``
    58% x 0% -- a rule through the middle, with no height at all. Over the whole set
    504 of the 1304 icons are 75% wide, 151 are half their box or less tall, and
    ``json`` and ``html`` reach 92%. So the square handed to a draw call says almost
    nothing about where the mark inside it will appear, and lining an icon up with a
    baseline or centring it in a card was work that could only be done against a
    render.

    The box is the strokes' centrelines, which is exactly what a stroked polyline's
    own extent is: the pen straddles the path, so a pen ``w`` wide paints ``w/2``
    outside every edge and moves no centre. ``minus`` is honestly 0 high, and
    centring on it puts the rule where a reader sees it.

    Upstream's dots -- the stub under an exclamation mark, the beads of ``braille``
    -- count at their centres, which is where a consumer centres the pen-wide square
    it paints for them. So wherever one of those stubs sits near an edge, the paint
    reaches up to half a pen past what this reports, and never further.
    """
    points = [point for run in _polylines(icon_paths(name)) for point in run]
    if not points:
        raise IconDataError(f"icon {name!r} carries no strokes to measure")
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    return IconInk(min(xs) / ICON_GRID, min(ys) / ICON_GRID, max(xs) / ICON_GRID, max(ys) / ICON_GRID)
