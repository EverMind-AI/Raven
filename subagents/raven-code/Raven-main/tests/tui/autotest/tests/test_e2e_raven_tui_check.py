"""E2E smoke: `raven tui --check` exits cleanly without entering Ink loop.

Validates that Harness can drive the production binary as far as the
Python parent + Node child handshake, without paying chat-E2E cost.
"""

from __future__ import annotations

import pytest


@pytest.mark.e2e
def test_tui_check_exits_clean(harness):
    harness.spawn("uv run raven tui --check")
    # --check is a print-then-exit mode; expect natural exit within 20s
    # (Node child cold-load + Python parent typer dispatch)
    assert harness.expect_exit(0, timeout=20.0), (
        f"`raven tui --check` did not exit 0 in 20s; screen=\n{harness.screen()}"
    )
