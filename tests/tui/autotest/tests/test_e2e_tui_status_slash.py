"""E2E: `raven tui` alt-screen `/status` round-trip — ACCEPTANCE.

This is the live ACCEPTANCE gate for the harness path: spawn production
`uv run raven tui` (Python parent + Node child + unix socket RPC + Ink
alt-screen renderer), wait for the status bar to report ready, deliver
characters through PTY into Ink's useInput hook, deliver a named key, and read
back output that only `cli.dispatch` can have produced.

The output assertion has to be a string the idle screen does not already carry.
It used to be `OpenRouter|Model:`, which the welcome frame prints in its own
status line, so the gate passed without the command running (#228).

Zero LLM cost; exercises everything except chat streaming — see
test_e2e_raven_tui_chat.py for the chat streaming path.
"""

from __future__ import annotations

import re

import pytest

from tests.tui.autotest.statusbar import READY_RE

_EXPECTED = r"Raven Status"


@pytest.mark.e2e
def test_tui_status_slash_round_trip(harness):
    harness.spawn("uv run raven tui")
    assert harness.wait(READY_RE, timeout=60.0), f"TUI status bar never reported ready; screen=\n{harness.screen()}"

    assert not re.search(_EXPECTED, harness.screen()), (
        f"{_EXPECTED!r} is already on the idle screen, so the assertion below would "
        f"pass without /status running; screen=\n{harness.screen()}"
    )

    harness.type("/status")
    assert harness.wait(r"/status", timeout=10.0), (
        f"composer never showed /status, so enter would submit nothing; screen=\n{harness.screen()}"
    )
    harness.press("enter")

    assert harness.wait(_EXPECTED, timeout=30.0), (
        f"`/status` output not rendered within 30s; screen=\n{harness.screen()}"
    )

    # Ctrl+C is a ladder (busy cancels, pending input clears, idle quits), so
    # press until the process is gone rather than a fixed number of times.
    harness.press("escape")
    for _ in range(4):
        if harness.expect_exit(0, timeout=2.0):
            break
        harness.press("ctrl+c")
    assert harness.expect_exit(0, timeout=10.0), f"TUI did not exit 0 after Ctrl+C; final screen=\n{harness.screen()}"
