"""``raven agent-plugins`` -- inspect and import Agent Plugin packages.

An Agent Plugin package is the portable package format of Agent Plugins
1.0: a directory whose root holds a ``plugin.json`` that validates against
the Agent Plugins manifest schema, with optional ``skills/`` and
``mcp.json`` components. It is data only and it is not a Raven kernel
plugin (those ship Python factories in ``raven-plugin.toml``).

The commands here read and write the workspace skill tree through the
package ledger, and the ledger proves ownership per workspace against the
install markers it recorded -- so nothing outside a directory Raven verifiably
installed is ever touched, in this workspace or any other sharing the home.

- ``agent-plugins list``      -- the packages this build can see
- ``agent-plugins inspect``   -- validate a package, touching nothing
- ``agent-plugins import``    -- validate a package and import its skills
- ``agent-plugins remove``    -- delete a package's imported skills
- ``agent-plugins check``     -- report an installed copy that went stale
"""

from __future__ import annotations

import json
import stat
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
    workspace = _workspace_path()
    ledger = _load_ledger()
    running = set(config.agent_plugins.mcp_packages)

    def install_state(name: str) -> tuple[bool, str]:
        """Whether a verified install stands here, and what the record says.

        ``imported`` answers only for a verified install: a record whose
        directory is gone or replaced, or a legacy record, describes the
        ledger, not an install standing in this workspace.
        """
        root = workspace / "skills" / name
        if ledger.entry(name, workspace=workspace) is None:
            if ledger.legacy_entry(name) is not None:
                return False, "legacy"
            return False, "none"
        if not root.exists():
            return False, "missing"
        if ledger.claim(root, workspace=workspace).owned:
            return True, "verified"
        return False, "unverified"

    states = {package.name: install_state(package.name) for package in packages}

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
                            "imported": states[package.name][0],
                            "install": states[package.name][1],
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

    status_labels = {
        "verified": "[green]imported[/green]",
        "unverified": "[yellow]imported (unverified copy)[/yellow]",
        "missing": "[yellow]imported (copy missing)[/yellow]",
        "legacy": "[yellow]imported (legacy record)[/yellow]",
        "none": "[yellow]available[/yellow]",
    }
    table = Table(title="Agent Plugin packages", show_header=True, header_style="bold cyan")
    table.add_column("Name")
    table.add_column("Version")
    table.add_column("Source")
    table.add_column("Status")
    table.add_column("MCP")
    for package in packages:
        status = status_labels[states[package.name][1]]
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
    from raven.agent_plugins import discover_packages, discover_skills

    config = _load_config(config_path)
    entry = Path(path).expanduser()
    packages, discovery_problems = discover_packages([entry], disabled=config.agent_plugins.disabled)

    reports: list[dict] = []
    problems: list[dict] = [{"path": item.path, "reason": item.reason} for item in discovery_problems]
    failed = bool(problems)
    if not as_json:
        for item in problems:
            console.print(f"[red]{escape(item['path'])}[/red]: {escape(item['reason'])}", highlight=False)
    for package in packages:
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
    from raven.agent_plugins import discover_packages
    from raven.agent_plugins import (
        import_package as run_import,
    )
    from raven.agent_plugins.ledger import LedgerError

    config = _load_config(config_path)
    entry = Path(path).expanduser()
    packages, discovery_problems = discover_packages([entry], disabled=config.agent_plugins.disabled)

    workspace = _workspace_path()
    ledger = _load_ledger()
    if ledger.unrecognized:
        console.print(
            f"[red]the package ledger at {escape(str(ledger.path))} cannot be read: "
            f"{escape(ledger.unrecognized)}; no package was imported[/red]",
            highlight=False,
        )
        raise typer.Exit(1)
    blocklist = config.skill_forge.blocklist
    reports: list[dict] = []
    problems: list[dict] = [{"path": item.path, "reason": item.reason} for item in discovery_problems]
    failed = bool(problems)
    if not as_json:
        for item in problems:
            console.print(f"[red]{escape(item['path'])}[/red]: {escape(item['reason'])}", highlight=False)

    for package in packages:
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
                    "removed": [],
                    "retained": [],
                    "released": [],
                    "skipped": [],
                    "failed": [],
                    "problems": [],
                    "refusal": report.refusal,
                }
            )
            continue
        reports.append(
            {
                "package": report.package,
                "version": report.version,
                "target": report.target,
                "installed": list(report.installed),
                "removed": list(report.removed),
                "retained": list(report.retained),
                "released": list(report.released),
                "skipped": [{"skill": name, "reason": reason} for name, reason in report.skipped],
                "failed": [{"skill": name, "reason": reason} for name, reason in report.failed],
                "problems": [{"name": item.name, "reason": item.reason} for item in report.problems],
                "refusal": report.refusal,
            }
        )
        failed = failed or bool(report.failed)

    try:
        ledger.save()
    except (LedgerError, OSError) as exc:
        console.print(
            f"[red]the package ledger could not be saved: {escape(str(exc))}; "
            "the imported skill directories stay on disk but are not recorded, "
            "and a re-import will refuse to touch them until they are moved away[/red]",
            highlight=False,
        )
        raise typer.Exit(1)

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
        for name in report["removed"]:
            console.print(f"  [yellow]removed[/yellow] {escape(name)}: no longer in the package")
        for name in report["retained"]:
            console.print(
                f"  [red]kept[/red] {escape(name)}: the old copy could not be removed",
                highlight=False,
            )
        for name in report["released"]:
            console.print(
                f"  [yellow]released[/yellow] {escape(name)}: no longer shipped and no longer "
                "verifiably the imported copy; left alone",
                highlight=False,
            )
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
    """Delete a package's imported skills and, when none remain, forget it.

    Exit code 0 does not mean the package is gone: a kept entry leaves the
    package directory and its ledger record in place. Read the kept list
    (kept_details with --json), not the exit code.
    """
    from raven.agent_plugins.ledger import (
        INSTALL_MARKER_FILENAME,
        LedgerError,
        read_install_marker,
        restore_install_marker,
    )
    from raven.agent_plugins.skills import remove_tree

    _load_config(config_path)
    workspace = _workspace_path()
    ledger = _load_ledger()
    if ledger.unrecognized:
        console.print(
            f"[red]the package ledger at {escape(str(ledger.path))} cannot be read: "
            f"{escape(ledger.unrecognized)}; no package was removed[/red]",
            highlight=False,
        )
        raise typer.Exit(1)
    entry = ledger.entry(name, workspace=workspace)
    if entry is None:
        if ledger.legacy_entry(name) is not None:
            console.print(
                f"[red]the import record for '{escape(name)}' predates ownership verification and "
                f"cannot be matched to the directories on disk; nothing was deleted. Move "
                f"{escape(str(workspace / 'skills' / name))} away yourself if you no longer want it[/red]",
                highlight=False,
            )
            raise typer.Exit(1)
        others = ledger.other_workspaces_with(name, workspace=workspace)
        if others:
            console.print(
                f"[red]no package named '{escape(name)}' is imported into this workspace; "
                "the ledger records it in another workspace[/red]",
                highlight=False,
            )
        else:
            console.print(
                f"[red]no package named '{escape(name)}' in the ledger of this workspace[/red]",
                highlight=False,
            )
        raise typer.Exit(1)

    target = workspace / "skills" / name
    removed: list[str] = []
    kept: list[dict] = []
    failed: list[tuple[str, str]] = []
    if target.is_dir():
        root_claim = ledger.claim(target, workspace=workspace)
        if not root_claim.owned:
            console.print(
                f"[red]{escape(str(target))} {escape(root_claim.reason)}; left alone. "
                "Move it away yourself if you no longer want it, then import again[/red]",
                highlight=False,
            )
            raise typer.Exit(1)
        for child in sorted(target.iterdir()):
            if child.name == INSTALL_MARKER_FILENAME:
                continue
            child_claim = ledger.claim(child, workspace=workspace)
            if child_claim.owned:
                try:
                    remove_tree(child)
                except OSError as exc:
                    failure = str(exc)
                    # The marker lives inside the tree being deleted, so a
                    # partial removal can consume it before failing. The
                    # ledger still records its token; put the marker back
                    # while the directory survives, or the retry this
                    # failure asks for cannot prove the directory is ours.
                    try:
                        info = child.lstat()
                    except OSError:
                        info = None
                    if info is not None and stat.S_ISDIR(info.st_mode) and not getattr(info, "st_reparse_tag", 0):
                        token = entry.skill_tokens.get(child.name, "")
                        if token and read_install_marker(child) != token:
                            try:
                                restore_install_marker(child, token)
                            except OSError as marker_exc:
                                failure += f"; the install marker could not be restored: {marker_exc}"
                    failed.append((child.name, failure))
                else:
                    removed.append(child.name)
            else:
                kept.append({"skill": child.name, "reason": child_claim.reason})
        if not failed and not kept:
            leftovers = [child for child in target.iterdir() if child.name != INSTALL_MARKER_FILENAME]
            if leftovers:
                # A directory appeared after the sweep: it was never claimed,
                # so keep the record and the marker that prove the rest.
                kept.extend(
                    {"skill": child.name, "reason": "appeared while the package was being removed"}
                    for child in leftovers
                )
            else:
                try:
                    (target / INSTALL_MARKER_FILENAME).unlink()
                except OSError:
                    pass
                try:
                    target.rmdir()
                except OSError as exc:
                    failed.append((name, str(exc)))
                    # The record below stays claimed, so the marker proving it
                    # must stand again: without it the retry this failure asks
                    # for would be refused by the very check that kept it.
                    if entry.install_token and not (target / INSTALL_MARKER_FILENAME).exists():
                        try:
                            restore_install_marker(target, entry.install_token)
                        except OSError as marker_exc:
                            failed.append((name, f"the install marker could not be restored: {marker_exc}"))

    if not failed and not kept:
        ledger.forget(name, workspace=workspace)
    try:
        ledger.save()
    except (LedgerError, OSError) as exc:
        console.print(
            f"[red]the package ledger could not be saved: {escape(str(exc))}[/red]",
            highlight=False,
        )
        raise typer.Exit(1)

    if as_json:
        console.print_json(
            json.dumps(
                {
                    "package": name,
                    "removed": removed,
                    "kept": [item["skill"] for item in kept],
                    "kept_details": kept,
                    "failed": [{"skill": skill, "reason": reason} for skill, reason in failed],
                }
            )
        )
        if failed:
            raise typer.Exit(1)
        return
    for skill, reason in failed:
        console.print(f"  [red]failed[/red] {escape(skill)}: {escape(reason)}", highlight=False)
    if kept:
        console.print(
            f"removed {len(removed)} imported skill(s) from '{escape(name)}'; "
            f"{len(kept)} unverified entr{'y remains' if len(kept) == 1 else 'ies remain'} in "
            f"{escape(str(target))}, so the package directory and its ledger record were kept. "
            "Move them away, then run remove again",
            highlight=False,
        )
    elif not failed:
        console.print(f"removed '{escape(name)}'" + (f" and {len(removed)} imported skill(s)" if removed else ""))
    for item in kept:
        console.print(
            f"  [yellow]kept[/yellow] {escape(item['skill'])}: {escape(item['reason'])}",
            highlight=False,
        )
    if failed:
        raise typer.Exit(1)


