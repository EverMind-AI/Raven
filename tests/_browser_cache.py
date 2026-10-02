"""Where Playwright keeps the Chromium a real-page test needs, by platform.

Two things about that look-up are easy to get wrong, and both were, in two
different files:

* Playwright resolves its browser cache relative to ``HOME``, and this suite
  redirects ``HOME`` at a per-test tmp dir (``tests/conftest.py``, so no test
  reads the config of whoever is running it). A real-page test therefore has to
  point the look-up back at the login's cache itself.
* The default location is per-OS -- ``~/Library/Caches/ms-playwright`` on
  macOS, ``%LOCALAPPDATA%``-shaped on Windows, ``~/.cache/ms-playwright`` on
  Linux -- and a fixture that hard-codes one of them is right on exactly one
  platform and, worse, right for whoever wrote it.

An explicit ``PLAYWRIGHT_BROWSERS_PATH`` always wins over both, so CI that
installs its browsers elsewhere is untouched by any of this.
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

    ``pwd`` rather than ``Path.home()`` because the latter follows ``HOME``,
    which is the value every caller here is trying to see past.
    """
    if pwd is not None:
        try:
            return pwd.getpwuid(os.getuid()).pw_dir
        except (KeyError, AttributeError):  # pragma: no cover - no passwd entry
            pass
    return os.path.expanduser("~")


def playwright_cache(home: str) -> str:
    """The directory ``playwright install chromium`` writes to under ``home``."""
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Caches", "ms-playwright")
    if sys.platform == "win32":
        return os.path.join(home, "AppData", "Local", "ms-playwright")
    return os.path.join(home, ".cache", "ms-playwright")


def browsers_dir() -> str:
    """The browser cache this process resolves to. Safe to call at import.

    Not the same question as "is Chromium installed": this is where the
    look-up goes, which depends on the environment and not on the disk.
    """
    return os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or playwright_cache(login_home())


def chromium_installed(where: str | None = None) -> bool:
    """Whether a Chromium the launcher would accept is under ``where``.

    A directory entry rather than ``Browser.probe()``, which only answers
    whether the ``playwright`` package imports: those are different questions,
    and skipping on the package alone turns a missing binary into six failures.
    """
    target = where or browsers_dir()
    try:
        return any(name.startswith("chromium") for name in os.listdir(target))
    except OSError:
        return False


def point_at_login_cache(monkeypatch) -> None:
    """Fixture body: aim Playwright at the login's cache, unless already aimed.

    Set before the launch rather than after, because the sandboxed ``HOME`` is
    already in place by the time a fixture runs and the first ``goto`` is what
    starts Chromium.
    """
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", playwright_cache(login_home()))


__all__ = [
    "browsers_dir",
    "chromium_installed",
    "login_home",
    "playwright_cache",
    "point_at_login_cache",
]
