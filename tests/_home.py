"""A test's home directory, on every platform.

Raven reads a home two ways: through ``Path.home`` / ``os.path.expanduser``
(the ssh alias helpers in :mod:`raven.ops.connection_add` open
``~/.ssh/config`` that way) and through ``HOME`` itself. On Windows the
first way reads ``USERPROFILE`` and ignores ``HOME`` (``ntpath.expanduser``,
since Python 3.8), so a test that moves only ``HOME`` keeps writing into
the profile of whoever runs it -- on a native Windows run, the operator's
own ``~/.ssh/config``. :func:`point_home` moves both.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def point_home(monkeypatch: pytest.MonkeyPatch, path: Path | str) -> Path:
    """Make ``path`` the home every reader sees, for the test that holds ``monkeypatch``."""
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))
    return Path(path)


__all__ = ["point_home"]
