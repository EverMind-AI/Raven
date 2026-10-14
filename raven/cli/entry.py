"""Where every raven process starts: the console script and ``python -m raven``.

Which certificates the process trusts is settled here, before anything else is
imported, because the CLI's own imports already build TLS contexts: the
sub-agent backends import aiohttp, which makes its default context on import.
"""

from __future__ import annotations

import sys


def run() -> None:
    """Console-script entry point.

    The supervisor `raven web` leaves running is the one launch taken without the
    command table: it only restarts the gateway, and whatever it imports stays
    resident for as long as the page is up.
    """
    from raven.security.tls import use_system_ca

    use_system_ca()

    if sys.argv[1:3] == ["web", "--supervise"]:
        from raven.cli.serve_commands import supervise_from_argv

        if supervise_from_argv(sys.argv[1:]):
            return

    from raven.cli.commands import run as run_commands

    run_commands()
