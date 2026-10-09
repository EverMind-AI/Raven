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
import types
from collections.abc import Iterator

import pytest

from tests import _browser_cache
from tests._browser_cache import (
    browsers_dir,
    chromium_installed,
    chromium_launch_failure,
    login_home,
    playwright_cache,
    point_at_login_cache,
)


@pytest.fixture(autouse=True)
def _fresh_launch_probe() -> Iterator[None]:
    """The probe answers once per process; each test here asks it afresh."""
    chromium_launch_failure.cache_clear()
    yield
    chromium_launch_failure.cache_clear()


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
    so a missing binary failed the real-page tests instead of skipping them."""
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


def _launches(monkeypatch: pytest.MonkeyPatch, *, raising: str | None = None) -> list[str | None]:
    """Stand in for the bare launch; record the cache each one was aimed at."""
    seen: list[str | None] = []

    def launch() -> None:
        seen.append(os.environ.get("PLAYWRIGHT_BROWSERS_PATH"))
        if raising is not None:
            raise RuntimeError(raising)

    monkeypatch.setattr(_browser_cache, "_bare_launch", launch)
    return seen


# The shape a host missing a system library reports: the loader's own refusal
# inside Playwright's launch error, whose first line only says the browser
# closed.
_LOADER_REFUSAL = (
    "BrowserType.launch: Target page, context or browser has been closed\n"
    "[pid=7][err] /cache/chrome-headless-shell: error while loading shared libraries: "
    "libatk-1.0.so.0: cannot open shared object file: No such file or directory"
)


def test_a_browser_that_cannot_load_a_library_is_skipped_with_the_library_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _launches(monkeypatch, raising=_LOADER_REFUSAL)

    assert chromium_launch_failure() == "the system library libatk-1.0.so.0 is missing"


@pytest.mark.parametrize(
    "failure",
    [
        "BrowserType.launch: Executable doesn't exist at /cache/chromium_headless_shell-1240/chrome-headless-shell",
        "BrowserType.launch: Timeout 180000ms exceeded.",
    ],
    ids=["older-build-in-the-cache", "launch-under-load"],
)
def test_any_other_failure_is_left_for_the_suites_to_report(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    """Only a host's lack is a reason to skip. An older build left in the
    cache is promised a loud failure (``chromium_installed``), and a launch
    that failed under load is not the host's either; a skip would turn both
    into a green exit for every test in the file."""
    seen = _launches(monkeypatch, raising=failure)

    assert chromium_launch_failure() is None
    assert len(seen) == 1, "the launch really was attempted"


def test_a_browser_that_starts_is_no_reason_to_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _launches(monkeypatch)

    assert chromium_launch_failure() is None
    assert len(seen) == 1, "the launch really was attempted"


def test_the_probe_looks_where_the_suites_will_and_restores_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It runs before the suites' own fixture aims Playwright, so it aims the
    launch itself, at the same cache -- and hands the environment back as it
    found it, unset or set."""
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    seen = _launches(monkeypatch)
    chromium_launch_failure()
    assert seen == [playwright_cache(login_home())]
    assert "PLAYWRIGHT_BROWSERS_PATH" not in os.environ

    chromium_launch_failure.cache_clear()
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/chosen/ms-playwright")
    chromium_launch_failure()
    assert seen[-1] == os.path.abspath("/chosen/ms-playwright")
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "/chosen/ms-playwright"


async def test_the_probe_answers_from_inside_a_running_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """An async fixture or test may be the one asking, and ``asyncio.run``
    refuses to start inside a running loop. The probe would then never launch
    at all, and a host missing a library would fail every test rather than
    skip them. That refusal is not something the host lacks, so the probe
    answers None either way -- which is why the launch itself is asserted."""
    launched: list[str] = []

    class _Browser:
        async def close(self) -> None:
            pass

    class _Chromium:
        async def launch(self, **kwargs: object) -> _Browser:
            launched.append("headless" if kwargs.get("headless") else "headful")
            return _Browser()

    class _Playwright:
        chromium = _Chromium()

        async def __aenter__(self) -> _Playwright:
            return self

        async def __aexit__(self, *exc: object) -> None:
            pass

    fake_api = types.ModuleType("playwright.async_api")
    fake_api.async_playwright = _Playwright  # type: ignore[attr-defined]
    fake_pkg = types.ModuleType("playwright")
    fake_pkg.async_api = fake_api  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", fake_pkg)
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_api)

    assert chromium_launch_failure() is None
    assert launched == ["headless"]


def test_the_probe_starts_a_browser_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _launches(monkeypatch, raising=_LOADER_REFUSAL)

    first, second = chromium_launch_failure(), chromium_launch_failure()

    assert first == second == "the system library libatk-1.0.so.0 is missing"
    assert len(seen) == 1
