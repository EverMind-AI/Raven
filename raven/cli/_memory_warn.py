"""User-visible warning when the memory backend fails to start.

Under the TUI, loguru is redirected to a file, so a start failure (e.g. a
memory identity that EverOS rejects) silently disables long-term memory with
nothing on screen. Mirror the embedding-unavailable warning in
``everos/backend.py`` and print to stderr so every entry point (TUI, agent
REPL, gateway) sees the same degraded-memory notice, not just the two that
already surface the traceback via stderr.
"""

from __future__ import annotations

from rich.console import Console


def warn_memory_start_failed(exc: BaseException) -> None:
    """Print a stderr notice that long-term memory is off this session."""
    Console(stderr=True).print(
        "[yellow]Memory backend failed to start; long-term memory is off this session.[/yellow]\n"
        f"[dim]Run `raven onboard` to reconfigure, or check the log ({type(exc).__name__}).[/dim]"
    )
