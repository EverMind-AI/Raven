"""Where every raven process starts: the console script and ``python -m raven``.

Which certificates the process trusts is settled here, before anything else is
imported, because the CLI's own imports already build TLS contexts: the
sub-agent backends import aiohttp, which makes its default context on import.
How a Windows process encodes its stdout and stderr is settled here too, so the
one launch that skips the command table writes them as every other does.
"""

from __future__ import annotations

import os
import sys


def run() -> None:
    """Console-script entry point.

    Two launches are taken without the command table. The supervisor `raven web`
    leaves running only restarts the gateway, and whatever it imports stays
    resident for as long as the page is up. The fork template loads what Raven
    agents load and forks them, so what it imports is exactly what it chooses.
    """
    from raven.security.tls import use_system_ca

    use_system_ca()
    _write_utf8_on_windows()

    if sys.argv[1:3] == ["web", "--supervise"]:
        from raven.cli.serve_commands import supervise_from_argv

        if supervise_from_argv(sys.argv[1:]):
            return

    if sys.argv[1:2] == ["--fork-template"]:
        from raven.cli._fork_template import main as serve_forks

        raise SystemExit(serve_forks(sys.argv[2:]))

    from raven.cli.commands import run as run_commands

    run_commands()


def _write_utf8_on_windows() -> None:
    """Write stdout and stderr in UTF-8 on Windows, and have child processes do the same.

    Redirected to a file or a pipe -- the ``web.log`` a supervisor shares with its
    gateway is one -- Python writes them in the ANSI code page there, which
    refuses any character outside it.
    """
    if sys.platform != "win32" or sys.stdout.encoding == "utf-8":
        return
    os.environ["PYTHONIOENCODING"] = "utf-8"
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
