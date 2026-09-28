"""``raven ops connection`` -- the front door to the machine registry.

Every ``connections.json`` that has existed so far was written by hand, and the
cost of that is in the record: five byte-identical copies kept in step by hand,
a repo fixture spelling ``name`` for ``display_name`` with nothing to report it,
and -- the expensive one -- a loop with no port to dial that tried 22, 2222,
8022, 10022 and 443, read ``~/.ssh/config``, pulled a stale port out of
``known_hosts`` and believed it, then went reading raven's own campaign directory
for the number. A dozen rounds, no job submitted.

None of that is fixed by asking the model to write the file instead. It does not
know the port either; it would guess one, and a guess that lands in the registry
stops being a guess and becomes what every later turn reads as fact. So the
owner answers, and the machine itself confirms: nothing is written until an ssh
that actually connects says so, and what the machine can be asked about itself
-- cores, memory, device, glibc -- is read off it rather than typed twice.

Two front doors, one command. A person in a terminal runs it bare and is asked
one thing at a time. A TUI cannot do that (``cli.dispatch`` runs a command
in-process and hands back stdout, with no channel for a prompt, which is why
``provider login`` is on its blacklist), so the same command takes every answer
as a flag: the main agent collects them in conversation, where the owner is
typing them anyway, and the probe still has the last word.
"""

from __future__ import annotations

import json
import shlex
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

# One probe, one writer, one alias, whichever door knocks: the same functions
# serve the ``ops_connection_add`` tool. Bound here by name so this module's
# ``add`` reads them as its own globals -- which is also what a test that
# stands in a reachable machine patches.
from raven.ops.connection_add import parse_probe as _parse_probe  # noqa: F401 -- re-export, the tests read it here
from raven.ops.connection_add import probe
from raven.ops.connection_add import write as _write
from raven.ops.connection_add import write_ssh_alias as _write_ssh_alias

connection_app = typer.Typer(help="The machines this instance can run work on.")
console = Console()


def _slug(text: str) -> str:
    from raven.utils.paths import mint_slug

    return mint_slug(text)


def _ask(label: str, default: str = "", *, required: bool = True) -> str:
    while True:
        got = str(typer.prompt(label, default=default, show_default=bool(default))).strip()
        if got or not required:
            return got
        console.print("[yellow]needed[/yellow]")


@connection_app.command("add")
def add(  # noqa: PLR0913 -- one option per field of the row; a dict would hide them from --help
    conn_id: str = typer.Option("", "--id", help="Internal id; never renamed, campaigns store it."),
    name: str = typer.Option("", "--name", help="What you call this machine."),
    transport: str = typer.Option("", "--transport", help="ssh, or local for this very computer."),
    host: str = typer.Option("", "--host"),
    port: int = typer.Option(0, "--port"),
    user: str = typer.Option("", "--user"),
    key: str = typer.Option("", "--key", help="Path to the private key; the path is stored, not the key."),
    software: str = typer.Option("", "--software", help="What is installed, with paths."),
    budget_unit: str = typer.Option("", "--budget-unit", help="minute | core-minute | gpu-minute"),
    concurrency: int = typer.Option(0, "--concurrency", help="Jobs it will run at once."),
    paths: list[str] = typer.Option([], "--path", help="A directory on it that holds your work; repeatable."),
    device: str = typer.Option("", "--device"),
    cores: int = typer.Option(0, "--cores"),
    memory: str = typer.Option("", "--memory"),
    note: str = typer.Option("", "--note"),
    non_interactive: bool = typer.Option(False, "--non-interactive", help="Take every answer from flags."),
    skip_probe: bool = typer.Option(False, "--skip-probe", help="Write without reaching the machine."),
) -> None:
    """Add one machine, after connecting to it.

    Bare, it asks. With ``--non-interactive`` every answer comes from a flag, which
    is the form a TUI can dispatch -- the main agent collects the answers in
    conversation and passes them through. Either way the machine is contacted
    before anything is written, and what it can say about itself is read off it.
    """
    from raven.ops.connections import LOCAL, SSH, row_problems, store_path

    kind = (transport or SSH).strip().lower()
    if not non_interactive:
        console.print(f"[dim]writing to {store_path()}[/dim]")
        name = name or _ask("What do you call this machine")
        kind = (transport or _ask("Reached over ssh, or is it this computer? (ssh/local)", SSH)).lower()
        conn_id = conn_id or _ask("Short id for it", _slug(name))
        if kind != LOCAL:
            host = host or _ask("Address")
            port = port or int(_ask("Port", "22"))
            user = user or _ask("Username", "root")
            key = key or _ask("Private key path", "~/.ssh/id_rsa")
    conn_id = conn_id or _slug(name)
    row: dict[str, Any] = {"id": conn_id, "display_name": name or conn_id, "transport": kind}
    if kind != LOCAL:
        row.update({"host": host, "port": port, "user": user, "key": key})

    if skip_probe:
        console.print("[yellow]--skip-probe: this machine was not contacted, so nothing here is confirmed.[/yellow]")
    else:
        reached, message, found = probe(row)
        if not reached:
            console.print(f"[red]{message}[/red]")
            console.print("Nothing was written. Fix the address, port, user or key and run this again.")
            raise typer.Exit(1)
        console.print(f"[green]{message}[/green]")
        if found:
            console.print("  " + "   ".join(f"{k} {v}" for k, v in found.items()))
        row.update(found)

    for field, value in (("device", device), ("cores", cores), ("memory", memory), ("note", note)):
        if value:
            row[field] = value
    if not non_interactive:
        software = software or _ask("What is installed on it (write the paths in)", str(row.get("software") or ""))
        budget_unit = budget_unit or _ask(
            "Budget counted in", "gpu-minute" if row.get("kind") == "gpu" else "core-minute"
        )
        concurrency = concurrency or int(_ask("How many jobs at once", "1"))
        if not paths:
            answer = _ask("Directories on it that hold your work (space separated, blank for none)", required=False)
            paths = shlex.split(answer)
    row["software"] = software or row.get("software", "")
    row["budget_unit"] = budget_unit
    row["concurrency"] = concurrency
    if paths:
        row["paths"] = list(paths)

    faults = row_problems(row)
    blocking = [f for f in faults if f.blocking]
    for fault in faults:
        console.print(f"  [{'red' if fault.blocking else 'yellow'}]- {fault}[/]")
    if blocking:
        console.print("[red]Nothing was written.[/red]")
        raise typer.Exit(1)
    try:
        written = _write(row)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        console.print("[red]Nothing was written.[/red]")
        raise typer.Exit(1) from None
    console.print(f"[green]wrote {row['id']} to {written}[/green]")
    try:
        alias = _write_ssh_alias(row)
    except OSError as exc:
        console.print(f"[yellow]ssh alias not written ({exc}); transfers will need the address by hand[/yellow]")
    else:
        if alias:
            console.print(f"[green]ssh alias '{alias}' written to ~/.ssh/config (rsync/scp reach it by id)[/green]")