@agent_plugin_app.command("check")
def check_packages(
    config_path: Optional[str] = typer.Option(None, "--config", "-c", help="Path to config file"),
    as_json: bool = typer.Option(False, "--json", help="Print JSON instead of a table"),
) -> None:
    """Report the imported copies that no longer match their package."""
    from raven.agent_plugins import package_fingerprint

    _load_config(config_path)
    workspace = _workspace_path()
    ledger = _load_ledger()
    rows: list[dict] = []
    for name, entry in sorted(ledger.entries_for(workspace).items()):
        # Old records may carry a path as typed; normalize what can be
        # normalized before asking whether the package is there.
        source = Path(entry.source).expanduser().resolve()
        install_dir = workspace / "skills" / name
        if not install_dir.exists():
            install = "missing"
        elif ledger.claim(install_dir, workspace=workspace).owned:
            install = "verified"
        else:
            install = "unverified"
        if not source.is_dir():
            row = {"name": name, "status": "missing", "detail": f"{source} is gone", "install": install}
        else:
            current = package_fingerprint(source)
            if current == entry.fingerprint:
                row = {"name": name, "status": "current", "detail": entry.version, "install": install}
            else:
                row = {
                    "name": name,
                    "status": "stale",
                    "detail": f"imported from {entry.version}",
                    "install": install,
                }
        if install == "unverified":
            row["detail"] += "; the installed directory is not the recorded copy"
        elif install == "missing":
            row["detail"] += "; the installed directory is gone"
        rows.append(row)
    for name, _entry in sorted(ledger.legacy_entries.items()):
        rows.append(
            {
                "name": name,
                "status": "unverified",
                "detail": (
                    "imported before the ledger recorded install identities; "
                    "move its skill directories away and import again to reinstall"
                ),
                "install": "unknown",
            }
        )

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
        colour = {"current": "green", "stale": "yellow", "missing": "red", "unverified": "yellow"}[row["status"]]
        table.add_row(row["name"], f"[{colour}]{row['status']}[/{colour}]", row["detail"])
    console.print(table)


__all__ = ["agent_plugin_app"]
