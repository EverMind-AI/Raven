"""``raven tracing`` — open the tracing dashboard.

The dashboard is a dependency-free Node viewer bundled under
``raven/cli/tracing_viewer/`` -- a surface the CLI launches, kept out of the
``tracing`` kernel package. Instrumentation itself runs in-process (installed at
CLI startup, see :mod:`raven.tracing`); this command only launches the viewer
that reads the captured spans from ``~/.raven/traces``.

``raven tracing`` (bare) lazily starts the viewer if it is not already running,
then opens the browser. It reuses raven's own Node discovery (:func:`find_node`),
so it needs the same Node >= 22 that ``raven tui`` already requires.

The background viewer's pid + port are recorded in ``<state_dir>/viewer.pid``
so a later ``raven tracing`` reuses the live instance instead of stacking
orphans, and ``raven tracing stop`` can shut it down. The record is replaced
atomically -- otherwise a reader could catch a half-written file, read it as
"no record", and drop it -- and every read-and-delete of it runs under the same
lock as that replacement, so a record a concurrent launcher just wrote is never
the one deleted. The stop path only signals a pid whose command line still
matches the node viewer — a recycled pid is never killed.

Registered as a top-level leaf command (not a subcommand group) so the TUI
command catalog lists it as a plain ``/tracing`` slash under "(top-level)".
``stop`` and ``compact`` are optional positional actions; foreground mode, port
and ``--dry-run`` are options, not subcommands.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
import urllib.request
import webbrowser
from contextlib import suppress
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from raven.tracing import config as tracing_config

console = Console()

#: zstd decoding landed in Node's zlib in 22.15 and 23.8, and the viewer reads
#: payloads compressed by default. Its floor is therefore higher than the TUI's.
_MIN_VIEWER_NODE_VERSION = (22, 15, 0)
_FIRST_VIEWER_NODE_23_VERSION = (23, 8, 0)


def _node_supports_zstd(version: tuple[int, ...]) -> bool:
    """Whether this Node has zlib zstd: 22.15+, 23.8+, or any later major."""
    if version[0] == 23:
        return version >= _FIRST_VIEWER_NODE_23_VERSION
    return version >= _MIN_VIEWER_NODE_VERSION


def _viewer_dir() -> Path:
    return Path(__file__).resolve().parent / "tracing_viewer"


# Asset the viewer must still be able to read off disk for the page to work.
# Served by ``serveStatic``, unlike the health payload and HTML shell, which the
# process answers from memory and therefore survive their files being deleted.
_VIEWER_UI_PROBE = "/app.js"


def _port_live(port: int) -> bool:
    """True if something is already listening on 127.0.0.1:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _viewer_health(port: int) -> bool:
    """True only if *our* tracing viewer is serving on ``port`` and can still
    serve its UI.

    Two probes, because either one alone accepts a viewer that cannot render:

    * ``/api/health`` separates our viewer from an unrelated server (e.g.
      another observability tool) holding the port -- a foreign server does not
      answer ``{"ok": true}``.
    * ``_VIEWER_UI_PROBE`` separates a working viewer from one whose files were
      deleted from under it. The health payload and the HTML shell are both
      answered from memory, so a viewer whose install directory was replaced
      (a version upgrade, a switch to an editable install) keeps passing the
      first probe indefinitely while every asset read 404s -- the page then
      renders unstyled and never connects.
    """
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.5) as resp:
            if resp.status != 200:
                return False
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 — any failure means "not our viewer"
        return False
    if not (isinstance(data, dict) and data.get("ok") is True):
        return False
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{_VIEWER_UI_PROBE}", timeout=0.5) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 — a 404 raises here; either way the UI is gone
        return False


def _find_free_port(start: int, span: int = 20) -> int | None:
    """First free port in ``[start, start+span)``, or ``None`` if all are taken."""
    for candidate in range(start, start + span):
        if not _port_live(candidate):
            return candidate
    return None


def _viewer_env(port: int) -> dict:
    env = dict(os.environ)
    # Both server.js and log-store.js resolve the trace dir from this var.
    env["TRACING_STATE_DIR"] = str(tracing_config.state_dir())
    env["TRACING_UI_PORT"] = str(port)
    return env


def _pid_file() -> Path:
    return tracing_config.state_dir() / "viewer.pid"


_PID_MAX = 2**31 - 1  # pid_t is a signed 32-bit int; real Windows pids stay far below it.


