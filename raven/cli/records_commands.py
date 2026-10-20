"""Record subcommands — owns the ``records_app`` Typer instance.

Bundles all ``raven records ...`` subcommands:

Read-only inspection:

- ``records list``                    — the records, filtered by kind and by state
- ``records get <id>``                — one record, with the evidence behind it

The acceptance ledger:

- ``records grant <id> <source>``     — record why one source's terms may be kept
- ``records revoke-grant <id> <src>`` — withdraw that acceptance

The grant is the decision the store asks for. Where a source's terms forbid
keeping its data, the store refuses the write until the operator has recorded
why this deployment may keep it anyway, and the refusal names this command.

``commands.py`` imports :data:`records_app` and registers it on the top-level
``app`` via ``app.add_typer(records_app, name="records")``.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

console = Console()


records_app = typer.Typer(help="Inspect the durable record store and its acceptance ledger")


def _store():
    from raven.records.library import records_store

    return records_store()


def _kind_of(value: str | None):
    """The record kind, or None when no filter was asked for."""
    if not value:
        return None
    from raven.records.types import RecordType

    try:
        return RecordType(value)
    except ValueError:
        console.print(f"[red]Unknown record kind: {value}[/red]")
        console.print("[dim]One of: " + ", ".join(kind.value for kind in RecordType) + "[/dim]")
        raise typer.Exit(2)


@records_app.command("list")
def records_list(
    kind: str | None = typer.Option(None, "--type", "-t", help="Filter by kind"),
    state: str | None = typer.Option(None, "--state", "-s", help="Filter by state"),
    limit: int = typer.Option(50, "--limit", "-n", help="Max rows shown"),
):
    """List the records the store holds."""
    record_type = _kind_of(kind)
    records = _store().list_records(record_type, state)

    if not records:
        console.print("[dim]No records yet.[/dim]")
        return

    table = Table(title=f"Records ({len(records)})")
    table.add_column("Id", style="cyan")
    table.add_column("Kind")
    table.add_column("Key")
    table.add_column("State", style="green")
    table.add_column("Sources")
    for record in records[:limit]:
        sources = sorted({entry.source for entry in record.provenance if entry.source})
        table.add_row(
            record.id,
            record.type.value,
            record.dedupe_key,
            record.state,
            ", ".join(sources) or "—",
        )
    console.print(table)
    if len(records) > limit:
        console.print(f"[dim]… {len(records) - limit} more. Raise --limit to see them.[/dim]")


@records_app.command("get")
def records_get(record_id: str = typer.Argument(..., help="The record's id")):
    """Show one record, with the evidence behind it."""
    record = _store().get_by_id(record_id)
    if record is None:
        console.print(f"[red]No record carries the id {record_id!r}[/red]")
        raise typer.Exit(1)

    console.print(f"[bold cyan]{record.id}[/bold cyan]  [dim]{record.type.value} · {record.dedupe_key}[/dim]")
    console.print(f"[dim]state[/dim]: {record.state}")
    console.print(f"[dim]updated[/dim]: {record.updated_at}")

    if record.fields:
        console.print("\n[bold]Fields[/bold]")
        for name, value in record.fields.items():
            console.print(f"  [dim]{name}[/dim]: {value}")

    console.print("\n[bold]Evidence[/bold]")
    if not record.provenance:
        console.print("  [dim]none — a field with no provenance is a guess the record cannot defend[/dim]")
    for entry in record.provenance:
        confidence = "" if entry.confidence is None else f" · confidence {entry.confidence}"
        console.print(f"  [cyan]{entry.field}[/cyan] ← {entry.source_url} [dim]{entry.source or 'no source named'}[/dim]{confidence}")


@records_app.command("grant")
def records_grant(
    record_id: str = typer.Argument(..., help="The record the acceptance applies to"),
    source: str = typer.Argument(..., help="The source whose terms are being accepted"),
    basis: str = typer.Option(..., "--basis", "-b", help="Why this deployment may keep the data"),
):
    """Record why one source's terms may be kept for one record.

    An acceptance with no reason behind it is not an acceptance: a later
    reader could not tell a checked deployment from an unchecked one.
    """
    from raven.records.grants import write_grant

    if not basis.strip():
        console.print("[red]An acceptance needs a basis: say why this deployment may keep the data.[/red]")
        raise typer.Exit(2)

    try:
        grant = write_grant(record_id, source, basis=basis)
    except Exception as exc:  # noqa: BLE001 - the store's sentence is the report
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    console.print(f"[green]Recorded[/green] {source} for record {record_id}.")
    console.print(f"[dim]basis: {grant.get('basis', '')}[/dim]")


@records_app.command("revoke-grant")
def records_revoke_grant(
    record_id: str = typer.Argument(..., help="The record the acceptance applies to"),
    source: str = typer.Argument(..., help="The source to withdraw"),
):
    """Withdraw an acceptance."""
    from raven.records.grants import delete_grant

    removed = delete_grant(record_id, source)
    if removed:
        console.print(f"[green]Withdrawn[/green] {source} for record {record_id}.")
    else:
        console.print(f"[dim]Nothing to withdraw: {source} was not granted for {record_id}.[/dim]")


__all__ = ["records_app"]
