"""The Playwright browser-cache look-up the real-page suites share.

Those suites need the cache Playwright would use for the *login's* home: the
suite redirects ``HOME`` at a per-test tmp dir, and Playwright resolves the
cache from the home directory. Getting it wrong is quiet -- the run reports
Chromium missing and suggests an install that writes somewhere else again --
so the cases below are the branches of Playwright's own rule, not a list of
the platforms someone happened to test on.
"""

from __future__ import annotations

import os
import sys

import pytest

from tests._browser_cache import (
    browsers_dir,
    chromium_installed,
    login_home,
    playwright_cache,
    point_at_login_cache,
)


@pytest.mark.parametrize(
    ("platform", "env", "expected"),
    [
        pytest.param("linux", {}, ("<home>", ".cache", "ms-playwright"), id="linux"),
        pytest.param("linux", {"XDG_CACHE_HOME": "/xdg"}, ("/xdg", "ms-playwright"), id="linux-xdg"),
        pytest.param("darwin", {}, ("<home>", "Library", "Caches", "ms-playwright"), id="darwin"),
        pytest.param("win32", {}, ("<home>", "AppData", "Local", "ms-playwright"), id="win32"),
        pytest.param("win32", {"LOCALAPPDATA": "/lad"}, ("/lad", "ms-playwright"), id="win32-localappdata"),
        pytest.param("sunos5", {}, None, id="a-platform-playwright-refuses"),
    ],
)
def test_the_default_cache_follows_playwright_s_rule(
    monkeypatch: pytest.MonkeyPatch, platform: str, env: dict[str, str], expected: tuple[str, ...] | None
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    for name in ("XDG_CACHE_HOME", "LOCALAPPDATA"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    got = playwright_cache("/h")

    if expected is None:
        assert got is None
    else:
        assert got == os.path.join(*("/h" if part == "<home>" else part for part in expected))


def test_an_explicit_cache_path_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/elsewhere/ms-playwright")

    assert browsers_dir() == os.path.abspath("/elsewhere/ms-playwright")


def test_a_relative_cache_path_is_taken_from_init_cwd_or_the_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", os.path.join("rel", "pw"))
    monkeypatch.delenv("INIT_CWD", raising=False)
    monkeypatch.chdir(tmp_path)
    assert browsers_dir() == str(tmp_path / "rel" / "pw")

    monkeypatch.setenv("INIT_CWD", str(tmp_path / "init"))
    assert browsers_dir() == str(tmp_path / "init" / "rel" / "pw")


def test_a_hermetic_install_is_looked_for_inside_the_package(monkeypatch: pytest.MonkeyPatch) -> None:
    playwright = pytest.importorskip("playwright")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "0")

    assert browsers_dir() == os.path.join(
        os.path.dirname(playwright.__file__), "driver", "package", ".local-browsers"
    ), "a literal '0' is a directory name to nothing but Playwright"


def test_the_login_home_sees_past_a_sandboxed_home(monkeypatch: pytest.MonkeyPatch) -> None:
    pwd = pytest.importorskip("pwd")
    try:
        real = pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        pytest.skip("this account has no passwd entry, so HOME is all there is to read")
    monkeypatch.setenv("HOME", "/tmp/sandboxed")

    assert login_home() == real


def test_the_fixture_aims_playwright_at_the_login_cache_unless_already_aimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    point_at_login_cache(monkeypatch)
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == playwright_cache(login_home())

    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/chosen")
    point_at_login_cache(monkeypatch)
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "/chosen", "a caller's own choice is left alone"


def test_a_chromium_directory_is_what_counts_as_installed(tmp_path) -> None:
    """The old skip asked ``Browser.probe()``, which only tests the package,
    so a missing binary failed six tests instead of skipping them."""
    cache = tmp_path / "ms-playwright"
    cache.mkdir()
    (cache / "ffmpeg-1011").mkdir()
    assert chromium_installed(str(cache)) is False, "another download is not a browser"

    (cache / "chromium_headless_shell-1234").mkdir()
    assert chromium_installed(str(cache)) is True

    assert chromium_installed(str(tmp_path / "absent")) is False


def test_a_platform_playwright_refuses_counts_as_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "sunos5")
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)

    assert chromium_installed() is False
    point_at_login_cache(monkeypatch)
    assert "PLAYWRIGHT_BROWSERS_PATH" not in os.environ
