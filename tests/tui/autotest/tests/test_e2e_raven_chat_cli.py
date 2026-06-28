"""E2E: `raven chat` line-mode round-trip with real Qwen provider.

Controls-on chat path for the alt-screen ACCEPTANCE gate
(test_e2e_raven_tui_chat). If THIS test fails, chat layer is broken
regardless of TUI; if THIS passes but tui_chat fails, the bug is in the
TUI/RPC/Ink path.

Requires user has OpenRouter / Qwen provider configured locally
(per spike verification 2026-05-20 — `~/.raven/config.json`).
"""

from __future__ import annotations

import pytest


@pytest.mark.e2e
@pytest.mark.xfail(
    reason=(
        "`raven chat` top-level subcommand still does not exist on "
        "20260519-TUI-refactor post tui-chat L2-A merge — tui-chat "
        "scope was TUI streaming side only (turn.send/turn.subscribe + "
        "SubscriptionEmitter). CLI `chat` REPL is a separate piece. Live "
        "chat ACCEPTANCE for now = `test_e2e_raven_tui_chat.py` (TUI "
        "alt-screen, wired post tui-chat). strict=True ensures this test "
        "starts FAILing if a `chat` subcommand lands."
    ),
    strict=True,
)
def test_chat_cli_qwen_round_trip(harness):
    harness.spawn("uv run raven chat")
    # Wait for chat REPL prompt; the exact glyph may vary but ❯ is the
    # prompt-toolkit-style indicator we saw during spike.
    assert harness.wait(r"❯|>", timeout=15.0), (
        f"chat REPL prompt not ready in 15s; screen=\n{harness.screen()}"
    )
    harness.type("What's your model's name?")
    harness.press("enter")
    assert harness.wait(r"Qwen", timeout=60.0), (
        f"Qwen model did not respond within 60s; screen=\n{harness.screen()}"
    )
    harness.press("ctrl+c")
    assert harness.expect_exit(0, timeout=10.0), (
        f"chat CLI did not exit 0 after Ctrl+C; final screen=\n{harness.screen()}"
    )
