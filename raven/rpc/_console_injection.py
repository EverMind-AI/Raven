"""Swap the module-level ``console`` of the CLI command modules for one call.

Each command module defines ``console = Console()`` and resolves
``console.print(...)`` by module-global lookup, so ``cli.dispatch`` can point
them at a buffer-backed console with ``setattr`` instead of threading a console
through every command signature. ``_CONSOLE_HOSTS`` lists the modules swapped.

Not internally locked: the ``cli.dispatch`` handler holds a module-level
``asyncio.Lock`` and serializes calls, which keeps the ``with`` block
synchronous inside the handler's ``redirect_stdout`` chain.
"""

from __future__ import annotations

import contextlib
import importlib
import pkgutil
from collections.abc import Iterator
from types import ModuleType

from rich.console import Console

import raven.cli


def _console_hosts() -> tuple[ModuleType, ...]:
    """Every module under ``raven.cli`` whose module-level ``console`` is a rich
    Console. Discovered rather than listed: the list drifted to 12 of 22 while it
    was maintained by hand, and a command module outside it printed past the TUI."""
    hosts: list[ModuleType] = []
    for info in pkgutil.iter_modules(raven.cli.__path__):
        if info.name.startswith("__"):
            continue
        mod = importlib.import_module(f"raven.cli.{info.name}")
        if isinstance(getattr(mod, "console", None), Console):
            hosts.append(mod)
    return tuple(sorted(hosts, key=lambda m: m.__name__))


_CONSOLE_HOSTS: tuple[ModuleType, ...] = _console_hosts()


@contextlib.contextmanager
def inject_consoles(out_console: Console) -> Iterator[None]:
    """Temporarily replace module-level ``console`` on all EC CLI modules.

    Args:
        out_console: the Rich ``Console`` instance that EC CLI commands will
            write to for the duration of the context. Typically constructed
            with ``file=StringIO(), force_terminal=True, color_system="truecolor",
            width=<TUI-supplied>``.

    On exit, the original ``console`` reference is restored regardless of how
    the context body terminated (normal / exception / generator close).

    Note: there is only ONE ``console`` per host module — stderr is captured
    out-of-band by the handler's ``contextlib.redirect_stderr(stderr_buf)``
    wrapping this context. The optional ``err_console`` parameter was dropped
    because none of the hosts use a separate stderr Console; revisit if a
    future version introduces ``Console(stderr=True)`` instances.
    """
    originals = {mod: mod.console for mod in _CONSOLE_HOSTS}
    try:
        for mod in _CONSOLE_HOSTS:
            mod.console = out_console
        yield
    finally:
        for mod, orig in originals.items():
            mod.console = orig


__all__ = ["inject_consoles", "_CONSOLE_HOSTS"]
