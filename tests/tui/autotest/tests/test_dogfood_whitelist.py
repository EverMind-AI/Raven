"""Dogfood — a slash command renders through the whole stack.

A slash the TUI does not own locally goes out as `slash.exec`, runs Click
in-process on the Python side, and comes back through the unix socket to be
rendered by Ink. That round trip is what only a real terminal can prove; the
dispatch layer itself has 31 dedicated tests in `tests/test_rpc_cli_dispatch.py`
and needs no TUI.

The TUI leg is identical for every dispatched command (`createSlashHandler.ts`
routes them all through one `slash.exec` call), so this covers output *shapes*
rather than a command list -- a paged table, an inline notice, a usage error.
Repeating it per command bought nothing and cost 12 spawns a run (#228).

Two properties this file has to keep, both learned the hard way (#228):

- The expected pattern is matched against the whole screen, which includes the
  composer echoing what was just typed and the welcome frame's own text. A
  pattern like `skill` or `cron` is therefore satisfied before the command runs
  at all. Every entry below is a literal from real command output, and
  `_assert_absent_before_submit` fails loudly the day one stops being specific.
- Ctrl+C is a ladder, not an exit key (`ui-tui/src/app/useInputHandlers.ts`):
  while the UI is busy it cancels the turn, with text in the composer it clears
  the input, and only then does it quit. `exit_tui` presses until the process
  is gone.
"""

from __future__ import annotations

import re

import pytest

from tests.tui.autotest.raven_ux import READY_RE, exit_tui

# (slash command, a literal from that command's real output)
#
# One entry per rendering shape the stack can produce, not one per command:
_WHITELIST = [
    # top-level command, panel output
    ("status", r"Raven Status"),
    # long output -> pager overlay, and a table header the welcome frame's own
    # "Available Skills (1)" line cannot satisfy
    ("skill list", r"Source.+Description"),
    # non-zero exit -> Click usage error rendered as a warning
    ("skill get", r"Missing argument"),
    # short output -> inline notice instead of a pager
    ("sentinel routines", r"No routines in store|Routines \(\d+\)"),
]


def _make_test_id(entry):
    return entry[0].replace(" ", "_")


def _assert_absent_before_submit(harness, slash: str, expected: str) -> None:
    """Guard against an expected pattern the idle screen already satisfies."""
    screen = harness.screen()
    assert not re.search(expected, screen), (
        f"expected pattern {expected!r} for /{slash} already matches the idle screen, "
        f"so the output assertion would pass without the command running; screen=\n{screen}"
    )


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

    exit_tui(harness)
    assert harness.expect_exit(0, timeout=10.0), f"TUI did not exit 0 after /{slash}; final screen=\n{harness.screen()}"
