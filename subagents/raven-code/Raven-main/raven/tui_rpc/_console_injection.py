"""Monkey-patch helper for swapping EC CLI modules' module-level ``console``.

Why monkey-patch rather than refactor:
- 4 EC CLI modules each define module-level ``console = Console()``; commands
  resolve ``console.print(...)`` via module name lookup.
- Refactoring all command signatures to accept ``console`` is ~40 file edits;
  monkey-patch is 4 ``setattr`` calls.

grep risk verification:

    $ grep -rn "from .* import console" raven/
    (no matches)

    $ grep -rn "Console(" raven/ | grep -v _console_injection.py | grep -v tui_rpc
    raven/cli/sandbox_commands.py:19:console = Console()       ← patched
    raven/cli/_cron_inspector.py:51:console = Console()        ← patched
    raven/cli/channel_commands.py:31:console = Console()       ← patched
    raven/cli/commands.py:60:console = Console()               ← patched
    raven/utils/helpers.py:205:Console().print(...)            ← NOT patched

Result: 4 hosts confirmed. ``utils/helpers.py:205`` is a fresh ``Console()``
inside ``sync_workspace_templates()``; it's only reachable from ``init`` /
workspace-template setup paths, none of which are in v0.1 dispatch whitelist
(no whitelisted command calls ``sync_workspace_templates``). If a future
whitelist entry depends on it, refactor or expand ``_CONSOLE_HOSTS``.

A later merge brought 8 new CLI modules with module-level
``console = Console()``; the patch list extends from 4 to 12.
``_cron_inspector`` was renamed to ``cron_commands``.

    Re-grep after merge:
    $ grep -rn "^console = Console" raven/cli/
    raven/cli/_helpers.py:22         ← patched (NEW)
    raven/cli/agent_commands.py:40    ← patched (NEW)
    raven/cli/channel_commands.py:38  ← patched (already)
    raven/cli/commands.py:58          ← patched (already)
    raven/cli/cron_commands.py:51     ← patched (renamed from _cron_inspector)
    raven/cli/gateway_commands.py:29  ← patched (NEW)
    raven/cli/onboard_commands.py:12  ← patched (NEW)
    raven/cli/provider_commands.py:36 ← patched (NEW)
    raven/cli/sandbox_commands.py:19  ← patched (already)
    raven/cli/sentinel_commands.py:33 ← patched (NEW)
    raven/cli/skill_commands.py:37    ← patched (NEW)
    raven/cli/status_commands.py:10   ← patched (NEW)

Total: 12 hosts. All exposed via ``_CONSOLE_HOSTS``.

Concurrency: this context manager is NOT internally locked. The cli.dispatch
handler holds a module-level ``asyncio.Lock`` to serialize calls (Q7 risk #4).
Putting the lock here would make ``inject_consoles`` async, which complicates
the ``with``-block ergonomics inside the handler's ``redirect_stdout`` chain.
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