@connection_app.command("list")
def list_connections(as_json: bool = typer.Option(False, "--json")) -> None:
    """Every machine this instance can run on, and anything wrong with the list."""
    from raven.ops.connections import problems, read, store_path

    found = read()
    faults = problems()
    if as_json:
        console.print_json(
            json.dumps(
                {
                    "path": str(store_path()),
                    "state": found.state,
                    "detail": found.detail,
                    "connections": found.rows,
                    "problems": [f.text for f in faults],
                },
                ensure_ascii=False,
            )
        )
        raise typer.Exit(1 if any(f.blocking for f in faults) else 0)
    if found.state == "unreadable":
        console.print(f"[red]{found.detail}[/red]")
        raise typer.Exit(1)
    if not found.rows:
        console.print(f"No machine is set up ({store_path()}). Add one with `raven ops connection add`.")
        return
    table = Table(title=str(store_path()))
    for column in ("id", "name", "how", "what it is", "budget", "at once"):
        table.add_column(column)
    for row in found.rows:
        reach = "local" if str(row.get("transport") or "ssh") == "local" else f"{row.get('host')}:{row.get('port')}"
        what = " ".join(str(row.get(k)) for k in ("kind", "device", "cores") if row.get(k))
        table.add_row(
            str(row.get("id")),
            str(row.get("display_name") or ""),
            reach,
            what,
            str(row.get("budget_unit") or "[red]-[/red]"),
            str(row.get("concurrency") or "[red]-[/red]"),
        )
    console.print(table)
    for fault in faults:
        console.print(f"[yellow]  - {fault}[/yellow]")


@connection_app.command("doctor")
def doctor(as_json: bool = typer.Option(False, "--json")) -> None:
    """Check the registry and say what is wrong. Exits non-zero when something is.

    Separate from ``list`` because a caller wants one of two things -- to look, or
    to branch on the answer. The host raven's DAG check is the second: a graph
    with an on-call node and no machine to run it on should be refused before a
    single sub-agent is dispatched, and this is what it asks.
    """
    from raven.ops.connections import problems, read, shown, store_path, usable

    found = read()
    faults = problems()
    fit = usable(found.rows)
    if as_json:
        console.print_json(
            json.dumps(
                {
                    "path": str(store_path()),
                    "state": found.state,
                    "listed": len(found.rows),
                    "usable": len(fit),
                    "usable_ids": [str(r.get("id")) for r in fit],
                    "machines": [shown(r) for r in fit],
                    "blocking": [f.text for f in faults if f.blocking],
                    "advisory": [f.text for f in faults if not f.blocking],
                },
                ensure_ascii=False,
            )
        )
    else:
        console.print(f"{store_path()}: {found.state}, {len(found.rows)} listed, {len(fit)} usable")
        for fault in faults:
            console.print(f"  [{'red' if fault.blocking else 'yellow'}]- {fault}[/]")
        if not faults:
            console.print("[green]  nothing wrong[/green]")
    raise typer.Exit(0 if fit else 1)


# The host's `ops` group holds only the registry. The campaign commands stay in
# the on-call agent's own checkout: the host needs to know which machines exist
# and to write one down while the owner is in the conversation, not to run
# campaigns itself.
ops_app = typer.Typer(help="The machines the owner's agents can run work on.")
ops_app.add_typer(connection_app, name="connection")
