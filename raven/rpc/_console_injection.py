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
from collections.abc import Iterator

from rich.console import Console

import raven.cli._helpers as ec_helpers
import raven.cli.agent_commands as ec_agent
import raven.cli.channel_commands as ec_channel
import raven.cli.commands as ec_commands
import raven.cli.cron_commands as ec_cron
import raven.cli.gateway_commands as ec_gateway
import raven.cli.onboard_commands as ec_onboard
import raven.cli.provider_commands as ec_provider
import raven.cli.sandbox_commands as ec_sandbox
import raven.cli.sentinel_commands as ec_sentinel
import raven.cli.skill_commands as ec_skill
import raven.cli.status_commands as ec_status

# Order is irrelevant (each module is patched independently); kept stable for
# readable test introspection.
_CONSOLE_HOSTS: tuple = (
    ec_commands,
    ec_sandbox,
    ec_channel,
    ec_cron,
    ec_agent,
    ec_gateway,
    ec_onboard,
    ec_provider,
    ec_sentinel,
    ec_skill,
    ec_status,
    ec_helpers,
)


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
