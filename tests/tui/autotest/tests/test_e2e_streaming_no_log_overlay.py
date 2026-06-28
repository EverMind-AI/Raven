"""E2E regression guard: TUI chat streaming must not leak log lines onto alt-screen.

During chat token-by-token streaming, log lines (LiteLLM verbose
DEBUG traces) print to the TUI terminal and overlay Ink rendering. Resize-window
recovers (Ink full redraw).

The mechanism observed at trunk 9fdf139: LiteLLM's "LiteLLM" / "LiteLLM Router" /
"LiteLLM Proxy" named loggers install their own ``<StreamHandler <stderr>>`` at
``import litellm`` time. ``_redirect_loguru_to_file`` installs an InterceptHandler
at the root stdlib logger via ``basicConfig(level=0, force=True)`` — root NOTSET
means ``isEnabledFor`` returns True for every child, so DEBUG records propagate.
The named-logger's own stderr StreamHandler fires per chunk regardless of root
configuration; ``litellm_core_utils/streaming_handler.py:1001`` emits one DEBUG
``model_response.choices[0].delta`` per SSE event, which bleeds straight to the
inherited terminal during alt-screen rendering.

Test strategy: race the leak-pattern regex against the streaming response. If a
log-line marker appears in the alt-screen frame within 30 s of pressing enter,
the bug is reproduced — assertion fails with the captured frame for triage.
"""

from __future__ import annotations

import re
import time

import pytest

from tests.tui.autotest.runner import BackendError

_LEAK_RE = re.compile(
    r"LiteLLM:(DEBUG|INFO|WARNING|ERROR)"
    r"|model_response\.choices\[\d+\]\.delta"
    r"|Request to litellm:"
    r"|litellm\.acompletion\("
    r"|\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3}\s*\|\s*(DEBUG|INFO|WARNING|ERROR)\s*\|"
)


@pytest.mark.e2e
def test_tui_chat_streaming_no_log_overlay(harness):
    harness.spawn("uv run raven tui")
    assert harness.wait(r"Raven", timeout=25.0), (
        f"TUI did not reach banner; screen=\n{harness.screen()}"
    )
    harness.type("Reply in exactly 30 words about anything.")
    harness.press("enter")

    # If the configured default model isn't accessible (e.g. claude-sonnet-4-6
    # without an API key on this host), the chat never streams — the leak
    # patterns can't fire. Skip so a vacuous pass doesn't mask a real bug.
    if harness.wait(r"error:\s*model_not_available", timeout=5.0):
        pytest.skip(
            "Default model returned model_not_available — streaming did not "
            "start, cannot validate log-overlay absence. Re-run with an "
            "accessible model configured as default (e.g. openrouter/qwen)."
        )

    # Race the leak pattern against the streaming response. If a log line
    # surfaces on the alt-screen at any moment in the next 30 s, fail with
    # the captured frame so the fix author can see the exact leaked text.
    leak_detected = harness.wait(_LEAK_RE, timeout=30.0)
    captured_frame = harness.screen()

    # Clean cancel regardless of leak / no-leak (mirrors test_e2e_raven_tui_chat).
    for key in ("escape", "ctrl+c"):
        try:
            harness.press(key)
        except BackendError:
            break
        time.sleep(0.5)
    harness.expect_exit(0, timeout=10.0)

    assert not leak_detected, (
        "Log lines leaked onto TUI alt-screen during chat streaming.\n"
        f"Leak pattern: {_LEAK_RE.pattern!r}\n\n"
        f"Captured frame:\n{captured_frame}"
    )
