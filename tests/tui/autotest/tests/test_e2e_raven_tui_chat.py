"""E2E ⭐ ACCEPTANCE: `raven tui` alt-screen chat pipeline liveness.

If this passes, the harness can drive a full Ink alt-screen TUI through
RPC + streaming + slash routing + Ctrl+C autonomy — i.e., Claude Code can
independently reproduce any TUI bug from `Bash()`.

This asserts the chat PIPELINE is alive (a prompt is accepted, a turn runs
through RPC + agent-loop + streaming, and the app exits cleanly), NOT any
specific model output. Asserting the model produced a particular answer (its
own name, a fact, a colour) is non-deterministic and therefore an illegitimate
e2e assertion — the model may or may not self-name, and any factual answer can
vary run to run.

Liveness is read from the status bar's turn state (working -> ready), which is
content-agnostic and robust: the Ink alt-screen redraws the entire frame each
tick (welcome art, borders, side panel), so a naive screen-text delta reports
chrome as if it were a reply. The turn-state cycle proves the pipeline ran the
turn regardless of what — or whether — the model rendered any text.

Requires:
- `tui-use` >=0.1.20 on PATH (npm install -g tui-use)
- Built `ui-tui/dist/entry.js` (npm install + npm run build in ui-tui/)
- An accessible default model configured (else the run skips, not fails)

Ink Ctrl+C autonomy yields exit 0, NOT 130. expect_exit(0) is correct.
"""

from __future__ import annotations

import re
import time

import pytest

from tests.tui.autotest.raven_ux import READY_RE as _READY_RE
from tests.tui.autotest.raven_ux import WORKING_RE as _WORKING_RE
from tests.tui.autotest.raven_ux import exit_tui

# Content-neutral prompt: we never assert WHAT the model says, only that the
# pipeline ran the turn.
_PROMPT = "Reply with a short friendly sentence."


@pytest.mark.e2e
def test_tui_chat_round_trip(harness):
    # turn.send/turn.subscribe + SubscriptionEmitter are wired; this test
    # runs as a regular live E2E ACCEPTANCE for the chat streaming path
    # through the TUI.
    harness.spawn("uv run raven tui")
    # Wait for the status bar to report ready, not merely for the banner: the
    # banner paints seconds before the app accepts input, so keying off it drops
    # the prompt and the turn never starts.
    assert harness.wait(_READY_RE, timeout=45.0), (
        f"TUI status bar never reported ready in 45s; screen=\n{harness.screen()}"
    )

    harness.type(_PROMPT)
    # Confirm the composer actually received the text before submitting: typing
    # and pressing enter back to back can outrun the render, and the dropped
    # prompt then looks like a dead pipeline.
    assert harness.wait(re.escape(_PROMPT), timeout=10.0), (
        f"composer never showed the typed prompt; screen=\n{harness.screen()}"
    )
    harness.press("enter")

    # Liveness, content-agnostic: the pipeline accepts the prompt (status bar
    # enters a working state) and completes the turn (status returns to ready).
    # Race the working state against model_not_available from t=0 — a blocking
    # skip-probe would blind us to a working phase that starts and ends inside
    # its window (the status verb is transient).
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
        "pipeline liveness failed: no turn started (status bar never entered a "
        f"working state) within 20s of submitting.\nscreen=\n{harness.screen()}"
    )
    assert harness.wait(_READY_RE, timeout=60.0), (
        "pipeline liveness failed: the turn never completed (status bar did not "
        f"return to ready) within 60s.\nscreen=\n{harness.screen()}"
    )

    exit_tui(harness)
    assert harness.expect_exit(0, timeout=10.0), f"TUI did not exit 0 after Ctrl+C; final screen=\n{harness.screen()}"


def _await_ready(harness) -> None:
    """Block until the status bar reports an idle turn state."""
    assert harness.wait(_READY_RE, timeout=45.0), (
        f"TUI status bar never reported ready in 45s; screen=\n{harness.screen()}"
    )


def _run_turn(harness, prompt: str) -> None:
    """Submit one prompt and return once the turn has started and settled."""
    harness.type(prompt)
    assert harness.wait(re.escape(prompt), timeout=10.0), (
        f"composer never showed {prompt!r}; screen=\n{harness.screen()}"
    )
    harness.press("enter")

    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        screen = harness.screen()
        if re.search(r"error:\s*model_not_available", screen):
            pytest.skip("default model returned model_not_available; configure an accessible model and re-run.")
        if _WORKING_RE.search(screen):
            break
        time.sleep(0.2)
    else:
        pytest.fail(f"turn never started for {prompt!r}; screen=\n{harness.screen()}")

    assert harness.wait(_READY_RE, timeout=60.0), f"turn never completed for {prompt!r}; screen=\n{harness.screen()}"


@pytest.mark.e2e
def test_tui_chat_multi_turn_accumulates_the_session(harness):
    """Three turns land in one session file, in order, each with a reply.

    The round-trip above proves one turn runs. This proves turns accumulate
    rather than each starting fresh. The evidence is the persisted session, not
    the screen: a prompt is echoed into the transcript as soon as it is typed,
    so screen-scraping for a planted word passes even when history is broken.
    """
    from raven.cli._helpers import load_runtime_config
    from raven.session.manager import SessionManager

    sessions = SessionManager(load_runtime_config(None, None).workspace_path)
    before = sessions.find_most_recent_chat_id("tui")

    harness.spawn("uv run raven tui")
    _await_ready(harness)

    prompts = [
        "Reply with just the word alpha.",
        "Reply with just the word bravo.",
        "Reply with just the word charlie.",
    ]
    for prompt in prompts:
        _run_turn(harness, prompt)

    exit_tui(harness)
    assert harness.expect_exit(0, timeout=10.0), f"TUI did not exit 0 after Ctrl+C; final screen=\n{harness.screen()}"

    # The run created a fresh tui session; it is now the most recent one.
    chat_id = sessions.find_most_recent_chat_id("tui")
    assert chat_id and chat_id != before, (
        f"the run did not create a new tui session (before={before!r}, after={chat_id!r})"
    )
    session = sessions.peek(f"tui:{chat_id}")
    assert session is not None, f"no persisted session for tui:{chat_id}"

    user_turns = [m.get("content", "") for m in session.get_history() if m.get("role") == "user"]
    for prompt in prompts:
        assert prompt in user_turns, f"turn missing from the persisted session: {prompt!r}; got {user_turns!r}"
    assert user_turns.index(prompts[0]) < user_turns.index(prompts[1]) < user_turns.index(prompts[2]), (
        f"turns did not accumulate in order: {user_turns!r}"
    )
    replies = [m for m in session.get_history() if m.get("role") == "assistant"]
    assert len(replies) >= len(prompts), (
        f"expected one assistant reply per turn, got {len(replies)} for {len(prompts)} prompts"
    )
