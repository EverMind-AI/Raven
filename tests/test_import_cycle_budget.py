"""The import cycles among raven's top-level subpackages may not grow.

`acyclic_siblings` is the import-linter contract that would forbid cycles
outright. It is not in pyproject.toml because it is BROKEN: 18 of the 40
top-level subpackages sit in one strongly connected component. Added as a gate
it would paint CI red on arrival and be switched off within the week.

So the same rule is stated as a budget. The three ceilings are what the tree
measures and may only go down: unpicking a cycle lowers one, which is a reviewed
edit to this file rather than a drive-by.

What the ceilings do and do not catch
-------------------------------------
They are aggregate counts, so they catch an edge that draws something NEW into a
cycle -- a package or module that was outside one, or a pair that did not import
each other. They do NOT catch an edge added between two members of the existing
component: everything there is already counted, so every total stays put. That
case is left to the contracts and to review; this file is a ratchet on breadth,
not a proof that no cycle was deepened.

Why three numbers
-----------------
Mutual pairs are the edges people add, and the failure message names them. A
cycle three packages long shows up in no pair, so a component measure carries
the general case. And the package measure is an artifact of granularity as much
as of design: collapsing a package to one node turns "config's writer reaches
agent, which reaches config's loader" into a cycle even where no module imports
itself back, so the module measure answers the different question of whether a
reader has a real knot to walk around. The two move independently -- the
vocabulary split below took the package count from 22 to 18 while moving the
module count by three.

All three read the graph import-linter itself is checked against, built with the
`exclude_type_checking_imports` setting from `[tool.importlinter]`. Measuring
without it counts imports that only exist for a type checker, which puts
`gateway` in a cycle it does not have at runtime.
"""

from __future__ import annotations

import tomllib
from collections import defaultdict
from pathlib import Path

import grimp

REPO = Path(__file__).resolve().parent.parent
ROOT_PACKAGE = "raven"

# Measured when the guard landed. Down only, with one exception on record: the
# 2026-09-11 upstream sync landed four modules that import each other in pairs
# (cli.trajectory_browse with cli.trajectory_commands, trajectory.bugreport with
# trajectory.review). Neither pair can be unpicked by deferring an import --
# this graph counts a function-local import the same as a top-level one -- so
# both would mean moving code that had just arrived from upstream, inside a sync.
# The module count was raised 42 -> 46 for that, deliberately; the package and
# pair counts did not move.
PACKAGES_IN_CYCLES_CEILING = 18
MUTUAL_PAIRS_CEILING = 13
MODULES_IN_CYCLES_CEILING = 46


def _graph() -> grimp.ImportGraph:
    """The graph the contracts are checked against, not a second opinion on it.

    ``[tool.importlinter]`` sets ``exclude_type_checking_imports``, so an import
    that only exists inside an ``if TYPE_CHECKING:`` block is not a dependency as
    far as this repository is concerned. Building without the option counts those
    edges and measures a graph no contract enforces: it puts ``gateway`` in a
    cycle it does not have at runtime, and moves the module count by three. The
    option is read from the file rather than hardcoded, so the guard cannot drift
    from the policy it is meant to track.

    ``cache_dir=None`` because grimp's default is a shared ``.grimp_cache`` that
    outlives a branch switch. A guard whose whole job is to report the graph of
    the checked-out tree must not read a graph some earlier revision left behind:
    a stale hit makes it red or green for reasons that are not in the diff. This
    graph takes about a second to build, so there is nothing to cache for.
    """
    settings = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    linter = settings["tool"]["importlinter"]
    return grimp.build_graph(
        ROOT_PACKAGE,
        exclude_type_checking_imports=linter["exclude_type_checking_imports"],
        cache_dir=None,
    )


def _subpackage_graph() -> dict[str, set[str]]:
    """Top-level subpackage -> the sibling subpackages it imports."""
    graph = _graph()
    edges: dict[str, set[str]] = defaultdict(set)
    for module in graph.modules:
        parts = module.split(".")
        if len(parts) < 2:
            continue
        importer = parts[1]
        for imported in graph.find_modules_directly_imported_by(module):
            target = imported.split(".")
            if len(target) >= 2 and target[0] == ROOT_PACKAGE and target[1] != importer:
                edges[importer].add(target[1])
    return dict(edges)


def _module_graph() -> dict[str, set[str]]:
    """Module -> the modules inside this package it imports."""
    graph = _graph()
    known = set(graph.modules)
    edges: dict[str, set[str]] = defaultdict(set)
    for module in graph.modules:
        for imported in graph.find_modules_directly_imported_by(module):
            if imported in known:
                edges[module].add(imported)
    return dict(edges)


def _nodes(edges: dict[str, set[str]]) -> set[str]:
    return set(edges) | {target for targets in edges.values() for target in targets}


def _reaches_itself(start: str, edges: dict[str, set[str]]) -> bool:
    seen: set[str] = set()
    stack = list(edges.get(start, ()))
    while stack:
        node = stack.pop()
        if node == start:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(edges.get(node, ()))
    return False


def _mutual_pairs(edges: dict[str, set[str]]) -> list[tuple[str, str]]:
    return sorted((a, b) for a in edges for b in edges[a] if a < b and a in edges.get(b, set()))


def test_the_graph_is_not_empty() -> None:
    """A graph read as empty would meet every budget below."""
    edges = _subpackage_graph()

    assert len(_nodes(edges)) >= 30, sorted(_nodes(edges))


def test_no_new_package_is_drawn_into_a_cycle() -> None:
    edges = _subpackage_graph()

    cyclic = sorted(node for node in _nodes(edges) if _reaches_itself(node, edges))

    assert len(cyclic) <= PACKAGES_IN_CYCLES_CEILING, (
        f"{len(cyclic)} subpackages are inside an import cycle, over the ceiling of "
        f"{PACKAGES_IN_CYCLES_CEILING}: {cyclic}"
    )


def test_no_new_pair_of_subpackages_imports_each_other() -> None:
    edges = _subpackage_graph()

    pairs = _mutual_pairs(edges)

    assert len(pairs) <= MUTUAL_PAIRS_CEILING, (
        f"{len(pairs)} pairs of subpackages import each other, over the ceiling of "
        f"{MUTUAL_PAIRS_CEILING}: {[f'{a} <-> {b}' for a, b in pairs]}"
    )


def test_no_new_module_is_drawn_into_a_cycle() -> None:
    """The package measure is blind to granularity; this one is not.

    Collapsing a package to one node turns "config's writer reaches agent, which
    reaches config's loader" into a cycle even where no module imports itself
    back. The module measure answers the other question -- whether a reader has a
    real knot to walk around -- and the two move independently: the vocabulary
    split ahead of this guard took the package count from 22 to 18 while moving
    this one by three.
    """
    edges = _module_graph()

    cyclic = sorted(node for node in _nodes(edges) if _reaches_itself(node, edges))

    assert len(cyclic) <= MODULES_IN_CYCLES_CEILING, (
        f"{len(cyclic)} modules are inside an import cycle, over the ceiling of {MODULES_IN_CYCLES_CEILING}: {cyclic}"
    )
