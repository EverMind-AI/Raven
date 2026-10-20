"""Source subcommands — owns the ``sources_app`` Typer instance.

Bundles all ``raven sources ...`` subcommands:

Read-only inspection:

- ``sources list``                    — the rows: what each source's terms permit
- ``sources check <source>``          — may this deployment use it, and if not, why

The acceptance ledger:

- ``sources optin <source>``          — record why this deployment may use it
- ``sources revoke-optin <source>``   — withdraw that acceptance

``sources optin`` is the command a refusal names. A source whose terms do not
permit a use is refused with the remedy written down, and the remedy is this
command — so it has to exist, and it has to take the reason.

``commands.py`` imports :data:`sources_app` and registers it on the top-level
``app`` via ``app.add_typer(sources_app, name="sources")``.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

console = Console()


sources_app = typer.Typer(help="Inspect the source registry and the operator's acceptances")


@sources_app.command("list")
def sources_list(
    tier: str | None = typer.Option(None, "--tier", "-t", help="Filter by tier"),
):
    """List the sources and what their terms permit."""
    from raven.sources.policy import SOURCE_POLICIES

    policies = list(SOURCE_POLICIES.values())
    if tier:
        policies = [policy for policy in policies if policy.tier.value == tier]
        if not policies:
            console.print(f"[red]No source is in the {tier!r} tier.[/red]")
            raise typer.Exit(2)

    table = Table(title=f"Sources ({len(policies)})")
    table.add_column("Source", style="cyan")
    table.add_column("Tier")
    table.add_column("May keep", style="green")
    table.add_column("Conditions", overflow="fold")
    for policy in policies:
        table.add_row(
            policy.source,
            policy.tier.value,
            "yes" if policy.store_allowed else "no",
            "; ".join(f"{use}: {reason}" for use, reason in policy.restrictions) or "—",
        )
    console.print(table)


@sources_app.command("check")
def sources_check(
    source: str = typer.Argument(..., help="The source the deployment wants to use"),
    use: str = typer.Option("research", "--use", "-u", help="What the deployment wants to do with it"),
):
    """Say whether this deployment may use this source for this use.

    The answer is never an exception: the question has an answer either way,
    and a refusal that names the rule and the remedy is more useful than a
    traceback.
    """
    from raven.sources.optins import has_opt_in
    from raven.sources.policy import SourceRefusedError, consult, policy_for

    recorded = has_opt_in(source, use)
    known = policy_for(source)
    try:
        policy = consult(source, use=use, has_opt_in=recorded)
    except SourceRefusedError as exc:
        console.print(f"[red]{source} refused by source policy.[/red]")
        console.print(f"[dim]rule:[/dim] {exc.reason}")
        console.print(f"[dim]to change it:[/dim] {exc.remedy}")
        if known is not None:
            console.print(f"[dim]the terms are at {known.citation}[/dim]")
        raise typer.Exit(1) from exc

    console.print(f"[green]Allowed.[/green] {source} may be used for {use}.")
    console.print(f"[dim]tier:[/dim] {policy.tier.value}")
    console.print(f"[dim]the terms are at {policy.citation}[/dim]")
    if recorded:
        console.print("[dim]an acceptance is on record for this deployment[/dim]")


@sources_app.command("optin")
def sources_optin(
    source: str = typer.Argument(..., help="The source the deployment may use"),
    basis: str = typer.Option(..., "--basis", "-b", help="Why this deployment may use it"),
    use: str = typer.Option("", "--use", "-u", help="The use the acceptance covers"),
    granted_by: str = typer.Option("", "--granted-by", help="Who recorded it"),
):
    """Record why this deployment may use this source.

    An acceptance with no reason behind it is refused: it would let a later
    reader conclude that the deployment had been checked when it had not.
    """
    from raven.sources.optins import write_optin
    from raven.sources.policy import SourcePolicyError, policy_for

    if policy_for(source) is None:
        console.print(f"[red]The source {source!r} is not in the registry, so its terms have not been looked at.[/red]")
        console.print("[dim]Add a row for it to raven/sources/policy.py before using it.[/dim]")
        raise typer.Exit(2)

    if not basis.strip():
        console.print("[red]An acceptance needs a basis: say why this deployment may use the source.[/red]")
        raise typer.Exit(2)

    try:
        grant = write_optin(source, basis=basis, use=use, granted_by=granted_by)
    except SourcePolicyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    console.print(f"[green]Recorded[/green] {source}." + (f" for the use {use!r}." if use else " for every use."))
    console.print(f"[dim]basis: {grant.get('basis', '')}[/dim]")


@sources_app.command("revoke-optin")
def sources_revoke_optin(
    source: str = typer.Argument(..., help="The source to withdraw"),
    use: str = typer.Option("", "--use", "-u", help="The use to withdraw; empty withdraws the source outright"),
):
    """Withdraw an acceptance."""
    from raven.sources.optins import delete_optin

    removed = delete_optin(source, use or None)
    if removed:
        console.print(f"[green]Withdrawn[/green] {source}." + (f" for the use {use!r}." if use else " outright."))
    else:
        console.print(f"[dim]Nothing to withdraw: {source} had no acceptance{(' for ' + repr(use)) if use else ''}.[/dim]")


__all__ = ["sources_app"]
