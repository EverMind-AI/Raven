"""The Playwright browser-cache look-up the real-page suites share.

Both of those suites need the cache under the *login's* home, because the
suite redirects ``HOME`` at a per-test tmp dir and Playwright resolves the
cache from ``HOME``. Getting it wrong is not loud: the run reports Chromium
missing and suggests an install that would write somewhere else again.
"""

from __future__ import annotations

import os
import sys

import pytest

from tests._browser_cache import browsers_dir, chromium_installed, login_home, playwright_cache


def test_the_cache_is_named_per_platform() -> None:
    """The three defaults Playwright documents. Hard-coding one of them is
    right on exactly one OS, and right for whoever wrote it."""
    assert playwright_cache("/home/u") == os.path.join("/home/u", ".cache", "ms-playwright")
    if sys.platform == "darwin":
        assert playwright_cache("/Users/u") == "/Users/u/Library/Caches/ms-playwright"
        pytest.skip("the other two branches are the two below, checked by the platform not taken")
    if sys.platform == "win32":
        assert playwright_cache("C:/Users/u") == os.path.join("C:/Users/u", "AppData", "Local", "ms-playwright")


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("darwin", "Library/Caches/ms-playwright"),
        ("win32", "AppData/Local/ms-playwright"),
        ("linux", ".cache/ms-playwright"),
    ],
)
def test_every_branch_of_the_platform_switch_is_reachable(monkeypatch, platform, expected) -> None:
    """Which branch runs is `sys.platform`, so exercise all three rather than
    only the one this machine happens to be."""
    monkeypatch.setattr(sys, "platform", platform)

    assert playwright_cache("/h") == os.path.join("/h", expected)


def test_the_login_home_ignores_a_sandboxed_home(monkeypatch) -> None:
    """`Path.home()` and `expanduser` both follow `HOME`, which is the value
    every caller here is trying to see past."""
    monkeypatch.setenv("HOME", "/tmp/sandboxed")

    home = login_home()

    assert home != "/tmp/sandboxed"
    assert os.path.isdir(home), "and it is a real directory, not a guess"


def test_an_explicit_cache_path_wins(monkeypatch) -> None:
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/elsewhere/ms-playwright")

    assert browsers_dir() == "/elsewhere/ms-playwright"


def test_the_skip_only_fires_when_there_is_no_chromium(monkeypatch, tmp_path) -> None:
    """The old skip asked `Browser.probe()`, which only tests the `playwright`
    package -- so a missing binary failed six tests instead of skipping them."""
    empty = tmp_path / "ms-playwright"
    empty.mkdir()
    assert chromium_installed(str(empty)) is False

    (empty / "chromium-1234").mkdir()
    assert chromium_installed(str(empty)) is True

    (empty / "chromium_headless_shell-1234").mkdir()
    assert chromium_installed(str(empty)) is True

    assert chromium_installed(str(tmp_path / "absent")) is False, "a missing directory is not an install"
