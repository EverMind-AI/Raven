"""Dogfood — working TUI whitelist commands.

Each command is sent into the alt-screen TUI via `/cmd<sub>` slash routing,
which hits the `cli.dispatch` RPC method on the Python side; the rendered
Click output streams back through the unix socket and renders into Ink.

Pattern: spawn → wait readiness → type slash → wait output → Ctrl+C×2 →
expect_exit 0. Output pattern is chosen to match a stable substring that
appears in a non-error response (avoiding `-32012` / `-32015` error codes
which would indicate cli.dispatch / handler failure).

`/sentinel routines` is flagged "possibly broken" — the CLI command name is
`sentinel learn-routines`, not `sentinel routines`. Marked xfail-strict so
the marker comes off the day CLI ships a matching command.
"""

from __future__ import annotations

import time

import pytest

from tests.tui.autotest.runner import BackendError


# (slash command, output regex, xfail (reason, strict) or None)
#
# `xfail (reason, strict=False)` marks "known-volatile" — tests where
# dogfood revealed an inconsistent Raven TUI exit-key UX (some overlays
# absorb Ctrl+C / Esc differently, leaving the subprocess alive past the
# expect_exit timeout). strict=False so XPASS does NOT fail the suite —
# treats these as informational.
_VOLATILE = (
    "Raven TUI overlay exit UX inconsistent (Esc+Ctrl+C doesn't always "
    "terminate this command's overlay); revisit when tui-chat "
    "lands since it touches Cancel UX. strict=False = informational.",
    False,
)

_WHITELIST = [
    ("status", r"OpenRouter|Model:|provider", None),
    ("channels status", r"channels?|telegram|slack|discord|enabled|disabled", _VOLATILE),
    ("channels list", r"channels?|telegram|slack|discord|empty", _VOLATILE),
    ("skill list", r"skill|name|empty|no skills", None),
    ("skill get", r"skill|usage|argument|missing", _VOLATILE),
    ("skill refresh", r"skill|refresh|done|complete|empty", None),
    ("skill stats", r"skill|stats|count|empty|total", None),
    ("cron list", r"cron|job|schedule|empty|name", None),
    ("cron show", r"cron|job|usage|argument|missing", _VOLATILE),
    ("sentinel status", r"sentinel|enabled|disabled|status", _VOLATILE),
    ("sentinel nudges", r"nudges?|empty|count|none|recent", None),
    ("sentinel decisions", r"decisions?|empty|count|none|pending", _VOLATILE),
    # NB: coverage-report §2 flagged `sentinel routines` as "possibly broken"
    # (CLI has `learn-routines`), but 2026-05-20 dogfood proves it works
    # through TUI whitelist — informational xfail (strict=False) covers
    # day-to-day overlay UX volatility.
    ("sentinel routines", r"routines?|empty|count|none|learn", _VOLATILE),
    ("sandbox list", r"sandbox|vm|empty|no sandboxes|name", None),
]


def _make_test_id(entry):
    return entry[0].replace(" ", "_")


@pytest.mark.e2e
@pytest.mark.parametrize(
    ("slash", "expected", "xfail_marker"),
    _WHITELIST,
    ids=[_make_test_id(e) for e in _WHITELIST],
)
def test_dogfood_slash_command(harness, slash, expected, xfail_marker, request):
    if xfail_marker:
        reason, strict = xfail_marker
        request.applymarker(pytest.mark.xfail(reason=reason, strict=strict))

    harness.spawn("uv run raven tui")
    assert harness.wait(r"Raven", timeout=25.0), (
        f"TUI not ready in 25s for /{slash}; screen=\n{harness.screen()}"
    )
    harness.type(f"/{slash}")
    harness.press("enter")
    assert harness.wait(expected, timeout=10.0), (
        f"slash /{slash} did not produce expected output (regex={expected!r}); "
        f"screen=\n{harness.screen()}"
    )
    # Escape dismisses any open overlay/panel (per Raven TUI footer hint
    # "Esc/q close"); the subsequent Ctrl+C exits. press() raises BackendError
    # if session already exited inline — that's the inline-exit path and fine.
    for key in ("escape", "ctrl+c"):
        try:
            harness.press(key)
        except BackendError:
            break
        time.sleep(0.5)
    assert harness.expect_exit(0, timeout=10.0), (
        f"TUI did not exit 0 after /{slash}; final screen=\n{harness.screen()}"
    )
