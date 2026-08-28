"""Non-TTY guard shared by questionary-backed interactive commands.

prompt_toolkit crashes with a bare ``OSError: [Errno 22]`` traceback when
stdin is redirected, so commands call :func:`die_if_not_tty` before building
a prompt: diagnosis line, alternative command, exit code 2 (the same shape
as onboard's ``_check_tty_or_die``, plus the stdin check it lacks).
"""

from __future__ import annotations

import sys

import typer
from rich.console import Console

console = Console()


def is_tty() -> bool:
    """True when both stdin and stdout are terminals — prompt_toolkit reads
    the former and renders to the latter, so either one redirected breaks it."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def die_if_not_tty(alternative: str) -> None:
    """Exit(2) with a two-line hint when the terminal is non-interactive."""
    if is_tty():
        return
    console.print("[red]Non-interactive terminal detected.[/red]")
    console.print(f"Re-run with: [cyan]{alternative}[/cyan]")
    raise typer.Exit(2)


__all__ = ["die_if_not_tty", "is_tty"]