def _is_json_int(value: object) -> bool:
    """True for a JSON integer, and false for a bool (``isinstance(True, int)``)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_pid(value: object) -> bool:
    """True for a pid the OS can answer about.

    A bool would name pid 1, and an int too large for ``os.kill`` raises
    OverflowError instead of answering "not running".
    """
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value <= _PID_MAX


def _parse_pid_record(text: str | None) -> dict | None:
    """The record in ``text``, or None when it is not one."""
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, dict) and _is_pid(data.get("pid")) and _is_json_int(data.get("port")):
        return data
    return None


def _read_pid_file() -> dict | None:
    """Read the record, dropping only a record that was read and is not one.

    A read that fails (a Windows sharing violation, a permission blip) is not
    evidence that the record is bad: it stays for the next run. Deleting a bad
    record is serialized with ``_write_pid_file``'s atomic replace, so a record
    a concurrent launcher wrote between the read and the unlink is not the one
    the delete removes.
    """
    path = _pid_file()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return None
    except ValueError:
        text = None  # not UTF-8: a corrupt record, removed below
    entry = _parse_pid_record(text)
    if entry is not None:
        return entry
    from raven.utils.atomic_io import write_transaction

    # The delete is a read-modify-write, so it runs under the writer's lock --
    # but taking that lock can fail too (a read-only state dir, an ACL that
    # denies the lock file). That is not evidence the record is bad, and the
    # recovery command must not turn it into a traceback: leave the record.
    try:
        with write_transaction(path):
            try:
                current = path.read_text(encoding="utf-8")
            except OSError:
                return None
            except ValueError:
                current = None
            entry = _parse_pid_record(current)
            if entry is not None:
                # A launcher replaced the corrupt record while this read was
                # failing over it: the newer record wins.
                return entry
            with suppress(OSError):
                path.unlink()
    except OSError:
        return None
    return None


def _write_pid_file(pid: int, port: int) -> None:
    """Record the viewer, replacing the file atomically.

    A reader that caught a half-written record would read it as "no record",
    drop the file, and leave a live viewer with no handle -- so the write goes
    through ``atomic_replace`` rather than straight to the file. That helper
    also creates the state directory, which is why this does not do it itself.
    """
    from raven.utils.atomic_io import atomic_replace

    atomic_replace(_pid_file(), json.dumps({"pid": pid, "port": port}))


def _clear_pid_file(pid: int) -> None:
    """Remove the record only while it still names ``pid``.

    The caller has just decided about that pid (stopped it, or found it stale).
    A launcher can replace the record in between; deleting that newer record
    would take away the handle of a viewer the caller never saw. A lock that
    cannot be taken leaves the record in place: the next run reads it again.
    """
    path = _pid_file()
    from raven.utils.atomic_io import write_transaction

    try:
        with write_transaction(path):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, ValueError):
                return
            entry = _parse_pid_record(text)
            if entry is None or entry["pid"] != pid:
                return
            with suppress(OSError):
                path.unlink()
    except OSError:
        return


def _pid_is_viewer(pid: int) -> bool | None:
    """Identify the Node viewer, keeping an unreadable live process unknown.

    A pid from the pid file may have been recycled by the OS for an unrelated
    process, so liveness alone is never enough to signal it. The shared process
    reader must identify both Node and the viewer script on every platform.
    """
    import re

    from raven.utils.pid import pid_alive
    from raven.utils.processes import command_line

    out = command_line(pid)
    if not out:
        return None if pid_alive(pid) else False
    node = r"node(?:js)?(?:-?v?\d+(?:\.\d+)*)?(?:\.exe)?"
    quoted = re.match(r"""^"([^"]+)"|^'([^']+)'""", out)
    if quoted is not None:
        name = re.split(r"[/\\]", quoted.group(1) or quoted.group(2))[-1]
        return re.fullmatch(node, name, re.IGNORECASE) is not None and "server.js" in out[quoted.end() :]
    # ps leaves spaces unquoted; an argument starting with a new absolute path
    # must not be consumed as part of the executable's directory.
    prefix = r"(?:(?:[A-Za-z]:)?[/\\]+)?(?:[^\s/\\]+(?:[ \t]+[^\s/\\]+)*[/\\])*"
    match = re.match(prefix + node + r"(?=\s|$)", out, re.IGNORECASE)
    return match is not None and "server.js" in out[match.end() :]


