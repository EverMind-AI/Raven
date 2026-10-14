"""Register the CLI's furniture into the rpc console socket.

The discovery lived in ``rpc/_console_injection`` when rpc could import the
cli; under the surfaces law the cli enumerates ITSELF and hands the results
over. Discovered rather than listed, for the reason the original recorded:
the hand-kept list drifted to 12 of 22 modules, and a command module outside
it printed past the TUI.
"""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType

from rich.console import Console

import raven.cli


def _console_host_modules() -> tuple[ModuleType, ...]:
    hosts: list[ModuleType] = []
    for info in pkgutil.iter_modules(raven.cli.__path__):
        if info.name.startswith("__"):
            continue
        mod = importlib.import_module(f"raven.cli.{info.name}")
        if isinstance(getattr(mod, "console", None), Console):
            hosts.append(mod)
    return tuple(sorted(hosts, key=lambda m: m.__name__))


def register_console_feature() -> None:
    """Idempotent; every entrance that mounts a console calls this at assembly."""
    import raven.cli.commands as commands
    from raven.cli._theme import _can_probe
    from raven.rpc import cli_socket

    # Finding the hosts imports every CLI module, and one of them asks the terminal
    # for its colours as it is imported. A host with no terminal to ask, such as the
    # gateway `raven web` detaches, finds them on its first console command; one
    # that shares a terminal, as the TUI's host does with the Ink screen, still
    # asks now, before the session starts.
    cli_socket.register_cli(commands, _console_host_modules() if _can_probe() else _console_host_modules)
