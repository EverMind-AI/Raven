"""Local slash-command handling for the interactive ``raven agent`` REPL.

Commands here are parsed and executed in-process, BEFORE the message is
submitted as a turn to the spine, so they never reach the LLM. They reuse the existing
``raven cron`` / ``raven sentinel`` CLI command functions directly
(the functions Typer decorates stay plain callables), so behaviour and
output match the shell CLI with no duplicated logic.

Scope (per the "cron full + sentinel read-only" decision):

- ``/cron``     — full job management: list / get / add / enable / disable /
                  delete, plus read-only ``config get``.
- ``/sentinel`` — read-only inspectors ONLY (status / nudges / decisions /
                  routines / attention / behaviors). Writes to the global
                  config (enable / disable / config set) and trigger ops
                  (tick / discover-now / behaviors-rebuild) are shell-only —
                  see ``_SENTINEL_SHELL_ONLY``.
- ``/help``     — list the available slash commands.

What is exposed follows one rule: the REPL may write *operational state*
(cron jobs in ``jobs.json`` — add/enable/disable/delete) but NOT the global
``~/.raven/config.json``. Every config write is shell-only — that covers
``cron config set/reset`` AND ``sentinel enable/disable/config set`` alike, so
the two namespaces share a single, non-contradictory rationale.

Two further REPL-specific constraints:

- The prompt_toolkit REPL owns the TTY, so a nested ``click`` confirm prompt
  would corrupt terminal state. Destructive cron ops (``delete`` / ``disable``)
  therefore require an inline ``-y`` instead of an interactive confirm.
- ``cron run`` / ``sentinel tick`` etc. call ``asyncio.run`` and/or cost LLM
  calls / rebuild a separate stack — shell-only, rejected with a CLI pointer.

Anything that is not a recognised ``/cron`` / ``/sentinel`` / ``/help`` command
returns ``False`` so the caller forwards it unchanged — that keeps control
commands like ``/stop`` and ``/restart`` working.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from rich.console import Console

_CRON_SHELL_ONLY = {
    "run": "test-fire mutates job state and uses asyncio.run, which can't "
    "run inside the REPL event loop. Use `raven cron run` in a shell.",
}

# Sentinel subcommands not exposed in the REPL, with the reason. Two classes:
#   - global-config writes (enable/disable/config) — same rule as cron config:
#     mutating ~/.raven/config.json is shell-only; the REPL stays read-only
#     for config.
#   - trigger ops (tick/ticks/discover-now/behaviors-rebuild) — cost LLM calls
#     and/or rebuild a separate stack, so they belong in a shell.
_SENTINEL_SHELL_ONLY = {
    "enable": "it writes the global config (sentinel.enabled).",
    "disable": "it writes the global config (sentinel.enabled).",
    "config": "it writes the global config (nudge-policy quotas).",
    "tick": "it costs LLM calls and rebuilds a separate Sentinel stack.",
    "ticks": "it costs LLM calls and rebuilds a separate Sentinel stack.",
    "discover-now": "it costs LLM calls and dispatches through the executor.",
    "behaviors-rebuild": "it costs LLM calls and rebuilds an extractor.",
}


def handle_repl_slash(command: str, *, console: "Console") -> bool:
    """Execute a local slash command. Return True iff it was handled here
    (caller must then NOT forward the input to the LLM)."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False  # unbalanced quotes — not ours to handle
    if not tokens:
        return False

    head, args = tokens[0].lower(), tokens[1:]
    if head == "/cron":
        return _handle_cron(args, console)
    if head == "/sentinel":
        return _handle_sentinel(args, console)
    if head in ("/help", "/?"):
        _print_help(console)
        return True
    return False


# ── tiny arg helpers ──────────────────────────────────────────────────


def _has_flag(tokens: list[str], *names: str) -> bool:
    return any(t in names for t in tokens)


def _opt(tokens: list[str], *names: str) -> str | None:
    """Value of ``--name value`` or ``--name=value``; None if absent."""
    for i, t in enumerate(tokens):
        if t in names and i + 1 < len(tokens):
            return tokens[i + 1]
        for nm in names:
            if t.startswith(nm + "="):
                return t.split("=", 1)[1]
    return None


def _first_positional(tokens: list[str]) -> str | None:
    return next((t for t in tokens if not t.startswith("-")), None)


