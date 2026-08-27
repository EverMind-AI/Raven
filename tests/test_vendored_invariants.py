"""A fix this trunk carries must be present in every vendored subagent.

Two things are under test and they fail differently. The registry itself is
checked against the real trees: every fork must hold every invariant, and a
path that no longer registers what the invariant names counts as a failure
rather than a pass, because a stale path is how a guard stops guarding without
anyone noticing. The reader is checked against synthetic sources, since the
shapes that matter -- a registration in the ``else`` of the guard, one behind a
local name -- are not all present in the trees at any given time.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from scripts.check_vendored_invariants import (
    INVARIANTS,
    GuardedRegistration,
    _check,
    _unguarded_lines,
    main,
)

REPO = Path(__file__).resolve().parent.parent

WEB_SEARCH = INVARIANTS[0]


def test_every_fork_holds_every_invariant() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/check_vendored_invariants.py"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_every_invariant_names_a_path_that_exists_in_every_fork() -> None:
    for inv in INVARIANTS:
        for rel in (*inv.trunk, *(p for paths in inv.forks.values() for p in paths)):
            assert (REPO / rel).is_file(), f"[{inv.id}] {rel} is gone"


def test_an_exemption_carries_a_reason() -> None:
    for inv in INVARIANTS:
        for fork, reason in inv.exempt.items():
            assert fork in inv.forks, f"[{inv.id}] exempts {fork}, which it does not check"
            assert reason.strip(), f"[{inv.id}] exempts {fork} with no reason"


def _lines(source: str) -> list[int]:
    unguarded, _ = _unguarded_lines(source, "WebSearchTool", ("api_key", "corpus_endpoint"))
    return unguarded


def test_an_unguarded_registration_is_reported() -> None:
    assert _lines("tools.register(WebSearchTool(api_key=k))\n") == [1]


def test_a_guarded_registration_is_accepted() -> None:
    source = "if k.api_key:\n    tools.register(WebSearchTool(api_key=k))\n"
    assert _lines(source) == []


def test_a_registration_behind_a_local_name_is_accepted() -> None:
    source = "s = WebSearchTool(api_key=k)\nif s.api_key:\n    tools.register(s)\n"
    assert _lines(source) == []


def test_a_registration_in_the_else_of_the_guard_is_reported() -> None:
    source = "s = WebSearchTool(api_key=k)\nif s.api_key:\n    pass\nelse:\n    tools.register(s)\n"
    assert _lines(source) == [5]


def test_an_inverted_guard_does_not_certify_its_body() -> None:
    """``if not key: register(...)`` is the defect wearing a guard's shape.

    Reading the test for the guard's *name* alone would pass it, which is worse
    than not checking: the report would say the fork holds the invariant.
    """
    source = "s = WebSearchTool(api_key=k)\nif not s.api_key:\n    tools.register(s)\n"
    assert _lines(source) == [3]


def test_an_inverted_guard_certifies_its_else() -> None:
    source = "s = WebSearchTool(api_key=k)\nif not s.api_key:\n    pass\nelse:\n    tools.register(s)\n"
    assert _lines(source) == []


def test_a_comparison_against_a_falsey_constant_reads_as_inverted() -> None:
    body = "s = WebSearchTool(api_key=k)\nif s.api_key is None:\n    tools.register(s)\n"
    orelse = "s = WebSearchTool(api_key=k)\nif s.api_key is None:\n    pass\nelse:\n    tools.register(s)\n"
    assert _lines(body) == [3]
    assert _lines(orelse) == []


def test_a_guard_this_reader_cannot_follow_fails_loudly() -> None:
    """An early return above the call is a real guard the reader does not model.

    It is reported rather than accepted, on purpose: a false alarm costs a
    reviewer one look, and a silent pass costs the guarantee.
    """
    source = (
        "def register_tools(self):\n"
        "    s = WebSearchTool(api_key=k)\n"
        "    if not s.api_key:\n"
        "        return\n"
        "    tools.register(s)\n"
    )
    assert _lines(source) == [5]


def test_either_named_guard_satisfies_the_invariant() -> None:
    source = "s = WebSearchTool(api_key=k)\nif s.corpus_endpoint:\n    tools.register(s)\n"
    assert _lines(source) == []


def test_an_unrelated_guard_does_not_satisfy_the_invariant() -> None:
    source = "s = WebSearchTool(api_key=k)\nif s.enabled:\n    tools.register(s)\n"
    assert _lines(source) == [3]


def test_an_outer_guard_covers_a_nested_registration() -> None:
    source = "s = WebSearchTool(api_key=k)\nif s.api_key:\n    if allowed:\n        tools.register(s)\n"
    assert _lines(source) == []


def test_registering_something_else_is_not_looked_at() -> None:
    unguarded, seen = _unguarded_lines("tools.register(WebFetchTool(api_key=k))\n", "WebSearchTool", ("api_key",))
    assert unguarded == [] and seen is False


def test_a_path_that_registers_nothing_fails_as_stale(tmp_path: Path) -> None:
    stale = tmp_path / "loop.py"
    stale.write_text("tools.register(WebFetchTool())\n", encoding="utf-8")
    problem = _check(stale, WEB_SEARCH)
    assert problem is not None and "stale" in problem


def test_an_unparseable_path_fails_rather_than_passes(tmp_path: Path) -> None:
    broken = tmp_path / "loop.py"
    broken.write_text("def (:\n", encoding="utf-8")
    problem = _check(broken, WEB_SEARCH)
    assert problem is not None and "cannot parse" in problem


def test_a_missing_path_fails_rather_than_passes(tmp_path: Path) -> None:
    problem = _check(tmp_path / "gone.py", WEB_SEARCH)
    assert problem is not None and "cannot read" in problem


def _run(monkeypatch, invariants: tuple[GuardedRegistration, ...]) -> int:
    monkeypatch.setattr("scripts.check_vendored_invariants.INVARIANTS", invariants)
    return main([])


def test_a_lagging_fork_fails_the_run(monkeypatch, capsys) -> None:
    lagging = replace(
        WEB_SEARCH,
        forks={"raven-oncall": ("subagents/raven-oncall/Raven-Oncall/raven/agent/tools/web.py",)},
    )
    assert _run(monkeypatch, (lagging,)) == 1
    assert "raven-oncall" in capsys.readouterr().err


def test_an_exempt_fork_is_not_checked(monkeypatch) -> None:
    exempted = replace(
        WEB_SEARCH,
        forks={"raven-oncall": ("subagents/raven-oncall/Raven-Oncall/raven/agent/tools/web.py",)},
        exempt={"raven-oncall": "not this fork's job"},
    )
    assert _run(monkeypatch, (exempted,)) == 0


def test_a_trunk_that_moved_fails_the_run(monkeypatch, capsys) -> None:
    moved = replace(WEB_SEARCH, trunk=("raven/agent/tools/web.py",), forks={})
    assert _run(monkeypatch, (moved,)) == 1
    assert "trunk" in capsys.readouterr().err
