"""E2E smoke: real tui-use spawns /bin/cat — verifies framework basic flow
without depending on raven or network."""

from __future__ import annotations

import time

import pytest

_LINE = "hello-autotest"


@pytest.mark.e2e
def test_cat_echoes_typed_input(harness):
    harness.spawn("/bin/cat")
    harness.type(_LINE)
    harness.press("enter")

    # One copy is the tty echoing the keystrokes, so waiting for the text only
    # proves key delivery. cat writing the line back makes a second copy, which
    # is the part this test is named for.
    echoed_twice = False
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if harness.screen().count(_LINE) >= 2:
            echoed_twice = True
            break
        time.sleep(0.1)
    assert echoed_twice, (
        f"{_LINE!r} appears {harness.screen().count(_LINE)} time(s); expected the tty echo "
        f"plus cat's own copy; screen=\n{harness.screen()}"
    )

    harness.press("ctrl+d")
    assert harness.expect_exit(0, timeout=3.0)
