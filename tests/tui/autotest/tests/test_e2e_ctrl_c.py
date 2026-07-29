"""E2E Ctrl+C hardening.

Ctrl+C is a ladder in `ui-tui/src/app/useInputHandlers.ts`, not an exit key:
a busy UI cancels the turn, a non-empty composer clears the input, and only an
idle UI with an empty composer quits. These pin the two rungs reachable without
spending a turn, and each asserts the intermediate state rather than only the
final exit code -- pressing twice and checking just the exit passes even when
the first press did nothing.

The cancel rung needs a live turn, which `test_e2e_raven_tui_chat.py` owns.
"""

from __future__ import annotations

import re
import time

import pytest

from tests.tui.autotest.statusbar import READY_RE

_PARTIAL = "partial input that will never send"


@pytest.mark.e2e
def test_ctrl_c_at_idle_prompt(harness):
    harness.spawn("uv run raven tui")
    assert harness.wait(READY_RE, timeout=60.0), f"TUI status bar never reported ready; screen=\n{harness.screen()}"
    harness.press("ctrl+c")
    assert harness.expect_exit(0, timeout=10.0), f"TUI did not exit 0 on idle Ctrl+C; final screen=\n{harness.screen()}"


@pytest.mark.e2e
def test_ctrl_c_clears_the_composer_before_it_exits(harness):
    """First Ctrl+C clears a pending line and leaves the process up; second exits."""
    harness.spawn("uv run raven tui")
    assert harness.wait(READY_RE, timeout=60.0), f"TUI status bar never reported ready; screen=\n{harness.screen()}"

    harness.type(_PARTIAL)
    assert harness.wait(re.escape(_PARTIAL), timeout=10.0), (
        f"composer never took the text, so there is no pending line to cancel; screen=\n{harness.screen()}"
    )

    harness.press("ctrl+c")
    cleared = False
    for _ in range(20):
        if not re.search(re.escape(_PARTIAL), harness.screen()):
            cleared = True
            break
        time.sleep(0.25)
    assert cleared, f"first Ctrl+C left the pending line in the composer; screen=\n{harness.screen()}"
    assert harness.expect_exit(0, timeout=0.0) is False, "first Ctrl+C exited instead of clearing the composer"

    harness.press("ctrl+c")
    assert harness.expect_exit(0, timeout=10.0), (
        f"second Ctrl+C did not exit 0 after the composer was cleared; final screen=\n{harness.screen()}"
    )
