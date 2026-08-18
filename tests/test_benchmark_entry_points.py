"""The benchmark harnesses construct AgentLoop by keyword; nothing else checks them.

`benchmarks/` is outside the package, has its own heavy dependencies and is never
imported by a test, so a parameter renamed or retired on `AgentLoop.__init__`
leaves those call sites passing an argument the constructor no longer takes.
Ruff does not flag a wrong keyword and CI stays green, while the harness dies on
its first line with `TypeError: unexpected keyword argument` -- which is how
`everos_config` survived there long after the parameter was retired.

Read statically rather than by importing: the harnesses pull in appworld,
pinchbench and clawbench dependencies this suite does not have, and the mistake
this guards against is visible in the source without running any of it.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop

_ROOT = Path(__file__).resolve().parents[1]
_HARNESSES = sorted((_ROOT / "benchmarks").rglob("*.py"))


def _agent_loop_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "AgentLoop"
    ]


def test_the_benchmarks_still_construct_agentloop_with_parameters_it_has() -> None:
    accepted = set(inspect.signature(AgentLoop.__init__).parameters)
    assert "self" in accepted, "signature read failed; the check below would pass vacuously"

    offenders: list[str] = []
    seen = 0
    for path in _HARNESSES:
        for call in _agent_loop_calls(path):
            seen += 1
            unknown = sorted({kw.arg for kw in call.keywords if kw.arg} - accepted)
            if unknown:
                offenders.append(f"{path.relative_to(_ROOT)}:{call.lineno} passes {unknown}")

    assert seen, "found no AgentLoop( call sites under benchmarks/ -- has the tree moved?"
    assert not offenders, "benchmark harness would die on construction:\n  " + "\n  ".join(offenders)


@pytest.mark.parametrize("retired", ["brave_api_key", "everos_config"])
def test_a_retired_parameter_is_gone_from_the_benchmarks_too(retired: str) -> None:
    """Named cases for the two this test was written after.

    The check above covers them, but only while it can parse every harness; these
    two are cheap to state outright, and a grep hit is a clearer failure than a
    signature diff.
    """
    hits = [f"{p.relative_to(_ROOT)}" for p in _HARNESSES if f"{retired}=" in p.read_text(encoding="utf-8")]
    assert not hits, f"{retired} is no longer an AgentLoop parameter, still passed by: {hits}"
