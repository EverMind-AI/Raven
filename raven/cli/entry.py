"""Where every raven process starts: the console script and ``python -m raven``.

Which certificates the process trusts is settled here, before anything else is
imported, because the CLI's own imports already build TLS contexts: the
sub-agent backends import aiohttp, which makes its default context on import.
"""

from __future__ import annotations


def run() -> None:
    """Console-script entry point."""
    from raven.security.tls import use_system_ca

    use_system_ca()

    from raven.cli.commands import run as run_commands

    run_commands()
