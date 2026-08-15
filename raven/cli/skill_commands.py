"""Skill subcommands — owns the ``skill_app`` Typer instance.

Bundles all ``raven skill ...`` subcommands:

Read-only inspection (registry-level):

- ``skill list``                — list skills visible to SkillForge
- ``skill get <name>``          — show one skill's metadata (and optionally body)

Lifecycle management (Hub install policy):

- ``skill block <name>``        — add to skillForge.blocklist (refused everywhere)
- ``skill unblock <name>``      — remove from skillForge.blocklist
- ``skill remove <name>``       — delete an installed Hub bundle from the workspace

``commands.py`` imports :data:`skill_app` and registers it on the top-level
``app`` via ``app.add_typer(skill_app, name="skill")``.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

console = Console()


skill_app = typer.Typer(help="Inspect and manage SkillForge skills")


def _build_skill_service():
    from raven.config.loader import load_config
    from raven.memory_engine.skill_forge import LocalSkillCatalog

    config = load_config()
    workspace = config.workspace_path
    sf_cfg = getattr(config, "skill_forge", None)
    return LocalSkillCatalog(workspace, config=sf_cfg, start_watcher=False)


@skill_app.command("list")
def skill_list(
    source: str | None = typer.Option(
        None, "--source", "-s", help="Filter by source (workspace/builtin/everos/mirror/*)"
    ),
    limit: int = typer.Option(50, "--limit", "-n", help="Max rows shown"),
):
    """List skills visible to SkillForge."""
    svc = _build_skill_service()
    metas = svc.gather_all_skills()
    if source:
        metas = [m for m in metas if m.source == source]
    metas = metas[:limit]

    if not metas:
        console.print("[dim]No skills found.[/dim]")
        return

    table = Table(title=f"Skills ({len(metas)})")
    table.add_column("Name", style="cyan")
    table.add_column("Source", style="green")
    table.add_column("Description", overflow="fold")
    for m in metas:
        desc = (m.description or "")[:120]
        table.add_row(m.name, m.source, desc)
    console.print(table)


@skill_app.command("get")
def skill_get(
    name: str = typer.Argument(..., help="Skill name"),
    with_body: bool = typer.Option(False, "--with-body/--no-body", help="Include SKILL.md content"),
):
    """Show one skill's metadata (and optionally its body)."""
    svc = _build_skill_service()
    meta = svc.get_skill_metadata(name)
    if meta is None:
        console.print(f"[red]Skill not found: {name}[/red]")
        raise typer.Exit(1)

    console.print(f"[bold cyan]{name}[/bold cyan]")
    for k, v in meta.items():
        console.print(f"  [dim]{k}[/dim]: {v}")

    if with_body:
        body = svc.load_skill(name)
        if body:
            console.print("\n[bold]── SKILL.md ──[/bold]")
            console.print(Markdown(body))


@skill_app.command("block")
def skill_block(name: str = typer.Argument(..., help="Skill name / slug to refuse everywhere")):
    """Add a skill to skillForge.blocklist (dropped from the injection pool
    and refused by use_skill on the next agent/gateway start)."""
    from raven.config.update import set_skill_blocked

    blocklist = set_skill_blocked(name, True)
    console.print(f"[green]Blocked[/green] {name!r}. skillForge.blocklist = {blocklist}")
    console.print("[dim]Takes effect on the next agent/gateway start.[/dim]")


@skill_app.command("unblock")
def skill_unblock(name: str = typer.Argument(..., help="Skill name / slug to allow again")):
    """Remove a skill from skillForge.blocklist."""
    from raven.config.update import set_skill_blocked

    blocklist = set_skill_blocked(name, False)
    console.print(f"[green]Unblocked[/green] {name!r}. skillForge.blocklist = {blocklist}")
    console.print("[dim]Takes effect on the next agent/gateway start.[/dim]")


@skill_app.command("remove")
def skill_remove(
    name: str = typer.Argument(..., help="Installed Hub skill slug to delete"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
):
    """Delete an installed Hub bundle (<workspace>/skills/hub/<slug>@<version>).

    Removal alone does not stop a re-install on the next catalog hit —
    pair it with ``skill block`` to keep the skill out."""
    import shutil

    from raven.config.loader import load_config

    hub_dir = load_config().workspace_path / "skills" / "hub"
    matches = [d for d in hub_dir.glob("*@*") if d.is_dir() and d.name.rsplit("@", 1)[0].casefold() == name.casefold()]
    if not matches:
        console.print(f"[red]No installed Hub bundle found for {name!r} under {hub_dir}[/red]")
        raise typer.Exit(1)

    for d in matches:
        console.print(f"  {d}")
    if not yes and not typer.confirm(f"Delete {len(matches)} bundle dir(s)?"):
        raise typer.Exit(1)
    for d in matches:
        shutil.rmtree(d)
    console.print(f"[green]Removed[/green] {len(matches)} bundle(s) of {name!r}.")
    console.print("[dim]Tip: `raven skill block " + name + "` prevents silent re-install.[/dim]")


__all__ = ["skill_app"]