def _invoke(console: "Console", fn: Callable[..., Any], **kwargs: Any) -> None:
    """Call a CLI command function, swallowing the control-flow exceptions
    Typer/Click would normally turn into a process exit. The command has
    already printed its own success/error output by the time these raise.

    Constraint: ``fn`` is a Typer-decorated function whose parameter defaults
    are ``OptionInfo`` sentinels, not real values. Callers MUST pass every
    parameter explicitly in ``kwargs`` — an omitted one binds the sentinel,
    not the intended default. The per-command tests guard against drift."""
    import click
    import typer

    try:
        fn(**kwargs)
    except (click.BadParameter, click.UsageError) as exc:
        console.print(f"[red]{exc.format_message()}[/red]")
    except (typer.Exit, click.exceptions.Exit, click.exceptions.Abort, SystemExit):
        pass


# ── /cron ──────────────────────────────────────────────────────────────


def _handle_cron(args: list[str], console: "Console") -> bool:
    from raven.cli import cron_commands as cc

    if not args or args[0] in ("help", "-h", "--help"):
        _print_cron_help(console)
        return True

    sub, rest = args[0], args[1:]

    if sub in _CRON_SHELL_ONLY:
        console.print(f"[yellow]/cron {sub} is shell-only: {_CRON_SHELL_ONLY[sub]}[/yellow]")
        return True

    if sub == "list":
        _invoke(console, cc.cron_list, all_=_has_flag(rest, "--all", "-a"))
        return True

    if sub == "get":
        ident = _first_positional(rest)
        if not ident:
            console.print("[red]usage: /cron get <id>[/red]")
            return True
        _invoke(console, cc.cron_get, id_prefix=ident)
        return True

    if sub == "enable":
        ident = _first_positional(rest)
        if not ident:
            console.print("[red]usage: /cron enable <id>[/red]")
            return True
        _invoke(console, cc.cron_enable, id_prefix=ident)
        return True

    if sub in ("delete", "disable"):
        return _handle_cron_destructive(sub, rest, console)

    if sub == "add":
        return _handle_cron_add(rest, console)

    if sub == "config":
        if rest and rest[0] in ("set", "reset"):
            console.print(
                "[yellow]/cron config set|reset is shell-only (writes global "
                "config). Use `raven cron config ...`.[/yellow]"
            )
            return True
        _invoke(
            console,
            cc.cron_config_get,
            forward_channels=_has_flag(rest, "--forward-channels"),
            default_timezone=_has_flag(rest, "--default-timezone"),
        )
        return True

    console.print(f"[red]Unknown /cron subcommand: {sub!r}[/red]")
    _print_cron_help(console)
    return True


def _handle_cron_destructive(sub: str, rest: list[str], console: "Console") -> bool:
    from raven.cli import cron_commands as cc

    ident = _first_positional(rest)
    if not ident:
        console.print(f"[red]usage: /cron {sub} <id> -y[/red]")
        return True
    if not _has_flag(rest, "-y", "--yes"):
        # No interactive confirm under prompt_toolkit — show what would change
        # and require an explicit -y on re-run.
        job = _resolve_quiet(ident)
        if job is not None:
            console.print(
                f"[yellow]Would {sub} job {job.id} "
                f"({cc._format_schedule(job.schedule)}). "
                f"Re-run with -y to confirm: /cron {sub} {ident} -y[/yellow]"
            )
        return True
    fn = cc.cron_delete if sub == "delete" else cc.cron_disable
    _invoke(console, fn, id_prefix=ident, yes=True)
    return True


def _handle_cron_add(rest: list[str], console: "Console") -> bool:
    from raven.cli import cron_commands as cc

    name = _opt(rest, "--name")
    message = _opt(rest, "--message")
    if not name or not message:
        console.print(
            "[red]usage: /cron add --name <name> --message <text> "
            "(--cron <expr> | --at <iso> | --every <dur>) "
            "[--tz <zone>] [--channel <ch>] [--to <id>][/red]"
        )
        return True
    # Schedule validation (exactly-one-of, syntax) is delegated to cron_add,
    # which prints friendly errors and raises typer.Exit on bad input.
    _invoke(
        console,
        cc.cron_add,
        name=name,
        message=message,
        cron=_opt(rest, "--cron"),
        at_iso=_opt(rest, "--at"),
        every=_opt(rest, "--every"),
        tz=_opt(rest, "--tz"),
        channel=_opt(rest, "--channel"),
        to=_opt(rest, "--to"),
        yes=True,
    )
    return True


def _resolve_quiet(ident: str):
    """Resolve a job id/prefix for a preview message; None on no/ambiguous
    match (the resolver has already printed why)."""
    import click
    import typer

    from raven.cli import cron_commands as cc

    try:
        return cc._resolve_id(cc._open_service(), ident)
    except (typer.Exit, click.exceptions.Exit, SystemExit):
        return None


