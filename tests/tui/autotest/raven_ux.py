"""Raven TUI behaviour the e2e tests share: status-bar patterns and the exit ladder.

Mirrored from ``ui-tui/src/content/verbs.ts``; kept in one place so a verb-pool
edit there has a single place to land rather than one copy per test file.
"""

from __future__ import annotations

import re

# Idle turn state (word-bounded so "readiness" won't match).
READY_RE = re.compile(r"\bready\b", re.IGNORECASE)

# Working-state verbs shown while a turn is in flight, mirroring VERBS.
WORKING_RE = re.compile(
    r"\b(pondering|contemplating|musing|cogitating|ruminating|deliberating|"
    r"mulling|reflecting|processing|reasoning|analyzing|computing|"
    r"synthesizing|formulating|brainstorming)…",
    re.IGNORECASE,
)


# Ctrl+C is a ladder in ui-tui/src/app/useInputHandlers.ts, not an exit key: a
# busy UI cancels the turn, a non-empty composer clears the input, and only an
# idle UI with an empty composer quits. How many presses a test needs therefore
# depends on what the command left behind, so press until the process is gone.
_MAX_CTRL_C = 4


def exit_tui(harness) -> None:
    """Escape closes any pager, then Ctrl+C until the ladder reaches quit."""
    from tests.tui.autotest.runner import BackendError

    try:
        harness.press("escape")
    except BackendError:
        return
    for _ in range(_MAX_CTRL_C):
        if harness.expect_exit(0, timeout=2.0):
            return
        try:
            harness.press("ctrl+c")
        except BackendError:
            return
