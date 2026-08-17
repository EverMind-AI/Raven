"""CLI subcommands for sandbox VM inspection and interaction."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from raven.sandbox._async_utils import cancel_and_collect as _cancel_and_collect

console = Console()
logger = logging.getLogger(__name__)


class _SocketClosedError(Exception):
    """Raised when the debug server closes the connection without sending a response."""


sandbox_app = typer.Typer(
    name="sandbox",
    help="Inspect and interact with sandbox VMs (requires sandbox.debug=true).",
    no_args_is_help=True,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_socket_path() -> Path:
    """Resolve the debug socket path from config (falls back to defaults)."""
    from raven.config.paths import get_data_dir
    from raven.sandbox.debug_server import SandboxDebugServer

    debug_socket = "sandbox/debug.sock"
    try:
        from raven.config.loader import load_config

        cfg = load_config()
        debug_socket = cfg.tools.sandbox.debug.socket
    except FileNotFoundError:
        # No config file at the expected path — use the default socket.
        pass
    except Exception as exc:
        # Config exists but failed to load/parse. Don't crash the CLI, but
        # warn loudly so the user can correlate with a wrong socket lookup.
        logger.warning("Failed to load sandbox debug config (%s); using default socket path", exc)

    return SandboxDebugServer.resolve_socket_path(debug_socket, get_data_dir())


def _check_socket(path: Path) -> None:
    """Exit with a clear error if the socket file is missing or inaccessible."""
    if not path.exists():
        console.print(f"[red]Debug socket not found at {path}.[/red]")
        console.print(
            "[dim]Is raven running with sandbox.debug.enabled=true? "
            "(If it is, the debug server may have failed to start — check the "
            "agent logs/output for a '[Sandbox debug]' message.)[/dim]"
        )
        raise typer.Exit(1)
    if not os.access(path, os.R_OK | os.W_OK):
        console.print(f"[red]Cannot connect to debug socket at {path}: permission denied[/red]")
        raise typer.Exit(1)


async def _connect(path: Path) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open a connection to the debug socket."""
    try:
        return await asyncio.open_unix_connection(str(path))
    except (ConnectionRefusedError, FileNotFoundError, OSError) as exc:
        console.print(f"[red]Cannot connect to debug socket at {path}: {exc}[/red]")
        raise typer.Exit(1) from exc


async def _send(writer: asyncio.StreamWriter, obj: dict) -> None:
    writer.write((json.dumps(obj) + "\n").encode())
    await writer.drain()


