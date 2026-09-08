"""Shared CLI rendering helpers: the console, the notices, the option parsers.

Everything here speaks the terminal's idiom -- rich output, typer exits and
parameter errors. The assembly it renders for lives in raven.core
(config_stack, provider_stack) and raven.providers.factory.
"""

from __future__ import annotations

import typer
from rich.console import Console

from raven.config.schema import Config
from raven.core import config_stack

console = Console()


def report_dropped_memory_writes(dropped: int, out: Console | None = None) -> None:
    """Tell the user how many turns never reached long-term memory at shutdown.

    The loop counts (``AgentLoop.drain_backend_stores``); the host renders, on
    its own console or stderr, because this is the last moment the loss is
    still actionable and the loop does not own a terminal.
    """
    if dropped:
        (out or Console(stderr=True)).print(
            f"[yellow]{dropped} turn(s) were not written to long-term memory "
            "because the memory service was unavailable.[/yellow]"
        )


def load_runtime_config(config: str | None = None, home: str | None = None) -> Config:
    """The CLI face of :func:`raven.core.config_stack.load_runtime_config`: a
    missing file is a red line and exit 1, a pinned file is announced."""
    try:
        loaded = config_stack.load_runtime_config(config, home=home)
    except FileNotFoundError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1) from exc
    if config:
        from pathlib import Path

        Console(stderr=True).print(f"[dim]Using config: {Path(config).expanduser().resolve()}[/dim]")
    return loaded


def print_probe_troubleshooting(provider: str | None) -> None:
    """Common-case hints when a probe fails.

    Shared by ``onboard`` Step 3 and ``doctor --probe`` so the diagnostic
    advice stays in one place.
    """
    console.print("\n  [dim]Troubleshooting:[/dim]")
    if provider:
        console.print(
            f"  [dim]·[/dim] [cyan]raven provider test {provider}[/cyan] — re-check credentials without spending tokens"
        )
        console.print(
            f"  [dim]·[/dim] [cyan]raven provider get {provider}[/cyan] — inspect what's actually stored on disk"
        )
    console.print(
        "  [dim]·[/dim] Check the model id in [cyan]~/.raven/config.json[/cyan] "
        "under [cyan]agents.defaults.model[/cyan] — it should match a model the "
        "provider serves."
    )


def parse_fake_now(fake_now: str | None):
    """Parse an ISO-8601 timestamp into a frozen ``now_fn`` callable.

    Used by the eval harness to drive the Sentinel stack at a deterministic
    wall-clock time via subprocess invocation. The returned callable always
    returns the same parsed datetime, so every component that reads "now"
    through ``now_fn`` sees the same snapshot for the duration of the call.

    Returns ``None`` when the flag is not set, in which case constructors
    fall through to their default ``datetime.now`` behavior.
    """
    if fake_now is None:
        return None
    from datetime import datetime as _dt

    try:
        frozen = _dt.fromisoformat(fake_now)
    except ValueError as exc:
        raise typer.BadParameter(
            f"--fake-now must be an ISO-8601 timestamp (e.g. 2026-05-13T09:00:00); got {fake_now!r}: {exc}"
        ) from exc
    return lambda: frozen


def print_deprecated_allow_destructive_notice(config: Config) -> None:
    """Warn when a config still sets the retired tools.exec.allowDestructiveCommands."""
    if config.tools.exec.should_warn_deprecated_allow_destructive:
        console.print(
            "[yellow]Hint:[/yellow] `tools.exec.allowDestructiveCommands` is ignored: deletes "
            "answer to the permission tiers (`permissions.mode`, `permissions.tools`), and "
            "a recursive delete of the root or home tree is refused in every mode. Remove the key."
        )


def print_deprecated_memory_window_notice(config: Config) -> None:
    """Warn when running with old memoryWindow-only config."""
    if config.agents.defaults.should_warn_deprecated_memory_window:
        console.print(
            "[yellow]Hint:[/yellow] Detected deprecated `memoryWindow` without "
            "`contextWindowTokens`. `memoryWindow` is ignored; run "
            "[cyan]raven onboard[/cyan] to refresh your config template."
        )


def print_config_migration_notices() -> None:
    """Tell the user about any config line a migration just changed for them.

    The migrations run inside the loader, which has no terminal; this is the
    place that does. Call it after the config is loaded and before the command
    takes over the screen -- once printed, the notices are gone.

    On stderr, because stdout is a command's answer and this is not part of it:
    ``raven doctor --json`` and ``raven import --json`` are documented for
    automation, and a line appended to their output is not a cosmetic problem
    but an unparseable document. That is also where the rest of this class of
    message already goes -- ``commands.run``'s own ConfigReadError branch and
    the loader's malformed-config warning both use stderr -- so a future
    ``--json`` command inherits the right behaviour without knowing about this.
    """
    from rich.console import Console

    from raven.config.loader import drain_migration_notices

    notices = drain_migration_notices()
    if not notices:
        return
    err = Console(stderr=True)
    for notice in notices:
        err.print(f"[yellow]Config updated:[/yellow] {notice}")


__all__ = [
    "console",
    "load_runtime_config",
    "parse_fake_now",
    "print_config_migration_notices",
    "print_deprecated_memory_window_notice",
    "print_probe_troubleshooting",
]
