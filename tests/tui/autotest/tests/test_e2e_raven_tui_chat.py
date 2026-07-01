"""E2E ⭐ ACCEPTANCE: `raven tui` alt-screen chat with real Qwen.

If this passes, the harness can drive a full Ink alt-screen TUI through
RPC + streaming + slash routing + Ctrl+C autonomy — i.e., Claude Code can
independently reproduce any TUI bug from `Bash()`.

Requires:
- `tui-use` >=0.1.20 on PATH (npm install -g tui-use)
- Built `ui-tui/dist/entry.js` (npm install + npm run build in ui-tui/)
- User has OpenRouter / Qwen provider configured

Ink Ctrl+C autonomy yields exit 0, NOT 130. expect_exit(0) is correct.
"""

from __future__ import annotations

import pytest


@pytest.mark.e2e
def test_tui_chat_qwen_round_trip(harness):
    # turn.send/turn.subscribe + SubscriptionEmitter are wired; this test
    # runs as a regular live E2E ACCEPTANCE for the chat streaming path
    # through the TUI.
    harness.spawn("uv run raven tui")
    # Note: tui-use snapshot returns ALT-SCREEN rendered text only — the 🦞
    # emoji visible in tui-use wait --text (which searches full stream incl.
    # scrollback) is NOT in snapshot once Ink switches to alt-screen.
    # Use "Raven" brand text (alt-screen-visible) for readiness instead.
    assert harness.wait(r"Raven", timeout=25.0), (
        f"TUI Raven readiness banner not seen in 25s; screen=\n{harness.screen()}"
    )
    harness.type("What's your model's name?")
    harness.press("enter")
    assert harness.wait(r"Qwen", timeout=60.0), f"Qwen model did not respond within 60s; screen=\n{harness.screen()}"
    # Cancel UX volatile post-streaming (same class as dogfood overlay tests).
    # Use Esc + Ctrl+C robust pattern.
    import time as _t

    from tests.tui.autotest.runner import BackendError

    for key in ("escape", "ctrl+c"):
        try:
            harness.press(key)
        except BackendError:
            break
        _t.sleep(0.5)
    assert harness.expect_exit(0, timeout=10.0), (
        f"TUI did not exit 0 after Esc+Ctrl+C; final screen=\n{harness.screen()}"
    )
