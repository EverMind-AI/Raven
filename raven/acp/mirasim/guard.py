"""``python -m raven.acp.mirasim.guard -- <argv>``: one ``ui-cli`` command that cannot outlive the adapter.

Raven tears an ACP connection down with ``SIGKILL`` to the adapter's process
group (``AcpClient.close`` in ``raven/acp_client/client.py``), which no handler
in the adapter can see. A ``ui-cli`` in that group would die with it and leave
the agent Mirasim started -- in a session of its own -- running a turn nobody
will read, in the caller's working directory. So the adapter starts each command under
this guard, in a session of the guard's own, and holds the guard's stdin open.
The kernel closes that pipe when the adapter goes, however it goes; the guard
then ends the command and everything it started
(:func:`raven.acp.mirasim.uicli.end_tree`). A cancel closes the pipe on purpose.

The command's output is relayed rather than inherited, for two reasons. A
process the command started that inherited its output -- a helper left running
in the background -- would otherwise hold the adapter's pipes open after the
command is done, and the turn's answer with them: the adapter reads to the end
of the output, which does not come while any copy of the pipe is open. And
``ui-cli`` would write to the adapter's own pipes, so the adapter's death could
reach it as a failed write in the instant before it reached the guard as end
of file; a ``ui-cli`` that dies of that leaves its agent outside any tree the
guard can read.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import threading

from raven.acp.mirasim.uicli import end_tree, exit_status

DRAIN_S = 1.0
"""How long output is still relayed after the command has exited or been ended.

What is left in a pipe then drains in far less; a process that inherited the
command's output can hold the pipe open for as long as it lives, and the
answer is not kept waiting for it."""

_CHUNK = 64 * 1024


def _watch_hangup(fd: int, loop: asyncio.AbstractEventLoop, hangup: asyncio.Event) -> None:
    """Block on ``fd`` until it closes, then set ``hangup``."""
    try:
        while os.read(fd, _CHUNK):
            pass
    except OSError:
        pass
    with contextlib.suppress(RuntimeError):
        loop.call_soon_threadsafe(hangup.set)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


async def _relay(source: asyncio.StreamReader, fd: int) -> None:
    """Copy ``source`` to ``fd``, and keep draining it once nobody reads ``fd``."""
    delivering = True
    while chunk := await source.read(_CHUNK):
        if delivering:
            try:
                _write_all(fd, chunk)
            except OSError:
                delivering = False


async def guard(argv: list[str], *, stdin_fd: int = 0, stdout_fd: int = 1, stderr_fd: int = 2) -> int:
    """Run ``argv`` until it exits or ``stdin_fd`` closes, and answer with its exit status."""
    loop = asyncio.get_running_loop()
    hangup = asyncio.Event()
    threading.Thread(target=_watch_hangup, args=(stdin_fd, loop, hangup), daemon=True).start()
    with contextlib.suppress(NotImplementedError, RuntimeError):
        loop.add_signal_handler(signal.SIGTERM, hangup.set)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        with contextlib.suppress(OSError):
            _write_all(stderr_fd, f"cannot start {argv[0]!r}: {exc}\n".encode())
        return 127
    assert proc.stdout is not None and proc.stderr is not None  # noqa: S101 - create_subprocess_exec(PIPE) contract
    relays = asyncio.gather(_relay(proc.stdout, stdout_fd), _relay(proc.stderr, stderr_fd))
    exited = asyncio.ensure_future(exit_status(proc))
    stopped = asyncio.ensure_future(hangup.wait())
    await asyncio.wait({exited, stopped}, return_when=asyncio.FIRST_COMPLETED)
    if not exited.done():
        await end_tree(proc)
    stopped.cancel()
    try:
        await asyncio.wait_for(relays, DRAIN_S)
    except asyncio.TimeoutError:
        pass
    code = await exited
    return code if code >= 0 else 128 - code


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["--"]:
        args = args[1:]
    if not args:
        print("usage: python -m raven.acp.mirasim.guard -- <command> [arg ...]", file=sys.stderr)
        return 2
    return asyncio.run(guard(args))


if __name__ == "__main__":
    raise SystemExit(main())
