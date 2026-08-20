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
"""

from __future__ import annotations

import difflib
import json
import math
from collections.abc import Mapping
from functools import lru_cache
from importlib.resources import files
from types import MappingProxyType

# Icons are strokes on this grid, in the vendor's editable M/L/C/Z path grammar.
# Nothing here is in physical units: a consumer scales the grid to whatever box
# it has.
ICON_GRID = 24.0

_DATA_PACKAGE = "raven.ppt.services.assets"
_DATA_FILE = "data/tabler_outline.json"

# Command name -> how many coordinates it carries.
_COMMAND_ARITY = {"M": 2, "L": 2, "C": 6, "Z": 0}

# One path is a run of commands; one icon is a list of paths.
IconPath = tuple[tuple[str, tuple[float, ...]], ...]


class IconDataError(RuntimeError):
    """The packaged icon data is missing, unparsable or malformed."""


class UnknownIconError(LookupError):
    """A name is not in the set, with the nearest candidates attached.

    ``LookupError`` rather than ``KeyError`` because ``KeyError``'s ``str()``
    wraps the whole message in quotes, and the message here is a sentence the
    author is meant to read and act on.
    """

    def __init__(self, name: str, candidates: list[str], total: int):
        self.name = name
        self.candidates = candidates
        hint = (
            f"; closest: {', '.join(candidates)}"
            if candidates
            else f"; nothing close -- all {total} names are in ICON_NAMES"
        )
        super().__init__(f"unknown icon {name!r}{hint}")


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


@lru_cache(maxsize=1)
def _load() -> tuple[Mapping[str, tuple[IconPath, ...]], Mapping[str, object], Mapping[str, list]]:
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
    header = {key: value for key, value in payload.items() if key != "icons"}
    # The raw form is kept alongside the parsed one because it is what travels
    # into a build directory verbatim; re-serializing the parsed tuples would
    # produce the same bytes only by accident.
    return MappingProxyType(parsed), MappingProxyType(header), MappingProxyType(dict(sorted(raw_icons.items())))


def icon_names() -> tuple[str, ...]:
    """Every icon name, sorted. Load errors surface here rather than at import."""
    return tuple(_load()[0])


def icon_provenance() -> Mapping[str, object]:
    """Source, upstream version and licence pointer, straight from the data file."""
    return _load()[1]


def catalog_json() -> str:
    """The geometry as JSON text, for injecting into a build directory.

    Text rather than a dict because that is what the caller writes: handing back
    the parsed structure would mean a deep copy to keep the cached one safe, and
    a re-serialization to get here anyway.
    """
    return json.dumps(dict(_load()[2]), separators=(",", ":"))


def _normalize(name: str) -> str:
    return str(name).strip().lower().replace("-", "_").replace(" ", "_").replace(".", "_")


def icon_candidates(name: str, limit: int = 8) -> list[str]:
    """The nearest names to a miss, best guess first.

    Three passes, because the ways a name misses are different. A shared word
    (``arrows-move`` -> ``arrows_exchange``) is the strongest signal and comes
    first. A containment (``chart_bars`` -> ``chart_bar``) is next. Character
    similarity (``click`` -> ``clock``) catches the typos the first two cannot,
    and runs last because it also produces the loosest matches.
    """
    key = _normalize(name)
    if not key:
        return []
    names = icon_names()
    tokens = [part for part in key.split("_") if part]
    ranked: list[str] = []

    def add(candidate: str) -> None:
        if candidate not in ranked:
            ranked.append(candidate)

    for candidate in names:
        parts = candidate.split("_")
        if any(token in parts for token in tokens):
            add(candidate)
    for candidate in names:
        if key in candidate or candidate in key:
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
