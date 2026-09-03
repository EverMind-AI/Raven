"""The console feature's registration socket: what a hosting CLI lends.

``console.cli_dispatch`` runs CLI commands, ``commands.catalog`` reflects
them, and the console injection swaps the modules they print through. All
three need the CLI's own furniture -- and rpc must not import the cli (the
surfaces law) -- so the hosting entrance registers it here at assembly: the
register-first idiom of the ask_user broker and ``bind_runtime``. The slot
holds the MODULE, not the app object, so a test that monkeypatches
``raven.cli.commands.app`` keeps its seam: every reader resolves ``.app`` at
call time.

Unregistered -- an acp host, a bare test server, any process that never
mounts a console -- the feature degrades to its existing graceful paths:
dispatch answers not-compatible, the catalog answers empty with a warning.
"""

from __future__ import annotations

from types import ModuleType

_commands_module: ModuleType | None = None
_console_hosts: tuple[ModuleType, ...] = ()


def register_cli(commands_module: ModuleType, console_hosts: tuple[ModuleType, ...] = ()) -> None:
    """Called by the hosting entrance at assembly; idempotent."""
    global _commands_module, _console_hosts
    _commands_module = commands_module
    _console_hosts = tuple(console_hosts)


def cli_commands() -> ModuleType | None:
    """The registered CLI commands module, or ``None`` on a console-less host."""
    return _commands_module


def console_hosts() -> tuple[ModuleType, ...]:
    """The modules whose ``console`` the injection swaps; empty when unhosted."""
    return _console_hosts


def reset() -> None:
    """Tests only: return the socket to the unregistered state."""
    global _commands_module, _console_hosts
    _commands_module = None
    _console_hosts = ()


__all__ = ["cli_commands", "console_hosts", "register_cli", "reset"]
