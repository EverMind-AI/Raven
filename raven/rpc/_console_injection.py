"""Swap the module-level ``console`` of the CLI command modules for one call.

Each command module defines ``console = Console()`` and resolves
``console.print(...)`` by module-global lookup, so ``cli.dispatch`` can point
them at a buffer-backed console with ``setattr`` instead of threading a console
through every command signature. ``_CONSOLE_HOSTS`` lists the modules swapped.

Not internally locked: the ``cli.dispatch`` handler holds a module-level
``asyncio.Lock`` and serializes calls, which keeps the ``with`` block
synchronous inside the handler's ``redirect_stdout`` chain.
"""

import contextlib
from collections.abc import Iterator

from rich.console import Console

from raven.rpc import cli_socket


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
    hosts = cli_socket.console_hosts()
    originals = {mod: mod.console for mod in hosts}
    try:
        for mod in hosts:
            mod.console = out_console
        yield
    finally:
        for mod, orig in originals.items():
            mod.console = orig


__all__ = ["inject_consoles"]
