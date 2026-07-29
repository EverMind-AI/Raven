"""Dogfood — TUI whitelist commands routed through cli.dispatch.

Each command is sent into the alt-screen TUI via `/cmd<sub>` slash routing,
which hits the `cli.dispatch` RPC method on the Python side; the rendered
Click output streams back through the unix socket and renders into Ink.

Pattern: spawn -> wait for the status bar to report ready -> type slash ->
confirm the composer took it -> enter -> wait for output -> exit.

Two properties this file has to keep, both learned the hard way (#228):

- The expected pattern is matched against the whole screen, which includes the
  composer echoing what was just typed and the welcome frame's own text. A
  pattern like `skill` or `cron` is therefore satisfied before the command runs
  at all. Every entry below is a literal from real command output, and
  `_assert_absent_before_submit` fails loudly the day one stops being specific.
- Ctrl+C is a ladder, not an exit key (`ui-tui/src/app/useInputHandlers.ts`):
  while the UI is busy it cancels the turn, with text in the composer it clears
  the input, and only then does it quit. `_exit_tui` presses until the process
  is gone.
"""

from __future__ import annotations

import re

import pytest

from tests.tui.autotest.runner import BackendError
from tests.tui.autotest.statusbar import READY_RE

# (slash command, a literal from that command's real output)
#
# Alternations cover renderings that depend on local state: an empty store
# prints a one-line notice where a populated one renders a table.
_WHITELIST = [
    ("status", r"Raven Status"),
    ("channels status", r"Channel Status"),
    ("channels list", r"Available Channels"),
    ("skill list", r"Source.+Description"),
    ("skill get", r"Missing argument"),
    ("cron list", r"Cron service"),
    ("cron get", r"Missing argument"),
    ("sentinel status", r"Sentinel Status"),
    # --state defaults on, so the NudgePolicy section prints even with an
    # empty feedback log; the table title only appears when events exist.
    ("sentinel nudges", r"Recent NudgeFeedback|NudgePolicy state"),
    ("sentinel decisions", r"No live decisions|Discovery decisions \("),
    ("sentinel routines", r"No routines in store|Routines \(\d+\)"),
    ("sandbox list", r"Sandbox VMs|Debug socket not found"),
]

_MAX_CTRL_C = 4


def _make_test_id(entry):
    return entry[0].replace(" ", "_")


def _assert_absent_before_submit(harness, slash: str, expected: str) -> None:
    """Guard against an expected pattern the idle screen already satisfies."""
    screen = harness.screen()
    assert not re.search(expected, screen), (
        f"expected pattern {expected!r} for /{slash} already matches the idle screen, "
        f"so the output assertion would pass without the command running; screen=\n{screen}"
    )


def _exit_tui(harness) -> None:
    """Escape closes any pager, then Ctrl+C until the ladder reaches quit."""
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


@pytest.mark.e2e
@pytest.mark.parametrize(
    ("slash", "expected"),
    _WHITELIST,
    ids=[_make_test_id(e) for e in _WHITELIST],
)
def test_dogfood_slash_command(harness, slash, expected):
    harness.spawn("uv run raven tui")
    assert harness.wait(READY_RE, timeout=60.0), (
        f"TUI status bar never reported ready for /{slash}; screen=\n{harness.screen()}"
    )

    _assert_absent_before_submit(harness, slash, expected)

    harness.type(f"/{slash}")
    assert harness.wait(re.escape(f"/{slash}"), timeout=10.0), (
        f"composer never showed /{slash}, so enter would submit nothing; screen=\n{harness.screen()}"
    )
    harness.press("enter")

    assert harness.wait(expected, timeout=30.0), (
        f"slash /{slash} did not produce expected output (regex={expected!r}); screen=\n{harness.screen()}"
    )

    _exit_tui(harness)
    assert harness.expect_exit(0, timeout=10.0), (
        f"TUI did not exit 0 after /{slash} and {_MAX_CTRL_C} ctrl+c presses; final screen=\n{harness.screen()}"
    )
