"""Internal helper: build a boxlite runtime rooted at a home the caller names.

Every boxlite usage inside raven (BoxliteExecutor, SandboxDebugServer)
goes through this helper so that the runtime's home_dir (DB, images, layers)
lives where raven keeps its data rather than at the boxlite default of
~/.boxlite. Which directory that is belongs to the caller: this package is
told, so that resolving raven's data dir stays outside the sandbox.

The runtime is memoised per (Boxlite class, home_dir) because boxlite's Rust
core takes a process-wide filesystem lock per home_dir that is only released
when the ``Boxlite`` instance is dropped — building a fresh ``Boxlite`` on
every call would conflict with the still-alive previous instance and panic
with "Another BoxliteRuntime is already using directory: …".

The class object is part of the cache key (not just the home_dir) so that
unit tests which ``mock.patch("boxlite.Boxlite")`` get a fresh mocked runtime
on each patch instead of the cached real-Boxlite from a prior test.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import boxlite as _boxlite_t

_runtime_cache: dict[tuple[int, str], Any] = {}


def get_boxlite_runtime(home_dir: Path) -> "_boxlite_t.Boxlite":
    import boxlite

    home = str(home_dir)
    key = (id(boxlite.Boxlite), home)
    rt = _runtime_cache.get(key)
    if rt is None:
        rt = boxlite.Boxlite(boxlite.Options(home_dir=home))
        _runtime_cache[key] = rt
    return rt
