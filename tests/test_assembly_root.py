"""One door: config becomes a running agent in exactly one place.

`raven/core` is the assembly root, and `build_runtime` is the call that turns a
config into a live `AgentLoop` with its stacks wired. The value of saying so is
that nobody else does it: an entrance that hand-rolled a loop would get the
tools it remembered to pass and none of the wiring it forgot, and the difference
would show up as a feature that works in the TUI and not over RPC.

Nothing enforced that. The import-linter contracts say who may import whom, not
who may construct what, so this reads the tree for the construction itself.

The one exception is deliberate and named below: replay builds a loop from a
recording rather than from a config, which is the opposite errand -- it must not
read the user's settings.
"""

from __future__ import annotations

import ast
from pathlib import Path

RAVEN = Path(__file__).resolve().parent.parent / "raven"

#: Where an ``AgentLoop`` may be constructed, and why it is allowed to be.
DOORS = {
    "raven/core/runtime.py": "the door: build_runtime, where a config becomes a running agent",
    "raven/trajectory/replay.py": "a recording, not a config: replay must not read the user's settings",
}


def _construction_sites() -> dict[str, list[int]]:
    """Every ``AgentLoop(...)`` call in the package, by file and line."""
    sites: dict[str, list[int]] = {}
    for path in sorted(RAVEN.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name == "AgentLoop":
                rel = path.relative_to(RAVEN.parent).as_posix()
                sites.setdefault(rel, []).append(node.lineno)
    return sites


def test_the_loop_is_built_where_the_architecture_says_it_is() -> None:
    sites = _construction_sites()

    assert set(sites) == set(DOORS), (
        "an AgentLoop is constructed somewhere new, or the door moved. Every "
        f"site needs a reason in DOORS: {sorted(set(sites) ^ set(DOORS))}"
    )


def test_the_door_is_still_a_door() -> None:
    """A guard whose allowlist matched nothing would pass on an empty tree."""
    sites = _construction_sites()

    assert sites, "no AgentLoop construction found at all -- the scan is broken"
    assert len(sites["raven/core/runtime.py"]) == 1, sites["raven/core/runtime.py"]
