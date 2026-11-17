"""Unit tests for the design-step editor (benchmarks.appworld.evolve.editor).

The editor runs the driver against a sandbox. A driver that reaches the
subject checkout is caught by the guard, and what the editor does with that
decides how much of the round one bad command can cost.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarks.appworld.evolve.editor import bash_edit_candidate  # noqa: E402
from benchmarks.appworld.evolve.sandbox import SubjectCheckoutWriteError  # noqa: E402


class _EscapingSandbox:
    """A sandbox whose every bash command reaches the subject checkout."""

    whitelist = ("benchmarks/appworld/",)

    def __init__(self, exc: SubjectCheckoutWriteError):
        self._exc = exc

    def bash(self, command: str, timeout: float = 60.0) -> str:
        raise self._exc


def _driver_returning(action: dict):
    def call_fn(convo: list) -> str:
        return json.dumps(action)

    return call_fn


class TestSubjectCheckoutEscape:
    def test_an_undone_escape_costs_the_candidate_only(self):
        # The guard put the checkout back the way it was, so the round's other
        # candidates are untouched and still worth evaluating.
        sandbox = _EscapingSandbox(
            SubjectCheckoutWriteError("sandbox command modified the subject checkout (tracked files)")
        )
        changed, deleted, summary = bash_edit_candidate(
            _driver_returning({"action": "bash", "command": "cat grader.py"}),
            sandbox,
            "diagnosis",
            [],
            "r1-why-0",
            import_smoke_module=None,
        )
        assert changed is None
        assert deleted == []
        assert "candidate discarded" in summary

    def test_an_escape_the_guard_could_not_undo_stops_the_round(self):
        # The checkout is still contaminated, so what the round measures from
        # here on cannot be trusted.
        sandbox = _EscapingSandbox(
            SubjectCheckoutWriteError(
                "sandbox command modified the subject checkout (tracked files); could not undo: the index",
                unrestored=("the index",),
            )
        )
        with pytest.raises(SubjectCheckoutWriteError, match="could not undo"):
            bash_edit_candidate(
                _driver_returning({"action": "bash", "command": "cat grader.py"}),
                sandbox,
                "diagnosis",
                [],
                "r1-why-0",
                import_smoke_module=None,
            )
