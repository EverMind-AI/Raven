"""``raven agent-plugins`` -- inspect and import Agent Plugin packages.

An Agent Plugin package is the portable package format of Agent Plugins
1.0: a directory whose root holds a ``plugin.json`` that validates against
the Agent Plugins manifest schema, with optional ``skills/`` and
``mcp.json`` components. It is data only and it is not a Raven kernel
plugin (those ship Python factories in ``raven-plugin.toml``).

The commands here read and write the workspace skill tree through the
package ledger, so nothing outside a ledger-owned directory is ever
touched.

- ``agent-plugins list``      -- the packages this build can see
- ``agent-plugins inspect``   -- validate a package, touching nothing
- ``agent-plugins import``    -- validate a package and import its skills
- ``agent-plugins remove``    -- delete a package's imported skills
- ``agent-plugins check``     -- report an installed copy that went stale
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

console = Console()

agent_plugin_app = typer.Typer(help="Inspect and import Agent Plugin packages (Agent Plugins 1.0)")


def _load_config(config_path: str | None):
    """Load the extension blocks the same way the other CLI commands do."""
    from raven.cli._helpers import load_runtime_config
    from raven.config.raven import load_raven_config

    load_runtime_config(config_path)
    return load_raven_config(Path(config_path) if config_path else None)


def _workspace_path() -> Path:
    from raven.config.loader import load_config

    return Path(load_config().workspace_path)


def _ledger_path() -> Path:
    from raven.config.paths import get_agent_plugins_ledger_path

    return get_agent_plugins_ledger_path()


def _load_ledger():
    from raven.agent_plugins import PackageLedger

    return PackageLedger.load(_ledger_path())


def _mcp_report(package):
    """What the package's ``mcp.json`` yields, read without starting anything."""
    from raven.agent_plugins.mcp import package_mcp_report

    return package_mcp_report(package, plugin_data=_package_data_dir(package.name))


def _package_data_dir(name: str) -> Path:
    """Where a package's server would keep its data.

    Named without creating: the read-only commands must not leave a
    directory behind for a package nothing will start.
    """
    from raven.config.paths import package_data_root_path

    return package_data_root_path() / name


@agent_plugin_app.command("list")
def list_packages(
    config_path: Optional[str] = typer.Option(None, "--config", "-c", help="Path to config file"),
    as_json: bool = typer.Option(False, "--json", help="Print JSON instead of a table"),
) -> None:
    """List the Agent Plugin packages this build can see."""
    from raven.agent_plugins import discover_packages

    config = _load_config(config_path)
    packages, problems = discover_packages(
        config.agent_plugins.packages,
        disabled=config.agent_plugins.disabled,
    )
    ledger = _load_ledger()
    entries = ledger.entries
    running = set(config.agent_plugins.mcp_packages)

    if as_json:
        console.print_json(
            json.dumps(
                {
                    "packages": [
                        {
                            "name": package.name,
                            "version": package.version,
                            "source": str(package.root),
                            "notes": list(package.notes),
                            "imported": package.name in entries,
                            "mcp": {
                                "servers": list(_mcp_report(package).servers),
                                "running": package.name in running,
                            },
                        }
                        for package in packages
                    ],
                    "problems": [{"path": problem.path, "reason": problem.reason} for problem in problems],
                }
            )
        )
        return

    if not packages and not problems:
        console.print(
            "[yellow]No Agent Plugin packages found.[/yellow] Point "
            "[bold]agentPlugins.packages[/bold] at a package root, or at a "
            "directory whose children are package roots.",
        )
        return

    table = Table(title="Agent Plugin packages", show_header=True, header_style="bold cyan")
    table.add_column("Name")
    table.add_column("Version")
    table.add_column("Source")
    table.add_column("Status")
    table.add_column("MCP")
    for package in packages:
        status = "[green]imported[/green]" if package.name in entries else "[yellow]available[/yellow]"
        mcp_servers = len(_mcp_report(package).servers)
        if not mcp_servers:
            mcp = "[dim]none[/dim]"
        elif package.name in running:
            mcp = f"[green]{mcp_servers} running[/green]"
        else:
            mcp = f"[yellow]{mcp_servers} not enabled[/yellow]"
        table.add_row(package.name, package.version or "(none)", str(package.root), status, mcp)
    console.print(table)

    for problem in problems:
        console.print(f"[red]{escape(problem.path)}[/red]: {escape(problem.reason)}", highlight=False)