# ── /sentinel (read-only) ───────────────────────────────────────────────


def _handle_sentinel(args: list[str], console: "Console") -> bool:
    from raven.cli import sentinel_commands as sc

    if not args or args[0] in ("help", "-h", "--help"):
        _print_sentinel_help(console)
        return True

    sub, rest = args[0], args[1:]

    if sub == "status":
        _invoke(console, sc.sentinel_status)
        return True

    if sub in _SENTINEL_SHELL_ONLY:
        console.print(
            f"[yellow]/sentinel {sub} is shell-only: {_SENTINEL_SHELL_ONLY[sub]} Use `raven sentinel {sub} …`.[/yellow]"
        )
        return True

    if sub == "nudges":
        n = _opt(rest, "-n", "--n")
        _invoke(
            console,
            sc.sentinel_nudges,
            n=int(n) if n and n.isdigit() else 20,
            show_state=not _has_flag(rest, "--no-state"),
        )
        return True

    if sub == "decisions":
        _invoke(
            console,
            sc.sentinel_decisions,
            all_=_has_flag(rest, "--all"),
            show_options=_has_flag(rest, "--show-options"),
        )
        return True

    if sub == "routines":
        _invoke(console, sc.sentinel_routines, status=_opt(rest, "--status"))
        return True

    if sub == "attention":
        _invoke(
            console,
            sc.sentinel_attention,
            section=_opt(rest, "--section", "-s"),
            workspace=_opt(rest, "--workspace", "-w"),
        )
        return True

    if sub == "behaviors":
        _invoke(
            console,
            sc.sentinel_behaviors,
            since=_opt(rest, "--since"),
            session_key=_opt(rest, "--session"),
            folded=_has_flag(rest, "--folded"),
            workspace=_opt(rest, "--workspace", "-w"),
        )
        return True

    console.print(f"[red]Unknown /sentinel subcommand: {sub!r}[/red]")
    _print_sentinel_help(console)
    return True


# ── help ─────────────────────────────────────────────────────────────────


def _print_help(console: "Console") -> None:
    console.print(
        "[bold]Local slash commands[/bold] (run in-process, not sent to the agent):\n"
        "  [cyan]/cron[/cyan] …      manage scheduled jobs — type [cyan]/cron help[/cyan]\n"
        "  [cyan]/sentinel[/cyan] …  inspect the proactivity engine — type [cyan]/sentinel help[/cyan]\n"
        "  [cyan]/help[/cyan]        this message"
    )


def _print_cron_help(console: "Console") -> None:
    console.print(
        "[bold]/cron[/bold] — scheduled jobs (~/.raven/cron/jobs.json)\n"
        "  [cyan]/cron list[/cyan] [--all]            list jobs\n"
        "  [cyan]/cron get[/cyan] <id>                full detail of one job\n"
        "  [cyan]/cron add[/cyan] --name N --message M (--cron E | --at ISO | --every DUR)\n"
        "  [cyan]/cron enable[/cyan] <id>             re-enable a paused job\n"
        "  [cyan]/cron disable[/cyan] <id> -y         pause a job\n"
        "  [cyan]/cron delete[/cyan] <id> -y          remove a job\n"
        "  [cyan]/cron config[/cyan] [get]            show cron routing config\n"
        "[dim]delete/disable need -y (no interactive prompt in the REPL); "
        "run / config set|reset are shell-only.[/dim]"
    )


def _print_sentinel_help(console: "Console") -> None:
    console.print(
        "[bold]/sentinel[/bold] — proactivity engine (read-only)\n"
        "  [cyan]/sentinel status[/cyan]                config + NudgePolicy view\n"
        "  [cyan]/sentinel nudges[/cyan] [-n N] [--no-state]   recent feedback + state\n"
        "  [cyan]/sentinel decisions[/cyan] [--all] [--show-options]\n"
        "  [cyan]/sentinel routines[/cyan] [--status candidate|active|retired]\n"
        "  [cyan]/sentinel attention[/cyan] [--section H2]     show attention.md\n"
        "  [cyan]/sentinel behaviors[/cyan] [--since DATE] [--session KEY] [--folded]\n"
        "[dim]config writes (enable/disable, config set) and trigger ops "
        "(tick, discover-now, behaviors-rebuild) are shell-only — run "
        "`raven sentinel …` in a shell.[/dim]"
    )


__all__ = ["handle_repl_slash"]