def _checked_viewer(pid: int) -> bool:
    identity = _pid_is_viewer(pid)
    if identity is None:
        console.print(
            f"[yellow]Cannot verify the tracing viewer (pid {pid}); its record is kept. "
            "Restore process access, or after confirming the record is stale, "
            f"remove {escape(str(_pid_file()))} and retry. Do not signal an unverified PID.[/yellow]"
        )
        raise typer.Exit(1)
    return identity


def _stop_viewer() -> None:
    had_record = _pid_file().exists()
    entry = _read_pid_file()
    if entry is None:
        if _pid_file().exists():
            # The record survived the read: it could not be read this time, or
            # its lock could not be taken. It is still there for the next run,
            # so do not send the user to kill a pid by hand.
            console.print(
                "The tracing viewer record could not be read this time; a viewer may still be "
                "running. Retry [cyan]raven tracing stop[/cyan]."
            )
        elif had_record:
            # There was a file and it could not be used, so `_read_pid_file`
            # dropped it. A viewer may still be running -- a record written by
            # an older CLI could be truncated by a crash mid-write -- so do not
            # claim there is none.
            console.print("No usable tracing viewer record; a viewer may still be running. Stop it by hand.")
        else:
            console.print("Tracing viewer is not running.")
        return
    pid = entry["pid"]
    if not _checked_viewer(pid):
        _clear_pid_file(pid)
        console.print("Tracing viewer is not running (cleared a stale pid file).")
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    _clear_pid_file(pid)
    console.print(f"Stopped tracing viewer (pid {pid}).")


def _resolve_node() -> str:
    from raven.cli.tui_commands import find_node

    # The viewer's floor is stricter than the TUI's, so the search has to use
    # it: a PATH node the TUI accepts would otherwise hide a zstd-capable one
    # later in the order. find_node still returns the best node it saw when
    # none qualifies, so the version check stays the caller's.
    node, version = find_node(_node_supports_zstd)
    if not node or version is None or not _node_supports_zstd(version):
        console.print(
            "[red]Node with zstd support not found (22.15+, or 23.8+).[/red] The tracing dashboard reads "
            "zstd-compressed payloads and needs a Node with zlib zstd support.\n"
            "Install: https://nodejs.org/  or  brew install node@22  or  nvm install 22"
        )
        raise typer.Exit(1)
    return node


def _server_js() -> Path:
    server_js = _viewer_dir() / "server.js"
    if not server_js.exists():
        console.print(f"[red]Viewer not found at {server_js}[/red]")
        raise typer.Exit(1)
    return server_js


