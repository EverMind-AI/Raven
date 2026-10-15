"""Register the CLI's furniture into the rpc console socket.

The discovery lived in ``rpc/_console_injection`` when rpc could import the
cli; under the surfaces law the cli enumerates ITSELF and hands over the
results, or, where they can wait for the first console command, the
enumerator. Discovered rather than listed, for the reason the original
recorded: the hand-kept list drifted to 12 of 22 modules, and a command module
outside it printed past the TUI.
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
    # for its colours as it is imported. Where the terminal cannot be asked -- none,
    # as for the gateway `raven web` detaches, or Windows, SSH, screen or tmux, CI --
    # they are found on the first console command; where it can, as from the TUI's
    # host, the question is asked now, before the Ink screen starts.
    cli_socket.register_cli(commands, _console_host_modules() if _can_probe() else _console_host_modules)
