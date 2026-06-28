"""Internal helper: build a boxlite runtime rooted at raven's data dir.

Every boxlite usage inside raven (BoxliteExecutor, SandboxDebugServer)
goes through this helper so that the runtime's home_dir (DB, images, layers)
lives under <data_dir>/sandbox/boxlite rather than the boxlite default of
~/.boxlite.

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

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import boxlite as _boxlite_t

_runtime_cache: dict[tuple[int, str], Any] = {}


def get_boxlite_runtime() -> "_boxlite_t.Boxlite":
    import boxlite

    from raven.config.paths import get_sandbox_dir

    home = str(get_sandbox_dir("boxlite"))
    key = (id(boxlite.Boxlite), home)
    rt = _runtime_cache.get(key)
    if rt is None:
        rt = boxlite.Boxlite(boxlite.Options(home_dir=home))
        _runtime_cache[key] = rt
    return rt
