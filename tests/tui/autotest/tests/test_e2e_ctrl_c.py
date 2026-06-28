"""E2E Ctrl+C hardening.

Two scenarios:
1. Ctrl+C at input prompt (no typing) — exit clean
2. Ctrl+C after typing partial input (cancel pending line) — exit clean

Streaming-period Ctrl+C is intentionally out of scope (timing-dependent,
flaky); deferred hardening.

Ink auto-exits 0 on Ctrl+C.
"""

from __future__ import annotations

import pytest


@pytest.mark.e2e
def test_ctrl_c_at_idle_prompt(harness):
    harness.spawn("uv run raven tui")
    assert harness.wait(r"Raven", timeout=25.0)
    # No input — direct Ctrl+C from idle state
    harness.press("ctrl+c")
    assert harness.expect_exit(0, timeout=10.0), (
        f"TUI did not exit 0 on idle Ctrl+C; final screen=\n{harness.screen()}"
    )


@pytest.mark.e2e
def test_ctrl_c_during_typing(harness):
    """Standard TUI ergonomics: 1st Ctrl+C cancels the in-progress input
    line; 2nd Ctrl+C exits. Raven TUI follows this pattern (verified
    interactively 2026-05-20)."""
    harness.spawn("uv run raven tui")
    assert harness.wait(r"Raven", timeout=25.0)
    harness.type("partial input that will never send")
    # First Ctrl+C — cancels input
    harness.press("ctrl+c")
    # Brief settle window for input clear
    import time as _t
    _t.sleep(0.5)
    # Second Ctrl+C — exits
    harness.press("ctrl+c")
    assert harness.expect_exit(0, timeout=10.0), (
        f"TUI did not exit 0 after cancel-then-exit Ctrl+C; "
        f"final screen=\n{harness.screen()}"
    )
