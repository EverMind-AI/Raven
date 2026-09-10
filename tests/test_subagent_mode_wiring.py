"""Every sub-agent dispatch lane must resolve its mode through one helper.

The override a user sets on an instance reached a direct chat and not a spawn,
because each lane resolved it for itself and only one of them remembered. The
result was silent: the spawn ran, answered, and cost whatever the agent's own
default costs, with nothing anywhere saying the setting had been ignored.

A per-lane test cannot catch the class. Each lane passes its own test; what is
wrong is the lane that was added without one. So this file enumerates the lanes
instead, and requires each to hand a mode over -- and requires the override
itself to be read in exactly one place, so the lanes cannot drift apart again.

Source-level on purpose: the failure mode is a missing keyword argument, which
produces no runtime signal at all.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

RAVEN = Path(__file__).resolve().parents[1] / "raven"

SUBAGENT = RAVEN / "agent" / "subagent"

# `probe.py` dispatches a `backend.run(task_id=...)` too, and correctly takes no
# mode: it is a connectivity test against a throwaway registry, not a lane a
# user's override could reach. Named here rather than met by an allow-list of
# lane files, because an allow-list cannot fail for the lane it does not list --
# which is the one this file exists to catch.
#
# `backends/routing.py` forwards one lane's own call to the implementation it
# picked, keywords and all (`**kwargs`): the mode was resolved by the lane that
# reached it, so the forward is not a place a mode could go missing. Its
# pass-through is pinned at runtime in test_subagent_routing_backend.py.
NOT_A_LANE = {"probe.py", "routing.py"}

# Where a sub-agent task is handed to a backend. A lane is a `backend.run(...)`
# carrying `task_id`, which is what separates a dispatch from every other `run`.
LANE_FILES = (
    "agent/subagent/manager.py",
    "agent/subagent/dag_runner.py",
)


def _lanes() -> list[tuple[str, int, set[str]]]:
    found = []
    for path in sorted(SUBAGENT.rglob("*.py")):
        if path.name in NOT_A_LANE:
            continue
        rel = path.relative_to(RAVEN).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "run":
                continue
            names = {kw.arg for kw in node.keywords if kw.arg}
            names |= {name for kw in node.keywords if kw.arg is None for name in _spread_keys(kw.value)}
            if "task_id" in names:
                found.append((rel, node.lineno, names))
    return sorted(found)


def _spread_keys(node: ast.AST) -> set[str]:
    """The keyword names a ``**...`` spread can hand over: a dict literal's
    constant keys (through an ``if`` expression) or ``optional_keyword``'s name
    argument -- the shape a lane uses for a keyword the paper gained after some
    backend was written, so the spread carries it only to a ``run`` that declares it."""
    if isinstance(node, ast.Dict):
        return {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    if isinstance(node, ast.IfExp):
        return _spread_keys(node.body) | _spread_keys(node.orelse)
    if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "optional_keyword":
        arg = node.args[1] if len(node.args) > 1 else None
        return {arg.value} if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else set()
    return set()


def test_the_enumeration_still_finds_the_dispatch_lanes():
    """Three: a spawn, a direct chat, and a DAG node. A fourth arriving here
    should fail this and be added deliberately, not inherit the rule by luck.

    The scan reaches every module under `agent/subagent/`, so a lane opened in a
    new file is caught by the count rather than missed by an allow-list; the
    file assertion then says which files are expected to hold one.
    """
    lanes = _lanes()
    assert len(lanes) == 3, [f"{rel}:{line}" for rel, line, _ in lanes]
    assert {rel for rel, _, _ in lanes} == set(LANE_FILES)


@pytest.mark.parametrize("lane", _lanes(), ids=lambda lane: f"{lane[0]}:{lane[1]}")
def test_every_dispatch_lane_hands_the_backend_a_mode(lane):
    rel, line, names = lane
    assert "mode" in names, f"{rel}:{line} dispatches without a mode"


@pytest.mark.parametrize("lane", _lanes(), ids=lambda lane: f"{lane[0]}:{lane[1]}")
def test_every_dispatch_lane_hands_the_backend_the_authored_task(lane):
    """The same enumeration, for the other value that went missing once: a
    routing entry reads the task as the model wrote it, and a lane that hands
    over only its rendering lets inlined file contents pick the implementation
    (and travel through the host model a second time). Source-level for the
    same reason as the mode: the missing keyword makes no runtime noise."""
    rel, line, names = lane
    assert "authored_task" in names, f"{rel}:{line} dispatches without the authored task"


def test_the_override_is_read_in_exactly_one_place():
    """`resolve_mode` is that place. A lane reading `instance_mode` for itself is
    how the spawn/chat split happened, and would re-introduce it -- the reader
    that forgets `requested` looks correct until a caller names a mode."""
    tree = ast.parse((RAVEN / "agent/subagent/manager.py").read_text(encoding="utf-8"))
    readers = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "instance_mode":
                    readers.add(fn.name)
    assert readers == {"resolve_mode"}


def test_the_dag_lane_takes_its_resolver_from_the_manager():
    """The DAG tool is built from the same config as the manager but does not own
    one (see its constructor), so the rule reaches that lane by injection or not
    at all. Both construction sites hand it over -- the registered tool and the
    playbook executor's -- because a playbook step names an instance too."""
    source = (RAVEN / "agent/loop/wiring.py").read_text(encoding="utf-8")
    assert source.count("mode_for=self.subagents.resolve_mode") == 2