def _open_dashboard(port: int) -> None:
    entry = _read_pid_file()
    # A record still on disk survived the read: `_read_pid_file` drops a record
    # it read and could not use, and keeps one it could not read (or whose lock
    # it could not take). Only the former is gone for good.
    record_kept = entry is None and _pid_file().exists()
    stuck: dict | None = None
    if entry is not None:
        if _checked_viewer(entry["pid"]):
            if _viewer_health(entry["port"]):
                url = f"http://127.0.0.1:{entry['port']}/"
                console.print(f"Tracing dashboard already running at [cyan]{url}[/cyan]")
                webbrowser.open(url)
                return
            # The record names a live viewer of ours, but it is not answering:
            # it may be hung, or its assets were replaced under it. Starting a
            # second viewer would leave this process unreachable, because
            # `raven tracing stop` only signals pids named in this file.
            if entry["port"] == port:
                # The user asked for this exact port, so the viewer that is not
                # answering is the one they are waiting for. Keep the record
                # and send them to stop it.
                console.print(
                    f"[yellow]Tracing viewer (pid {entry['pid']}) is not answering on "
                    f"http://127.0.0.1:{entry['port']}/. "
                    "Run [cyan]raven tracing stop[/cyan] to stop it, then retry.[/yellow]"
                )
                raise typer.Exit(1)
            # Another port: the viewer that is stuck is not the one the user is
            # waiting for, so the launcher goes on to the port they asked for.
            # The record still names the old viewer until a viewer started
            # below takes it over, and `raven tracing stop` can then reach only
            # the new one.
            stuck = entry
        else:
            _clear_pid_file(entry["pid"])

    if _port_live(port):
        if _viewer_health(port):
            url = f"http://127.0.0.1:{port}/"
            console.print(f"Tracing dashboard already running at [cyan]{url}[/cyan]")
            # This viewer has no record of its own -- there was none, the one we
            # had was stale and has just been cleared, or the record names a
            # viewer on another port -- so nothing in the CLI can stop it
            # later. Say so instead of handing out a dashboard the user cannot
            # shut down.
            if record_kept:
                console.print(
                    "[yellow]Its pid record could not be read this time, so [cyan]raven tracing stop[/cyan] "
                    "may not manage it; retry that command before stopping the instance by hand.[/yellow]"
                )
            else:
                console.print(
                    "[yellow]It has no usable pid record, so [cyan]raven tracing stop[/cyan] will "
                    "not manage it; stop that instance by hand.[/yellow]"
                )
            if stuck is not None:
                # The record names the stuck viewer on the other port; that is
                # the one `stop` still manages, so name it rather than let the
                # earlier decision go unsaid.
                console.print(
                    f"[yellow]The recorded viewer (pid {stuck['pid']}, port {stuck['port']}) is not "
                    "answering and keeps running; [cyan]raven tracing stop[/cyan] manages that one, "
                    f"not the viewer on {port}.[/yellow]"
                )
            webbrowser.open(url)
            return
        # Port is held by something that is NOT our viewer (a stale instance or
        # an unrelated server) — reusing it would open a broken/foreign page.
        # Move to the next free port instead of clashing.
        free = _find_free_port(port + 1)
        if free is None:
            console.print(
                f"[red]Port {port} is in use by another process, and no free port "
                f"was found nearby.[/red] Stop it, or pass --port to pick one."
            )
            raise typer.Exit(1)
        console.print(
            f"[yellow]Port {port} is held by another process (not the tracing viewer); "
            f"starting on {free} instead.[/yellow]"
        )
        port = free

    node = _resolve_node()
    server_js = _server_js()
    log_dir = tracing_config.state_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(log_dir / "viewer.log", "a", encoding="utf-8")  # noqa: SIM115 — handed to the child
    proc = subprocess.Popen(
        [node, str(server_js)],
        cwd=str(_viewer_dir()),
        env=_viewer_env(port),
        stdout=log_file,
        stderr=log_file,
        start_new_session=True,
    )
    _write_pid_file(proc.pid, port)
    if stuck is not None:
        # The record now names the viewer just started, so the old one is out of
        # the CLI's reach: name its pid so the user can stop it by hand.
        console.print(
            f"[yellow]The recorded viewer (pid {stuck['pid']}, port {stuck['port']}) is not "
            "answering and keeps running; [cyan]raven tracing stop[/cyan] now manages the "
            "new viewer only, so stop the old one by hand.[/yellow]"
        )

    for _ in range(24):  # wait up to ~6s for the server to actually serve
        if _viewer_health(port):
            break
        time.sleep(0.25)

    url = f"http://127.0.0.1:{port}/"
    console.print(f"Tracing dashboard at [cyan]{url}[/cyan]")
    webbrowser.open(url)


def _serve_foreground(port: int) -> None:
    node = _resolve_node()
    console.print(f"Serving tracing dashboard on http://127.0.0.1:{port}/  (Ctrl-C to stop)")
    try:
        subprocess.run(
            [node, str(_server_js())],
            cwd=str(_viewer_dir()),
            env=_viewer_env(port),
            check=False,
        )
    except KeyboardInterrupt:
        pass


def _run_compact(*, dry_run: bool) -> None:
    from raven.tracing.compact import compact
    from raven.tracing.store import TraceStore

    artifacts_dir = TraceStore(tracing_config.state_dir()).artifacts_dir
    if not artifacts_dir.is_dir():
        console.print(f"[yellow]No artifacts at {escape(str(artifacts_dir))}.[/yellow]")
        return
    result = compact(artifacts_dir, dry_run=dry_run)
    if dry_run:
        console.print("[dim]dry run - nothing is written[/dim]")
    console.print(f"scanned {result.scanned}, folded {result.folded}, skipped {result.skipped_fresh} fresh")
    console.print(
        f"removed {result.blobs_removed} unreferenced blobs, reclaimed {result.bytes_reclaimed / (1024 * 1024):.1f} MB"
    )
    for message in result.errors[:10]:
        console.print(f"[yellow]skipped:[/yellow] {escape(message)}")
    hidden = max(len(result.errors) - 10, 0) + result.errors_dropped
    if hidden:
        console.print(f"[yellow]... and {hidden} more[/yellow]")