@agent_plugin_app.command("inspect")
def inspect_package(
    path: str = typer.Argument(..., help="A package root, or a directory of package roots"),
    config_path: Optional[str] = typer.Option(None, "--config", "-c", help="Path to config file"),
    as_json: bool = typer.Option(False, "--json", help="Print JSON instead of a table"),
) -> None:
    """Validate a package and report what it holds, importing nothing."""
    from raven.agent_plugins import (
        AgentPluginManifestError,
        candidate_roots,
        discover_skills,
        load_package,
    )

    _load_config(config_path)
    entry = Path(path).expanduser()
    roots = candidate_roots(entry)
    if not roots:
        console.print(f"[red]{escape(str(entry))}[/red] holds no plugin.json", highlight=False)
        raise typer.Exit(1)

    reports: list[dict] = []
    problems: list[dict] = []
    failed = False
    for root in roots:
        try:
            package = load_package(root)
        except AgentPluginManifestError as exc:
            failed = True
            problems.append({"path": str(root), "reason": str(exc)})
            console.print(f"[red]{escape(str(root))}[/red]: {escape(str(exc))}", highlight=False)
            continue
        skills, skill_problems = discover_skills(package)
        mcp_report = _mcp_report(package)
        reports.append(
            {
                "name": package.name,
                "version": package.version,
                "source": str(package.root),
                "description": package.manifest.description,
                "notes": list(package.notes),
                "skills": [skill.name for skill in skills],
                "problems": [{"name": item.name, "reason": item.reason} for item in skill_problems],
                "mcp": {
                    "servers": list(mcp_report.servers),
                    "problems": [{"server": item.server, "reason": item.reason} for item in mcp_report.problems],
                    "disabled": mcp_report.disabled,
                },
            }
        )

    if as_json:
        console.print_json(json.dumps({"packages": reports, "problems": problems}))
        if failed:
            raise typer.Exit(1)
        return

    for report in reports:
        console.print(f"[bold]{escape(report['name'])}[/bold] {escape(report['version'])}")
        console.print(f"  [dim]source:[/dim] {escape(report['source'])}")
        if report["description"]:
            console.print(f"  [dim]description:[/dim] {escape(str(report['description']))}")
        for note in report["notes"]:
            console.print(f"  [yellow]note:[/yellow] {escape(note)}")
        console.print(f"  [dim]skills:[/dim] {len(report['skills'])}")
        for skill in report["skills"]:
            console.print(f"    - {escape(skill)}")
        for problem in report["problems"]:
            console.print(
                f"    [red]{escape(problem['name'])}[/red]: {escape(problem['reason'])}",
                highlight=False,
            )
        mcp = report["mcp"]
        console.print(f"  [dim]mcp servers:[/dim] {len(mcp['servers'])}")
        for server in mcp["servers"]:
            console.print(f"    - {escape(server)}")
        if mcp["disabled"]:
            console.print(f"  [yellow]mcp disabled:[/yellow] {escape(mcp['disabled'])}", highlight=False)
        for problem in mcp["problems"]:
            console.print(
                f"    [red]{escape(problem['server'])}[/red]: {escape(problem['reason'])}",
                highlight=False,
            )
    if failed:
        raise typer.Exit(1)


@agent_plugin_app.command("import")
def import_package(
    path: str = typer.Argument(..., help="A package root, or a directory of package roots"),
    config_path: Optional[str] = typer.Option(None, "--config", "-c", help="Path to config file"),
    as_json: bool = typer.Option(False, "--json", help="Print JSON instead of a table"),
) -> None:
    """Validate a package and import its skills into the workspace."""
    from raven.agent_plugins import (
        AgentPluginManifestError,
        LedgerEntry,
        candidate_roots,
        load_package,
    )
    from raven.agent_plugins import (
        import_package as run_import,
    )

    config = _load_config(config_path)
    entry = Path(path).expanduser()
    roots = candidate_roots(entry)
    if not roots:
        console.print(f"[red]{escape(str(entry))}[/red] holds no plugin.json", highlight=False)
        raise typer.Exit(1)

    workspace = _workspace_path()
    ledger = _load_ledger()
    blocklist = config.skill_forge.blocklist
    reports: list[dict] = []
    problems: list[dict] = []
    failed = False

    for root in roots:
        try:
            package = load_package(root)
        except AgentPluginManifestError as exc:
            failed = True
            problems.append({"path": str(root), "reason": str(exc)})
            console.print(f"[red]{escape(str(root))}[/red]: {escape(str(exc))}", highlight=False)
            continue
        report = run_import(
            package,
            workspace=workspace,
            ledger=ledger,
            blocklist=blocklist,
        )
        if report.refusal:
            failed = True
            reports.append(
                {
                    "package": report.package,
                    "version": report.version,
                    "target": report.target,
                    "installed": [],
                    "skipped": [],
                    "failed": [],
                    "problems": [],
                    "refusal": report.refusal,
                }
            )
            continue
        ledger.record(
            LedgerEntry(
                name=report.package,
                version=report.version,
                source=str(package.root),
                imported_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                skills=report.installed,
                fingerprint=package.fingerprint(),
            )
        )
        reports.append(
            {
                "package": report.package,
                "version": report.version,
                "target": report.target,
                "installed": list(report.installed),
                "skipped": [{"skill": name, "reason": reason} for name, reason in report.skipped],
                "failed": [{"skill": name, "reason": reason} for name, reason in report.failed],
                "problems": [{"name": item.name, "reason": item.reason} for item in report.problems],
                "refusal": report.refusal,
            }
        )
        failed = failed or bool(report.failed)

    ledger.save()

    if as_json:
        console.print_json(json.dumps({"packages": reports, "problems": problems}))
        if failed:
            raise typer.Exit(1)
        return

    for report in reports:
        if report.get("refusal"):
            console.print(f"[bold]{escape(report['package'])}[/bold] {escape(report['version'])} -> [red]refused[/red]")
            console.print(f"  [red]{escape(report['refusal'])}[/red]", highlight=False)
            continue
        console.print(
            f"[bold]{escape(report['package'])}[/bold] {escape(report['version'])} -> "
            f"[green]{len(report['installed'])}[/green] installed"
        )
        console.print(f"  [dim]target:[/dim] {escape(report['target'])}")
        for name, reason in [(item["skill"], item["reason"]) for item in report["skipped"]]:
            console.print(f"  [yellow]skipped[/yellow] {escape(name)}: {escape(reason)}", highlight=False)
        for item in report["failed"]:
            console.print(f"  [red]failed[/red] {escape(item['skill'])}: {escape(item['reason'])}", highlight=False)
        for item in report["problems"]:
            console.print(
                f"  [red]{escape(item['name'])}[/red]: {escape(item['reason'])}",
                highlight=False,
            )
    if failed:
        raise typer.Exit(1)


