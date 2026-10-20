"""Outbound subcommands — owns the ``outbound_app`` Typer instance.

Bundles all ``raven outbound ...`` subcommands:

- ``outbound list``               — the interactions: what was sent, to whom
- ``outbound ambiguous``           — the sends whose outcome nobody knows
- ``outbound resolve <token>``     — a person's decision about one of them

A prepared send is a message that may or may not have gone out. The list is
where they are seen, and ``resolve`` is where a person decides what happened
to one. Nothing here decides by itself: a send that cannot be told apart
from a send that did not happen is exactly the case that must not be
guessed at.

``commands.py`` imports :data:`outbound_app` and registers it on the
top-level ``app`` via ``app.add_typer(outbound_app, name="outbound")``.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

console = Console()


outbound_app = typer.Typer(help="Inspect outbound sends and the ones whose outcome is unknown")


def _store():
    from raven.records.library import records_store

    return records_store()


@outbound_app.command("list")
def outbound_list(
    channel: str | None = typer.Option(None, "--channel", "-c", help="Filter by channel"),
    limit: int = typer.Option(50, "--limit", "-n", help="Max rows shown"),
):
    """List the outbound interactions."""
    from raven.records.types import RecordType

    interactions = _store().list_records(record_type=RecordType.INTERACTION)
    if channel:
        interactions = [i for i in interactions if i.fields.get("channel") == channel]

    if not interactions:
        console.print("[dim]No outbound sends recorded.[/dim]")
        return

    table = Table(title=f"Outbound sends ({len(interactions)})")
    table.add_column("Token", style="cyan")
    table.add_column("Subject")
    table.add_column("Channel")
    table.add_column("State", style="green")
    table.add_column("Sent at", style="dim")
    for interaction in interactions[:limit]:
        state = interaction.state
        if state == "prepared":
            state = "[bold yellow]prepared (outcome unknown)[/bold yellow]"
        table.add_row(
            str(interaction.fields.get("correlation_token") or ""),
            str(interaction.fields.get("subject") or ""),
            str(interaction.fields.get("channel") or ""),
            state,
            str(interaction.fields.get("sent_at") or ""),
        )
    console.print(table)


@outbound_app.command("ambiguous")
def outbound_ambiguous():
    """List the sends whose outcome nobody knows.

    These are the ones a person has to decide. A send that is prepared and
    unresolved is a message that may or may not have reached somebody, and
    it is never resent on the strength of a guess.
    """
    from raven.outbound import unresolved

    pending = unresolved(_store())
    if not pending:
        console.print("[green]No send is waiting on a decision.[/green]")
        return

    table = Table(title=f"Unresolved sends ({len(pending)})")
    table.add_column("Token", style="cyan")
    table.add_column("Subject")
    table.add_column("Channel")
    table.add_column("Prepared at", style="dim")
    for interaction in pending:
        table.add_row(
            str(interaction.fields.get("correlation_token") or ""),
            str(interaction.fields.get("subject") or ""),
            str(interaction.fields.get("channel") or ""),
            interaction.updated_at,
        )
    console.print(table)
    console.print(
        "[dim]Decide each one with: raven outbound resolve <token> --sent | --not-sent[/dim]"
    )


@outbound_app.command("resolve")
def outbound_resolve(
    token: str = typer.Argument(..., help="The correlation token of the send to decide"),
    sent: bool = typer.Option(
        None, "--sent/--not-sent", help="Whether the message actually went out"
    ),
):
    """Record what happened to one unresolved send.

    ``--sent`` moves the interaction to ``sent``, so the message counts as
    one that was written. ``--not-sent`` removes it: a record of a message
    that never left is a claim that does not belong in the store, and
    leaving it would say we wrote to somebody we did not.
    """
    from raven.outbound import find_by_token, unresolved

    if sent is None:
        console.print("[red]Say what happened: --sent or --not-sent.[/red]")
        raise typer.Exit(2)

    store = _store()
    interaction = find_by_token(store, token)
    if interaction is None:
        console.print(f"[red]No send carries the token {token!r}.[/red]")
        raise typer.Exit(1)
    if interaction.state != "prepared":
        console.print(f"[dim]{token} is already {interaction.state}; nothing to decide.[/dim]")
        return

    if sent:
        from raven.outbound import complete_send
        from raven.outbound.guard import PreparedSend

        complete_send(
            store,
            PreparedSend(
                record_id=interaction.id,
                token=token,
                subject=str(interaction.fields.get("subject") or ""),
                subject_type=str(interaction.fields.get("subject_type") or ""),
                channel=str(interaction.fields.get("channel") or ""),
                body=str(interaction.fields.get("body") or ""),
                fields=dict(interaction.fields),
            ),
        )
        console.print(f"[green]Recorded[/green] {token} as sent.")
    else:
        store.delete(interaction.id)
        console.print(f"[green]Removed[/green] {token}: the message did not go out.")

    remaining = unresolved(store)
    if remaining:
        console.print(f"[dim]{len(remaining)} send(s) still waiting on a decision.[/dim]")


__all__ = ["outbound_app"]
