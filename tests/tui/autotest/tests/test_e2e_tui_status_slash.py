"""E2E: `raven tui` alt-screen `/status` round-trip — ACCEPTANCE.

This is the **live ACCEPTANCE gate**. It exercises the full harness
path:
1. spawn production `uv run raven tui` (Python parent + Node child + unix
   socket RPC + Ink alt-screen renderer)
2. wait readiness in alt-screen via Harness.wait (proves snapshot polling works
   against Ink-rendered content)
3. type a slash command into the TUI input (proves character delivery through
   PTY → Ink useInput hook)
4. press enter (proves named-key delivery)
5. wait for slash-routed RPC response rendered into alt-screen (proves
   cli.dispatch round-trip — Raven-side Python in-process Click + Rich
   render → RPC → Node-side Ink display)
6. press ctrl+c twice (cancel input then exit per Raven UX)
7. expect_exit 0 (proves Ink autonomy)

Zero LLM cost; exercises everything except chat streaming — see
test_e2e_raven_tui_chat.py for the chat streaming path.
"""

from __future__ import annotations

import pytest


@pytest.mark.e2e
def test_tui_status_slash_round_trip(harness):
    harness.spawn("uv run raven tui")
    assert harness.wait(r"Raven", timeout=25.0), (
        f"TUI Raven readiness banner not seen in 25s; screen=\n{harness.screen()}"
    )
    harness.type("/status")
    harness.press("enter")
    # /status output includes provider table — "OpenRouter" is the user's
    # configured provider (verified during 2026-05-20 spike). Use it as a
    # proxy for "command output rendered into alt-screen".
    assert harness.wait(r"OpenRouter|Model:", timeout=10.0), (
        f"`/status` output not rendered within 10s; screen=\n{harness.screen()}"
    )
    # Ctrl+C twice — first cancels open status overlay / input, second exits
    harness.press("ctrl+c")
    import time as _t
    _t.sleep(0.5)
    harness.press("ctrl+c")
    assert harness.expect_exit(0, timeout=10.0), (
        f"TUI did not exit 0 after Ctrl+C; final screen=\n{harness.screen()}"
    )
