"""``raven tracing`` — open the tracing dashboard.

The dashboard is a dependency-free Node viewer bundled under
``raven/tracing/viewer/``. Instrumentation itself runs in-process (installed at
CLI startup, see :mod:`raven.tracing`); this command only launches the viewer
that reads the captured spans from ``~/.raven/traces``.

``raven tracing`` (bare) lazily starts the viewer if it is not already running,
then opens the browser. It reuses raven's own Node discovery (:func:`find_node`),
so it needs the same Node >= 22 that ``raven tui`` already requires.

Registered as a top-level leaf command (not a subcommand group) so the TUI
command catalog lists it as a plain ``/tracing`` slash under "(top-level)".
Foreground mode and port are options, not subcommands.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
import webbrowser
from pathlib import Path

import typer
from rich.console import Console

from raven.tracing import config as tracing_config

console = Console()


def _viewer_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "tracing" / "viewer"


def _port_live(port: int) -> bool:
    """True if something is already listening on 127.0.0.1:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _viewer_env(port: int) -> dict:
    env = dict(os.environ)
    # Both server.js and log-store.js resolve the trace dir from this var.
    env["TRACING_STATE_DIR"] = str(tracing_config.state_dir())
    env["TRACING_UI_PORT"] = str(port)
    return env


def _resolve_node() -> str:
    from raven.cli.tui_commands import find_node

    node, _version = find_node()
    if not node:
        console.print(
            "[red]Node (>= 22) not found.[/red] The tracing dashboard needs the "
            "same Node runtime as the TUI.\n"
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
    url = f"http://127.0.0.1:{port}/"
    if _port_live(port):
        console.print(f"Tracing dashboard already running at [cyan]{url}[/cyan]")
        webbrowser.open(url)
        return

    node = _resolve_node()
    server_js = _server_js()
    log_dir = tracing_config.state_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(log_dir / "viewer.log", "a", encoding="utf-8")  # noqa: SIM115 — handed to the child
    subprocess.Popen(
        [node, str(server_js)],
        cwd=str(_viewer_dir()),
        env=_viewer_env(port),
        stdout=log_file,
        stderr=log_file,
        start_new_session=True,
    )

    for _ in range(24):  # wait up to ~6s for the server to bind
        if _port_live(port):
            break
        time.sleep(0.25)

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


def register(app: typer.Typer) -> None:
    """Attach the ``tracing`` command to ``app``.

    A top-level leaf command (not a subcommand group) so the TUI command
    catalog surfaces it as a plain ``/tracing`` slash under "(top-level)",
    the same as ``/status`` / ``/doctor``.
    """

    @app.command("tracing")
    def tracing(
        port: int = typer.Option(None, "--port", "-p", help="Port to bind (default: config or 4318)."),
        foreground: bool = typer.Option(
            False, "--foreground", "-f", help="Run the viewer in the foreground (blocks; Ctrl-C to stop)."
        ),
    ) -> None:
        """Open the tracing dashboard (captured LLM/tool/memory spans)."""
        bind_port = port if port is not None else tracing_config.port()
        if foreground:
            _serve_foreground(bind_port)
        else:
            _open_dashboard(bind_port)