@agent_plugin_app.command("remove")
def remove_package(
    name: str = typer.Argument(..., help="The package name, as its manifest spells it"),
    config_path: Optional[str] = typer.Option(None, "--config", "-c", help="Path to config file"),
    as_json: bool = typer.Option(False, "--json", help="Print JSON instead of a table"),
) -> None:
    """Delete a package's imported skills and forget it."""
    _load_config(config_path)
    workspace = _workspace_path()
    ledger = _load_ledger()
    entry = ledger.entry(name)
    if entry is None:
        console.print(f"[red]no package named '{escape(name)}' in the ledger[/red]", highlight=False)
        raise typer.Exit(1)

    target = workspace / "skills" / name
    removed: list[str] = []
    kept: list[str] = []
    if target.is_dir():
        if not ledger.owns_directory(target, workspace=workspace):
            console.print(
                f"[red]{escape(str(target))}[/red] is not owned by the ledger; left alone",
                highlight=False,
            )
            raise typer.Exit(1)
        for child in sorted(target.iterdir()):
            if ledger.owns_directory(child, workspace=workspace):
                shutil.rmtree(child)
                removed.append(child.name)
            else:
                kept.append(child.name)
        if not any(target.iterdir()):
            target.rmdir()

    ledger.forget(name)
    ledger.save()

    if as_json:
        console.print_json(json.dumps({"package": name, "removed": removed, "kept": kept}))
        return
    console.print(f"removed '{escape(name)}'" + (f" and {len(removed)} imported skill(s)" if removed else ""))
    for child in kept:
        console.print(
            f"  [yellow]kept[/yellow] {escape(child)}: the ledger does not claim it",
            highlight=False,
        )


@agent_plugin_app.command("check")
def check_packages(
    config_path: Optional[str] = typer.Option(None, "--config", "-c", help="Path to config file"),
    as_json: bool = typer.Option(False, "--json", help="Print JSON instead of a table"),
) -> None:
    """Report the imported copies that no longer match their package."""
    from raven.agent_plugins import package_fingerprint

    _load_config(config_path)
    ledger = _load_ledger()
    rows: list[dict] = []
    for name, entry in sorted(ledger.entries.items()):
        source = Path(entry.source)
        if not source.is_dir():
            rows.append({"name": name, "status": "missing", "detail": f"{source} is gone"})
            continue
        current = package_fingerprint(source)
        if current == entry.fingerprint:
            rows.append({"name": name, "status": "current", "detail": entry.version})
        else:
            rows.append({"name": name, "status": "stale", "detail": f"imported from {entry.version}"})

    if as_json:
        console.print_json(json.dumps({"packages": rows}))
        return

    if not rows:
        console.print("[yellow]No imported packages.[/yellow]")
        return

    table = Table(title="Imported packages", show_header=True, header_style="bold cyan")
    table.add_column("Name")
    table.add_column("Status")
    table.add_column("Detail")
    for row in rows:
        colour = {"current": "green", "stale": "yellow", "missing": "red"}[row["status"]]
        table.add_row(row["name"], f"[{colour}]{row['status']}[/{colour}]", row["detail"])
    console.print(table)


__all__ = ["agent_plugin_app"]