def _run_compress(*, dry_run: bool, logs: bool = False) -> None:
    from raven.tracing.compress import compress, compress_logs
    from raven.tracing.store import TraceStore

    store = TraceStore(tracing_config.state_dir())
    if dry_run:
        console.print("[dim]dry run - nothing is written[/dim]")
    artifacts_dir = store.artifacts_dir
    if artifacts_dir.is_dir():
        result = compress(artifacts_dir, dry_run=dry_run)
        console.print(
            f"blobs {result.blobs}: compressed {result.compressed}, already {result.already}, relinked {result.relinked}"
        )
        console.print(f"messages {result.messages}: compressed {result.messages_compressed}")
        console.print(
            f"{result.bytes_before / (1024 * 1024):.1f} MB -> {result.bytes_after / (1024 * 1024):.1f} MB, "
            f"saved {result.saved / (1024 * 1024):.1f} MB"
        )
        _print_skipped(result.errors)
    else:
        console.print(f"[yellow]No artifacts at {escape(str(artifacts_dir))}.[/yellow]")
    if logs:
        log_result = compress_logs(store.logs_dir, dry_run=dry_run)
        console.print(
            f"logs {log_result.files}: compressed {log_result.compressed}, already {log_result.already}, "
            f"left for the next rotation {log_result.newest}"
        )
        console.print(
            f"{log_result.bytes_before / (1024 * 1024):.1f} MB -> {log_result.bytes_after / (1024 * 1024):.1f} MB, "
            f"saved {log_result.saved / (1024 * 1024):.1f} MB"
        )
        _print_skipped(log_result.errors)


def _run_pack_messages(*, dry_run: bool) -> None:
    from raven.tracing.message_pack import pack_messages
    from raven.tracing.store import TraceStore

    artifacts_dir = TraceStore(tracing_config.state_dir()).artifacts_dir
    if dry_run:
        console.print("[dim]dry run - nothing is written[/dim]")
    if not artifacts_dir.is_dir():
        console.print(f"[yellow]No artifacts at {escape(str(artifacts_dir))}.[/yellow]")
        return
    result = pack_messages(artifacts_dir, dry_run=dry_run)
    console.print(
        f"messages {result.scanned}: packed {result.packed}, already {result.already}, "
        f"packs {result.packs}, removed files {result.removed}"
    )
    console.print(
        f"{result.bytes_before / (1024 * 1024):.1f} MB -> {result.bytes_after / (1024 * 1024):.1f} MB, "
        f"saved {result.saved / (1024 * 1024):.1f} MB"
    )
    _print_skipped(result.errors)


def _print_skipped(errors: list[str]) -> None:
    for message in errors[:10]:
        console.print(f"[yellow]skipped:[/yellow] {escape(message)}")
    hidden = max(len(errors) - 10, 0)
    if hidden:
        console.print(f"[yellow]... and {hidden} more[/yellow]")


def register(app: typer.Typer) -> None:
    """Attach the ``tracing`` command to ``app``.

    A top-level leaf command (not a subcommand group) so the TUI command
    catalog surfaces it as a plain ``/tracing`` slash under "(top-level)",
    the same as ``/status`` / ``/doctor``.
    """

    @app.command("tracing")
    def tracing(
        action: str = typer.Argument(
            None,
            help=(
                "Optional action: 'stop' shuts down the background viewer; "
                "'compact' folds duplicate artifacts; 'compress' rewrites payloads as zstd "
                "(--logs adds the rotated span and event logs); 'pack-messages' moves message blobs "
                "into packs. All but 'stop' are terminal only, not available from the TUI."
            ),
        ),
        port: int = typer.Option(None, "--port", "-p", help="Port to bind (default: config or 4318)."),
        foreground: bool = typer.Option(
            False, "--foreground", "-f", help="Run the viewer in the foreground (blocks; Ctrl-C to stop)."
        ),
        dry_run: bool = typer.Option(
            False,
            "--dry-run",
            help="With 'compact', 'compress' or 'pack-messages': report what would change without writing.",
        ),
        logs: bool = typer.Option(False, "--logs", help="With 'compress': also compress rotated span and event logs."),
    ) -> None:
        """Open the tracing dashboard (captured LLM/tool/memory spans)."""
        if action == "compact":
            _run_compact(dry_run=dry_run)
            return
        if action == "compress":
            _run_compress(dry_run=dry_run, logs=logs)
            return
        if action == "pack-messages":
            _run_pack_messages(dry_run=dry_run)
            return
        if action == "stop":
            _stop_viewer()
            return
        if action is not None:
            console.print(
                f"[red]Unknown action '{escape(action)}'.[/red] "
                "Supported actions: stop, compact, compress, pack-messages"
            )
            raise typer.Exit(2)
        bind_port = port if port is not None else tracing_config.port()
        if foreground:
            _serve_foreground(bind_port)
        else:
            _open_dashboard(bind_port)