async def _recv(reader: asyncio.StreamReader) -> dict:
    line = await reader.readline()
    if not line:
        raise _SocketClosedError("server closed connection without sending a response")
    try:
        return json.loads(line.decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # The framing is internal between client and server, so this should
        # never happen in practice — but if it does (truncated line, garbage,
        # protocol mismatch), surface it as a clean error rather than a
        # traceback in the user's terminal.
        raise _SocketClosedError(f"server sent malformed response: {exc}") from exc


def _close(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# list / ls
# ---------------------------------------------------------------------------


def _run_list() -> None:
    socket_path = _get_socket_path()
    _check_socket(socket_path)

    async def _do() -> None:
        reader, writer = await _connect(socket_path)
        try:
            await _send(writer, {"cmd": "list"})
            try:
                msg = await _recv(reader)
            except _SocketClosedError as exc:
                console.print(f"[red]Error: {exc}[/red]")
                raise typer.Exit(1) from exc
        finally:
            _close(writer)

        if msg.get("type") == "error":
            console.print(f"[red]Error: {msg.get('message')}[/red]")
            raise typer.Exit(1)

        vms = msg.get("vms", [])
        if not vms:
            console.print("[dim]No VMs found.[/dim]")
            return

        table = Table(title="Sandbox VMs")
        table.add_column("", style="bold", no_wrap=True)  # owned marker
        table.add_column("ID", style="cyan", no_wrap=True)
        # table.add_column("Name")  # VMs are not named today; restore when naming is supported
        table.add_column("State")
        table.add_column("Image")
        table.add_column("CPUs", justify="right")
        table.add_column("Mem MiB", justify="right")
        table.add_column("Created At")

        for vm in vms:
            owned_marker = "[green]*[/green]" if vm.get("owned") else "[dim]-[/dim]"
            status = vm.get("status", "")
            status_styled = f"[green]{status}[/green]" if status == "running" else f"[dim]{status}[/dim]"
            created = (vm.get("created_at") or "")[:19].replace("T", " ")
            table.add_row(
                owned_marker,
                vm.get("id", ""),
                # vm.get("name") or "",  # VMs are not named today; restore when naming is supported
                status_styled,
                vm.get("image", ""),
                str(vm.get("cpus", "")),
                str(vm.get("memory_mib", "")),
                created,
            )
        console.print(table)

    asyncio.run(_do())


@sandbox_app.command("list")
def sandbox_list() -> None:
    """List all sandbox VMs (owned VMs marked with *)."""
    _run_list()


@sandbox_app.command("ls")
def sandbox_ls() -> None:
    """Alias for 'list'."""
    _run_list()


# ---------------------------------------------------------------------------
# exec
# ---------------------------------------------------------------------------


@sandbox_app.command("exec", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def sandbox_exec(
    ctx: typer.Context,
    vm: str | None = typer.Option(None, "--vm", help="Target VM — ID or name (auto-select if only one running VM)."),
) -> None:
    """Run a command inside a sandbox VM and stream its output."""
    cmd_args = ctx.args
    if not cmd_args:
        console.print("[red]Error: a command to execute is required.[/red]")
        console.print("Usage: raven sandbox exec [--vm VM_REF] CMD [ARG ...]")
        raise typer.Exit(1)

    program, *args = cmd_args
    socket_path = _get_socket_path()
    _check_socket(socket_path)

    async def _do() -> int:
        reader, writer = await _connect(socket_path)
        # Buffer for line-aware stderr prefixing: chunks aren't guaranteed
        # to be line-aligned (boxlite may split a single line across
        # chunks, or coalesce many lines into one), so prefix per "\n" line
        # rather than per chunk to avoid mid-line "[stderr] " markers.
        stderr_buf = bytearray()

        def _emit_stderr(chunk: bytes) -> None:
            stderr_buf.extend(chunk)
            while True:
                nl = stderr_buf.find(b"\n")
                if nl < 0:
                    break
                sys.stderr.buffer.write(b"[stderr] " + bytes(stderr_buf[: nl + 1]))
                del stderr_buf[: nl + 1]
            sys.stderr.buffer.flush()

        def _flush_stderr_tail() -> None:
            # Flush any final partial line (no trailing newline) so it isn't
            # silently dropped when the VM process exits between writes.
            if stderr_buf:
                sys.stderr.buffer.write(b"[stderr] " + bytes(stderr_buf))
                sys.stderr.buffer.write(b"\n")
                sys.stderr.buffer.flush()
                stderr_buf.clear()

        try:
            await _send(
                writer,
                {
                    "cmd": "exec",
                    "vm_ref": vm,
                    "program": program,
                    "args": args,
                },
            )
            exit_code = 1
            while True:
                try:
                    msg = await _recv(reader)
                except _SocketClosedError as exc:
                    _flush_stderr_tail()
                    console.print(f"[red]Error: {exc}[/red]")
                    return 1
                mtype = msg.get("type")
                if mtype == "stdout":
                    data = base64.b64decode(msg["data"])
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                elif mtype == "stderr":
                    _emit_stderr(base64.b64decode(msg["data"]))
                elif mtype == "exit":
                    _flush_stderr_tail()
                    exit_code = msg.get("code", 0)
                    break
                elif mtype == "error":
                    _flush_stderr_tail()
                    console.print(f"[red]Error: {msg.get('message')}[/red]")
                    exit_code = 1
                    break
            return exit_code
        finally:
            _close(writer)

    code = asyncio.run(_do())
    raise typer.Exit(code)


# ---------------------------------------------------------------------------
# shell
# ---------------------------------------------------------------------------


@sandbox_app.command("shell")
def sandbox_shell(
    vm: str | None = typer.Option(None, "--vm", help="Target VM — ID or name (auto-select if only one running VM)."),
    shell_path: str = typer.Option("/bin/sh", "--shell", help="Shell binary inside the VM."),
) -> None:
    """Open an interactive shell inside a sandbox VM."""
    import fcntl
    import signal
    import struct
    import termios
    import tty

    socket_path = _get_socket_path()
    _check_socket(socket_path)

    async def _do() -> int:
        reader, writer = await _connect(socket_path)
        try:
            await _send(writer, {"cmd": "shell", "vm_ref": vm, "shell": shell_path})

            # Wait for ready or error before entering raw mode
            try:
                first = await _recv(reader)
            except _SocketClosedError as exc:
                console.print(f"[red]Error: {exc}[/red]")
                return 1
            if first.get("type") == "error":
                console.print(f"[red]Error: {first.get('message')}[/red]")
                return 1
            if first.get("type") != "ready":
                console.print(f"[red]Unexpected server response: {first}[/red]")
                return 1

            # Save terminal state and enter raw mode
            fd = sys.stdin.fileno()
            old_attrs = termios.tcgetattr(fd)

            def _restore():
                try:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
                except Exception:
                    pass

            tty.setraw(fd)

            exit_code = [1]
            done = asyncio.Event()
            loop = asyncio.get_running_loop()
            stdin_queue: asyncio.Queue[bytes | None] = asyncio.Queue()

            def _on_readable() -> None:
                # Fired by the event loop when fd becomes readable; we're already
                # in the loop thread, so put_nowait is safe to call directly.
                try:
                    chunk = os.read(fd, 4096)
                    stdin_queue.put_nowait(chunk if chunk else None)
                except OSError:
                    stdin_queue.put_nowait(None)

            loop.add_reader(fd, _on_readable)

            async def _do_send_resize(rows: int, cols: int) -> None:
                # Wrapped so a broken-pipe error during shutdown can't surface
                # as 'Task exception was never retrieved' on GC.
                try:
                    await _send(writer, {"cmd": "resize", "rows": rows, "cols": cols})
                except Exception:
                    pass

            def _send_resize():
                try:
                    winsz = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\x00" * 8)
                    rows, cols = struct.unpack("HHHH", winsz)[:2]
                    loop.create_task(_do_send_resize(rows, cols))
                except Exception:
                    pass

            # SIGWINCH may fire on any thread context; bounce through the loop
            # so create_task / writer access happen in the loop thread.
            original_sigwinch = signal.getsignal(signal.SIGWINCH)
            signal.signal(
                signal.SIGWINCH,
                lambda *_: loop.call_soon_threadsafe(_send_resize),
            )

            # Send initial terminal size
            _send_resize()

            async def _recv_loop():
                try:
                    while True:
                        msg = await _recv(reader)
                        mtype = msg.get("type")
                        if mtype == "stdout":
                            data = base64.b64decode(msg["data"])
                            sys.stdout.buffer.write(data)
                            sys.stdout.buffer.flush()
                        elif mtype == "exit":
                            exit_code[0] = msg.get("code", 0)
                            break
                        elif mtype == "error":
                            _restore()
                            console.print(f"\r\n[red]Error: {msg.get('message')}[/red]")
                            break
                except _SocketClosedError as exc:
                    # Distinguish a server-side disconnect from a clean exit so
                    # the user sees *why* the shell ended (agent crashed, server
                    # restarted, malformed protocol, …) instead of a silent close.
                    _restore()
                    console.print(f"\r\n[red]Error: {exc}[/red]")
                except Exception as exc:
                    # Anything else (KeyError on a malformed-but-valid-JSON payload,
                    # base64 decode failure, etc.) is a real bug — show enough info
                    # for a follow-up report instead of silently dropping the user
                    # back to a closed terminal with exit code 1.
                    _restore()
                    console.print(f"\r\n[red]Internal error in sandbox shell: {type(exc).__name__}: {exc}[/red]")
                    logger.exception("sandbox shell recv loop failed")
                finally:
                    done.set()

            async def _stdin_loop():
                # Race each queue.get() against done.wait() so cancellation is
                # immediate — no 50 ms polling lag, no wasted wake-ups.
                done_task = asyncio.create_task(done.wait())
                try:
                    while True:
                        get_task = asyncio.create_task(stdin_queue.get())
                        finished, _ = await asyncio.wait(
                            {get_task, done_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if done_task in finished:
                            await _cancel_and_collect(get_task)
                            break
                        chunk = get_task.result()
                        if not chunk:
                            # Local stdin reached EOF (e.g. piped input ended,
                            # or terminal closed). Forward a final EOT byte
                            # (\x04) so the VM-side PTY's line discipline sees
                            # VEOF and terminates the shell — matches `docker
                            # exec -it` behavior on Ctrl-D / pipe end.
                            data = base64.b64encode(b"\x04").decode()
                            try:
                                await _send(writer, {"cmd": "stdin", "data": data})
                            except (ConnectionResetError, BrokenPipeError):
                                pass
                            break
                        data = base64.b64encode(chunk).decode()
                        await _send(writer, {"cmd": "stdin", "data": data})
                except Exception:
                    pass
                finally:
                    await _cancel_and_collect(done_task)

            try:
                recv_task = asyncio.create_task(_recv_loop())
                stdin_task = asyncio.create_task(_stdin_loop())
                await done.wait()
            finally:
                recv_task.cancel()
                stdin_task.cancel()
                await asyncio.gather(recv_task, stdin_task, return_exceptions=True)
                loop.remove_reader(fd)
                _restore()
                signal.signal(signal.SIGWINCH, original_sigwinch)

            return exit_code[0]
        finally:
            _close(writer)

    code = asyncio.run(_do())
    raise typer.Exit(code)
