"""The relay: what the host's ssh runs on the machine, bridging its session to the daemon.

``python -I -m raven.node --attach --dir D`` connects to the daemon's socket
under ``D`` and copies bytes both ways until either side closes -- the ssh
session on one side, the daemon on the other. It parses nothing. When no
daemon answers it starts one, detached from this session so the daemon
outlives it, and waits for the socket to come up. So the host never has to
install or start a service: the first relay after a reboot brings the daemon
back, and the daemon leaves on its own once idle.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path

from raven.node import server

#: How long a relay waits for a daemon it started to open its socket.
START_TIMEOUT_S = 20.0
_POLL_S = 0.2
_BLOCK = 65536


def start_daemon(root: Path, *, python: str | None = None) -> subprocess.Popen[bytes]:
    """Start the daemon under ``root`` in a session of its own; its words go to ``log/serve.log``."""
    log = server.own_file(server.own_dir(root / server.LOG_DIR) / "serve.log")
    argv = [python or sys.executable, "-I", "-m", "raven.node", "--serve", "--dir", str(root)]
    # How long the daemon stays idle is the daemon's default, unless the
    # environment the relay runs in says otherwise (tests end theirs quickly).
    if idle := os.environ.get("RAVEN_NODE_IDLE_S", "").strip():
        argv += ["--idle", idle]
    try:
        return subprocess.Popen(  # noqa: S603 - this install's own Python, no caller input
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
            cwd=str(Path.home()),
        )
    finally:
        log.close()


async def connect(
    root: Path, *, python: str | None = None, timeout_s: float = START_TIMEOUT_S
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """A connection to the daemon under ``root``, started here when none answers.

    The socket's directory is this user's own (:func:`raven.node.server.own_dir`),
    so what listens there is this user's daemon; a thing at the socket's place
    that is somebody else's is not forwarded to, and no daemon is started
    beside it.
    """
    sock = server.socket_path(root)
    server.own_dir(sock.parent)
    deadline = time.monotonic() + timeout_s
    started = False
    while True:
        if not server.owned(sock):
            raise ConnectionError(f"{sock} is not this user's own; the relay forwards to this user's Raven daemon only")
        try:
            return await asyncio.open_unix_connection(str(sock), limit=_BLOCK)
        except (FileNotFoundError, ConnectionRefusedError, OSError):
            if not started:
                start_daemon(root, python=python)
                started = True
            elif time.monotonic() > deadline:
                raise ConnectionError(f"the Raven daemon under {root} did not answer within {timeout_s:.0f}s") from None
            await asyncio.sleep(_POLL_S)


async def bridge(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, stdin: asyncio.StreamReader, out: object
) -> None:
    """Copy bytes between the session (``stdin``/``out``) and the daemon until either side ends."""

    async def inward() -> None:
        while True:
            data = await stdin.read(_BLOCK)
            if not data:
                return
            writer.write(data)
            await writer.drain()

    async def outward() -> None:
        while True:
            data = await reader.read(_BLOCK)
            if not data:
                return
            out.write(data)  # type: ignore[attr-defined]
            out.flush()  # type: ignore[attr-defined]

    pumps = [asyncio.create_task(inward()), asyncio.create_task(outward())]
    try:
        await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for pump in pumps:
            pump.cancel()
        with contextlib.suppress(OSError, ConnectionError):
            writer.close()
            await writer.wait_closed()


def run_attach(root: Path) -> int:
    """Bridge this process's stdin and stdout to the daemon under ``root``; 1 when it cannot be reached."""
    out = sys.stdout.buffer
    sys.stdout = sys.stderr

    async def main() -> int:
        try:
            reader, writer = await connect(root)
        except (ConnectionError, PermissionError) as exc:
            sys.stderr.write(f"{exc}\n")
            return 1
        loop = asyncio.get_running_loop()
        stdin = asyncio.StreamReader(limit=_BLOCK)
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(stdin), sys.stdin)
        await bridge(reader, writer, stdin, out)
        return 0

    return asyncio.run(main())


__all__ = ["START_TIMEOUT_S", "bridge", "connect", "run_attach", "start_daemon"]
