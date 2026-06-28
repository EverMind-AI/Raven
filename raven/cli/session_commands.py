"""``raven session`` subapp — manage conversation sessions.

User-facing "session id" is the bare chat_id (strip "cli:" prefix for
display; re-prepend internally). Full session key = "cli:<chat_id>".

Persistence semantics:
- ``session create`` (bare): mints a new id and prints it. Nothing is
  written to disk — the id materialises on first use (lazy). Note: a
  lazily-minted id that was never used cannot be found by ``resume``
  because no file exists yet; the output includes a reminder.
- ``session create --title TEXT``: mints + immediately persists metadata
  so the title survives process exit. This diverges from the TUI's lazy
  semantics because the CLI process dies immediately after the command
  returns, leaving no opportunity for a later lazy flush.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from raven.cli._log_silence import mute_subsystem_logs_unless_debug
from raven.config.paths import get_workspace_path
from raven.session.export import default_export_path, write_transcript
from raven.session.manager import SessionManager, new_chat_id

console = Console()

session_app = typer.Typer(
    help="Manage conversation sessions.",
    no_args_is_help=True,
)

_CLI_CHANNEL = "cli"


@session_app.callback()
def _suppress_info_logs() -> None:
    mute_subsystem_logs_unless_debug()


def _open_manager() -> SessionManager:
    return SessionManager(get_workspace_path())


def _bare_id(key: str) -> str:
    if key.startswith(f"{_CLI_CHANNEL}:"):
        return key[len(f"{_CLI_CHANNEL}:"):]
    return key


def _full_key(bare_or_key: str) -> str:
    if ":" in bare_or_key:
        return bare_or_key
    return f"{_CLI_CHANNEL}:{bare_or_key}"


def resolve_session(manager: SessionManager, id_or_prefix: str) -> str:
    """Resolve a bare id or prefix to a full session key.

    Searches only the cli channel. Returns the key on unambiguous match.
    Exits with code 1 on not-found or ambiguous prefix.
    """
    sessions = manager.list_sessions(channel=_CLI_CHANNEL)
    bare_input = _bare_id(id_or_prefix)

    exact = [s for s in sessions if _bare_id(s["key"]) == bare_input]
    if exact:
        return exact[0]["key"]

    prefix_matches = [s for s in sessions if _bare_id(s["key"]).startswith(bare_input)]
    if not prefix_matches:
        console.print(f"[red]No session matching id/prefix {id_or_prefix!r}[/red]")
        raise typer.Exit(code=1)
    if len(prefix_matches) > 1:
        cands = ", ".join(_bare_id(s["key"]) for s in prefix_matches[:8])
        console.print(
            f"[red]Ambiguous prefix {id_or_prefix!r} — candidates: {cands}[/red]"
        )
        raise typer.Exit(code=1)
    return prefix_matches[0]["key"]


def resolve_session_cross_channel(manager: SessionManager, value: str) -> str:
    """Resolve the agent ``--session`` value to a full ``channel:chat_id`` key.

    Unlike :func:`resolve_session` (cli-only, exits on no-match), this searches
    every channel and never leaves a colon-less value that would mis-path:

    - already a full key (contains ':') → returned unchanged;
    - bare id with exactly one exact chat_id match → that full key;
    - bare id with exactly one prefix match → that full key;
    - multiple matches → ``typer.BadParameter`` listing candidate full keys;
    - no match → ``cli:<value>`` fallback (mirrors the interactive bare-as-cli
      path, allowing a new cli session to be named/created).

    Delegates the resolution rules to :meth:`SessionManager.resolve_key`; this
    function only maps the structured outcome to the agent path's CLI behavior.
    """
    res = manager.resolve_key(value)
    if res.status == "ambiguous":
        cands = ", ".join(res.candidates[:8])
        raise typer.BadParameter(
            f"{value!r} matches multiple sessions: {cands}. "
            "Pass the full channel:chat_id key."
        )
    if res.status == "resolved":
        return res.key
    return f"{_CLI_CHANNEL}:{value}"


# ── create ────────────────────────────────────────────────────────────


@session_app.command("create")
def session_create(
    title: str | None = typer.Option(None, "--title", "-t", help="Optional session title"),
) -> None:
    """Mint a new session id.

    Without --title: prints the bare id only; nothing is written to disk.
    The id materialises on first use when ``raven agent --session`` is
    called with it.

    With --title: persists session metadata immediately so the title
    survives process exit.
    """
    chat_id = new_chat_id()
    key = f"{_CLI_CHANNEL}:{chat_id}"

    if title is not None:
        manager = _open_manager()
        session = manager.get_or_create(key)
        session.metadata["title"] = title
        manager.save(session)
        console.print(f"[green]✓[/green] Created session [cyan]{chat_id}[/cyan] (title: {title!r})")
        console.print(f"  Use with: raven agent --session {key}")
    else:
        console.print(chat_id)
        console.print(
            f"[dim]  (lazy — materialises on first use: "
            f"raven agent --session {key})[/dim]"
        )


# ── list ──────────────────────────────────────────────────────────────


@session_app.command("list")
def session_list(
    all_channels: bool = typer.Option(
        False,
        "--all",
        help="Show sessions from all channels (default: cli only)",
    ),
) -> None:
    """List sessions, sorted by most recently updated.

    Default shows only sessions on the ``cli`` channel (within-surface).
    Pass ``--all`` to include sessions from every channel.
    """
    manager = _open_manager()
    channel_filter = None if all_channels else _CLI_CHANNEL
    sessions = manager.list_sessions(channel=channel_filter)

    if not sessions:
        label = "all channels" if all_channels else "cli channel"
        console.print(f"[dim]No sessions found ({label}).[/dim]")
        return

    table = Table()
    table.add_column("ID", style="cyan")
    if all_channels:
        table.add_column("Channel")
    table.add_column("Title")
    table.add_column("Messages", justify="right")
    table.add_column("Updated")

    for s in sessions:
        key = s["key"]
        channel, _, chat_id = key.partition(":")
        meta = s.get("metadata") or {}
        title = meta.get("title") or "-"
        updated = (s.get("updated_at") or "")[:19].replace("T", " ")
        msg_count = str(s.get("message_count", 0))

        row = [chat_id]
        if all_channels:
            row.append(channel)
        row += [title, msg_count, updated]
        table.add_row(*row)

    console.print(table)


# ── resume ────────────────────────────────────────────────────────────


@session_app.command("resume")
def session_resume(
    id_or_prefix: str = typer.Argument(..., metavar="ID", help="Session id or unique prefix"),
) -> None:
    """Resolve a session id or prefix and print the full session key.

    The printed key can be fed directly to ``raven agent --session``.
    This command is a pure resolver — it does not start an agent loop.
    """
    manager = _open_manager()
    key = resolve_session(manager, id_or_prefix)
    console.print(key)
    console.print(f"[dim]  Use with: raven agent --session {key}[/dim]")


# ── delete ────────────────────────────────────────────────────────────


@session_app.command("delete")
def session_delete(
    id_or_key: str = typer.Argument(..., metavar="ID", help="Bare session id or full key"),
) -> None:
    """Delete a session by bare id or full key (e.g. ``cli:<id>``)."""
    manager = _open_manager()
    key = _full_key(id_or_key)

    if not manager.exists(key):
        console.print(f"[red]Session not found: {id_or_key!r}[/red]")
        raise typer.Exit(code=1)

    if manager.delete(key):
        console.print(f"[green]✓[/green] Deleted session {_bare_id(key)}")
    else:
        console.print(f"[red]Failed to delete session {id_or_key!r}[/red]")
        raise typer.Exit(code=1)


# ── fork ──────────────────────────────────────────────────────────────


@session_app.command("fork")
def session_fork(
    id_or_prefix: str = typer.Argument(..., metavar="ID", help="Session id or unique prefix to fork"),
    title: str | None = typer.Option(None, "--title", "-t", help="Optional title for the forked session"),
) -> None:
    """Fork a session into a new diverging copy (fork-at-head).

    Resolves the id/prefix within the cli channel, copies its full history
    into a fresh session, records the source as the child's parent, and prints
    the new child id (first line) for use with ``raven agent --session``.
    """
    manager = _open_manager()
    key = resolve_session(manager, id_or_prefix)
    child = manager.fork(key, title=title)
    if child is None:
        console.print(f"[red]Cannot fork {id_or_prefix!r}: session is empty or missing[/red]")
        raise typer.Exit(code=1)

    child_bare = _bare_id(child.key)
    console.print(child_bare)
    console.print(
        f"[dim]  (forked from {_bare_id(key)}; use: raven agent --session {child.key})[/dim]"
    )


# ── export ────────────────────────────────────────────────────────────


@session_app.command("export")
def session_export(
    id_or_prefix: str = typer.Argument(..., metavar="ID", help="Session id, prefix, or full channel:chat_id key"),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Write to this path (absolute, or relative to the current directory) instead of the default exports dir",
    ),
) -> None:
    """Export a session transcript to a human-readable Markdown file.

    Resolves the id across channels (full key / bare id / unique prefix). A
    value matching no session exits non-zero rather than creating one, since
    export is read-only.
    """
    manager = _open_manager()
    res = manager.resolve_key(id_or_prefix)
    if res.status == "ambiguous":
        cands = ", ".join(res.candidates[:8])
        console.print(f"[red]Ambiguous id {id_or_prefix!r} — candidates: {cands}[/red]")
        raise typer.Exit(code=1)
    session = manager.peek(res.key) if res.status == "resolved" else None
    if session is None:
        console.print(f"[red]No session matching {id_or_prefix!r}[/red]")
        raise typer.Exit(code=1)
    dest = output if output is not None else default_export_path(get_workspace_path(), res.key)
    try:
        written = write_transcript(session, dest)
    except OSError as exc:
        console.print(f"[red]Failed to write export to {dest}: {exc}[/red]")
        raise typer.Exit(code=1)
    console.print(f"[green]✓[/green] Exported to [cyan]{written}[/cyan]")


__all__ = ["session_app", "resolve_session", "resolve_session_cross_channel"]
