"""Find the native cairo library, and make it reachable while `cairosvg` is imported.

`cairosvg` draws SVG through `cairocffi`, which finds libcairo with
`ctypes.util.find_library` and then a bare `dlopen` by leaf name. On an Apple
silicon Mac neither looks where Homebrew puts it: brew installs into
`/opt/homebrew/lib`, and dyld's default fallback path is `~/lib:/usr/local/lib:
/lib:/usr/lib`. So `import cairosvg` raised `OSError` on a machine that had
`brew install cairo` done, and worked only when the process was started with
`DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib` -- which a user launching
`raven web` from their own terminal never sets.

Preloading the dylib by absolute path does not help: dyld does not match a bare
leaf name against an image already loaded. What does is the variable itself,
because `find_library` on macOS reads it from `os.environ` at call time and then
hands `cairocffi` an absolute path. It is set only around the import and put
back afterwards, so nothing the process spawns later inherits it.

Two callers: the ppt engine rasterises fetched SVG logos and imports `cairosvg`
inside `libcairo_reachable`, and `raven doctor` reports whether that can happen.
The import itself stays with the engine, the only thing that depends on
`cairosvg`.
"""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes.util import find_library
from pathlib import Path

# The names `cairocffi` asks `find_library` for, in its order.
_LIBRARY_NAMES = ("cairo-2", "cairo", "libcairo-2")
# Homebrew on Apple silicon, Homebrew on Intel, MacPorts.
_DARWIN_LIBRARY_DIRS = ("/opt/homebrew/lib", "/usr/local/lib", "/opt/local/lib")
_DARWIN_FILENAMES = ("libcairo.2.dylib", "libcairo.dylib")
_FALLBACK_VAR = "DYLD_FALLBACK_LIBRARY_PATH"

_import_lock = threading.Lock()


def _darwin_library_dir() -> str | None:
    for directory in _DARWIN_LIBRARY_DIRS:
        if any((Path(directory) / name).is_file() for name in _DARWIN_FILENAMES):
            return directory
    return None


def _found_by_default() -> str | None:
    return next((path for name in _LIBRARY_NAMES if (path := find_library(name))), None)


def libcairo_path() -> str | None:
    """The libcairo `cairosvg` would load, or None when there is none to load."""
    if (path := _found_by_default()) is not None:
        return path
    if sys.platform == "darwin" and (directory := _darwin_library_dir()) is not None:
        return next(str(Path(directory) / name) for name in _DARWIN_FILENAMES if (Path(directory) / name).is_file())
    return None


def install_hint() -> str:
    """The command that installs libcairo on this platform, bare so a caller can phrase it."""
    if sys.platform == "darwin":
        return "brew install cairo"
    if sys.platform == "win32":
        return "install the GTK runtime and put its bin directory on PATH"
    return "apt install libcairo2"


@contextmanager
def libcairo_reachable() -> Iterator[None]:
    """Hold a Homebrew or MacPorts lib directory in dyld's fallback path, then restore it.

    A no-op off macOS and wherever the default lookup already finds libcairo. The
    variable is process-wide, so the import that needs it runs under a lock and a
    user's own value is kept behind the added directory.
    """
    with _import_lock:
        directory = _darwin_library_dir() if sys.platform == "darwin" and _found_by_default() is None else None
        if directory is None:
            yield
            return
        previous = os.environ.get(_FALLBACK_VAR)
        os.environ[_FALLBACK_VAR] = os.pathsep.join(p for p in (directory, previous) if p)
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop(_FALLBACK_VAR, None)
            else:
                os.environ[_FALLBACK_VAR] = previous
