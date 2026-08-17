"""E2E smoke: real tui-use spawns /bin/cat — verifies framework basic flow
without depending on raven or network."""

from __future__ import annotations

import pytest


@pytest.mark.e2e
def test_cat_echoes_typed_input(harness):
    harness.spawn("/bin/cat")
    harness.type("hello-autotest")
    harness.press("enter")
    assert harness.wait(r"hello-autotest", timeout=3.0), (
        f"cat did not echo input within 3s; screen=\n{harness.screen()}"
    )
    harness.press("ctrl+d")
    assert harness.expect_exit(0, timeout=3.0)
