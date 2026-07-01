"""E2E: `raven chat` line-mode round-trip — pipeline liveness.

Controls-on chat path for the alt-screen ACCEPTANCE gate
(test_e2e_raven_tui_chat). If THIS test fails, the chat layer is broken
regardless of TUI; if THIS passes but tui_chat fails, the bug is in the
TUI/RPC/Ink path.

Like the TUI acceptance test, this asserts the chat PIPELINE is alive (a turn
runs through and the app exits cleanly), NOT any specific model output —
asserting a particular answer is non-deterministic.

Currently xfail(strict): `raven chat` is not a registered top-level subcommand
(the CLI exposes channels / cron / provider / sandbox / sentinel / skill / tui /
sessions, no `chat`). The body is written as a liveness round-trip so that the
day a `chat` REPL lands, strict-xfail flips this to a real failure and the test
becomes a live liveness check with no further edits.

Requires an accessible default model configured locally (`~/.raven/config.json`);
without one the run skips rather than fails.
"""

from __future__ import annotations

import re
import time

import pytest

from tests.tui.autotest.runner import BackendError

_PROMPT = "Reply with a short friendly sentence."

# Working-state verbs mirrored from ui-tui/src/content/verbs.ts (VERBS); the
# CLI REPL shares the turn-state vocabulary. Keep in sync with that pool.
_WORKING_RE = re.compile(
    r"\b(pondering|contemplating|musing|cogitating|ruminating|deliberating|"
    r"mulling|reflecting|processing|reasoning|analyzing|computing|"
    r"synthesizing|formulating|brainstorming)…",
    re.IGNORECASE,
)
_READY_RE = re.compile(r"\bready\b", re.IGNORECASE)


@pytest.mark.e2e
@pytest.mark.xfail(
    reason=(
        "`raven chat` top-level subcommand does not exist — the CLI exposes "
        "channels / cron / provider / sandbox / sentinel / skill / tui / "
        "sessions, with no `chat` REPL. Live chat ACCEPTANCE for now is "
        "`test_e2e_raven_tui_chat.py` (TUI alt-screen). strict=True ensures "
        "this test starts FAILing the day a `chat` subcommand lands."
    ),
    strict=True,
)
def test_chat_cli_round_trip(harness):
    harness.spawn("uv run raven chat")
    # Wait for chat REPL prompt; the exact glyph may vary but ❯ is the
    # prompt-toolkit-style indicator we saw during spike.
    assert harness.wait(r"❯|>", timeout=15.0), f"chat REPL prompt not ready in 15s; screen=\n{harness.screen()}"

    harness.type(_PROMPT)
    harness.press("enter")

    # Race the working state against model_not_available from t=0 — a blocking
    # skip-probe would blind us to a transient working phase.
    started = False
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        screen = harness.screen()
        if re.search(r"error:\s*model_not_available", screen):
            pytest.skip(
                "default model returned model_not_available — the pipeline "
                "could not run a turn; configure an accessible default model "
                "and re-run."
            )
        if _WORKING_RE.search(screen):
            started = True
            break
        time.sleep(0.2)
    assert started, (
        "pipeline liveness failed: no turn started (status never entered a "
        f"working state) within 20s of submitting.\nscreen=\n{harness.screen()}"
    )
    assert harness.wait(_READY_RE, timeout=60.0), (
        "pipeline liveness failed: the turn never completed (status did not "
        f"return to ready) within 60s.\nscreen=\n{harness.screen()}"
    )

    # Turn settled; exit via the standard double Ctrl+C (first clears input,
    # second exits).
    harness.press("ctrl+c")
    time.sleep(0.5)
    try:
        harness.press("ctrl+c")
    except BackendError:
        pass  # already exiting after the first Ctrl+C
    assert harness.expect_exit(0, timeout=10.0), (
        f"chat CLI did not exit 0 after Ctrl+C; final screen=\n{harness.screen()}"
    )
