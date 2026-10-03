"""Where Playwright keeps the Chromium a real-page test needs.

This suite redirects ``HOME`` at a per-test tmp dir (``tests/conftest.py``,
so no test reads the config of whoever is running it), and Playwright looks
its browsers up from the home directory -- so a real-page test has to aim the
look-up back at the login's own cache before it launches anything.

The rule mirrored here is ``registryDirectory`` in playwright-core's registry,
read from the driver bundled with playwright 1.62:

* ``PLAYWRIGHT_BROWSERS_PATH=0`` -- a hermetic install, inside the package;
* any other value of it -- that directory, a relative one taken from
  ``INIT_CWD`` or the working directory;
* otherwise ``ms-playwright`` under a per-OS cache root: ``XDG_CACHE_HOME``
  or ``~/.cache`` on Linux, ``~/Library/Caches`` on macOS, ``LOCALAPPDATA`` or
  ``~/AppData/Local`` on Windows. Playwright refuses every other platform.

Only the home directory needs correcting: the two environment variables are
not sandboxed, so where one is set Playwright already finds the cache.

A browser in the cache is not yet one this host can run -- a missing system
library stops it at launch -- so ``chromium_launch_failure`` starts one and
names the library it could not load.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import os
import sys

try:
    import pwd
except ImportError:  # pragma: no cover - Windows has no pwd module
    pwd = None  # type: ignore[assignment]


def login_home() -> str:
    """The login directory, ignoring a ``HOME`` a fixture has sandboxed.

    ``pwd`` rather than ``Path.home()``, which follows ``HOME`` -- the value
    every caller here is trying to see past. Without a passwd entry there is
    nothing to see past it with, and ``HOME`` is the answer.
    """
    if pwd is not None:
        try:
            return pwd.getpwuid(os.getuid()).pw_dir
        except KeyError:
            pass
    return os.path.expanduser("~")


def playwright_cache(home: str) -> str | None:
    """The default browser cache for ``home``, or None where Playwright has none."""
    if sys.platform == "linux":
        root = os.environ.get("XDG_CACHE_HOME") or os.path.join(home, ".cache")
    elif sys.platform == "darwin":
        root = os.path.join(home, "Library", "Caches")
    elif sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    else:
        return None
    return os.path.join(root, "ms-playwright")


def browsers_dir() -> str | None:
    """The browser cache this process resolves to. Safe to call at import."""
    explicit = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if explicit == "0":
        try:
            import playwright
        except ImportError:
            return None
        return os.path.join(os.path.dirname(playwright.__file__), "driver", "package", ".local-browsers")
    if explicit:
        return os.path.abspath(os.path.join(os.environ.get("INIT_CWD") or os.getcwd(), explicit))
    return playwright_cache(login_home())


def chromium_installed(where: str | None = None) -> bool:
    """Whether a Chromium build sits in the browser cache.

    A directory entry rather than ``Browser.probe()``, which only answers
    whether the ``playwright`` package imports: skipping on the package alone
    turned a missing binary into failures. It does not check the revision
    -- a cache holding only an older build still fails the launch, loudly, and
    the driver's error then names the path it looked for.
    """
    target = where or browsers_dir()
    if target is None:
        return False
    try:
        return any(name.startswith("chromium") for name in os.listdir(target))
    except OSError:
        return False


def _bare_launch() -> None:
    """Start and stop a headless Chromium with Playwright alone; raises on failure.

    On a worker thread with a loop of its own, because a caller may already be
    inside a running event loop, where ``asyncio.run`` refuses to start.
    """
    from playwright.async_api import async_playwright

    async def launch() -> None:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            await browser.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(lambda: asyncio.run(launch())).result()


@functools.cache
def chromium_launch_failure() -> str | None:
    """The system library this host lacks for a headless Chromium, or None.

    A directory in the cache says a browser was downloaded, not that this host
    can run it: one missing a system library it links against fails every test
    at launch, and no change to the code under test can fix that. Any other
    failure answers None, so the suites run and fail on it loudly -- an older
    build left in the cache (see ``chromium_installed``) or a launch that
    failed under load is not something this host lacks. Started with Playwright
    alone, not through raven's driver, so what it finds is the host's by
    construction. Asked once per process, since it costs a browser start, and
    aimed at the cache the suites' own fixture will point at, because this runs
    before that fixture does.
    """
    from raven.browser.driver import _host_library_gap

    target = browsers_dir()
    if target is None:
        return None
    saved = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = target
    try:
        _bare_launch()
    except Exception as exc:  # noqa: BLE001 - a host's lack is named, anything else is no reason to skip
        return _host_library_gap(str(exc))
    finally:
        if saved is None:
            os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
        else:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = saved
    return None


def point_at_login_cache(monkeypatch) -> None:
    """Fixture body: aim Playwright at the login's cache, unless already aimed."""
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return
    cache = playwright_cache(login_home())
    if cache is not None:
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", cache)


__all__ = [
    "browsers_dir",
    "chromium_installed",
    "chromium_launch_failure",
    "login_home",
    "playwright_cache",
    "point_at_login_cache",
]
