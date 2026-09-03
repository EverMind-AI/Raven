"""Run a block as if the ``everos-memory`` distribution were installed or not.

The dev environment installs the plugin, so the only way to test what a host
surface does without it is to take it away for the duration of one block. The
``None`` sentinel in ``sys.modules`` is what does that: ``import raven_everos``
raises, and ``importlib.util.find_spec`` answers ``None`` -- the same two
answers a machine that never installed the plugin gives.

:func:`everos_plugin_broken` is the other half of the pair, and the reason the
host asks about presence with a spec lookup rather than a ``try: import``: the
package is there, importing part of it fails, and that must reach the caller as
the plugin's own error rather than as an absence.

Deliberately not a stub of the host's own presence check: a test that patches
``everos_plugin_installed`` proves the guard branches, not that the import it
guards would have failed.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Iterator
from types import ModuleType

_PACKAGE = "raven_everos"


def _evict() -> dict[str, ModuleType]:
    """Take every ``raven_everos`` module out of ``sys.modules``, keeping them.

    Submodules go too: an already-imported ``raven_everos.health`` is handed
    straight back without the parent being consulted, and the parent is the one
    under test.
    """
    saved = {name: mod for name, mod in sys.modules.items() if name == _PACKAGE or name.startswith(f"{_PACKAGE}.")}
    for name in saved:
        del sys.modules[name]
    return saved


@contextlib.contextmanager
def everos_plugin_absent() -> Iterator[None]:
    """Make every ``raven_everos`` import fail for the duration of the block."""
    saved = _evict()
    sys.modules[_PACKAGE] = None  # type: ignore[assignment]
    try:
        yield
    finally:
        sys.modules.pop(_PACKAGE, None)
        sys.modules.update(saved)


@contextlib.contextmanager
def everos_plugin_broken() -> Iterator[None]:
    """Installed, and unable to hand out any of its modules."""
    saved = _evict()
    sys.modules[_PACKAGE] = ModuleType(_PACKAGE)
    try:
        yield
    finally:
        sys.modules.pop(_PACKAGE, None)
        sys.modules.update(saved)
