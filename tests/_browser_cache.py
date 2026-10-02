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
"""

from __future__ import annotations

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
    "login_home",
    "playwright_cache",
    "point_at_login_cache",
]
