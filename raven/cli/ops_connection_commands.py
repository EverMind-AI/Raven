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
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

connection_app = typer.Typer(help="The machines this instance can run work on.")
console = Console()

_PROBE = (
    "echo CORES=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null); "
    "echo MEM=$(free -g 2>/dev/null | awk '/^Mem:/{print $2}'); "
    "echo GPU=$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | paste -sd'|' -); "
    "echo LIBC=$(ldd --version 2>/dev/null | head -1)"
)


def _run(argv: list[str], timeout: float) -> tuple[int, str, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return 127, "", f"{argv[0]} not found on this computer"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout:.0f}s"
    return done.returncode, done.stdout, done.stderr


def _ssh_argv(host: str, port: int, user: str, key: str, command: str) -> list[str]:
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-p",
        str(port),
        "-i",
        os.path.expanduser(key),
        f"{user}@{host}",
        command,
    ]


def probe(row: dict[str, Any], *, timeout: float = 30.0) -> tuple[bool, str, dict[str, Any]]:
    """Reach the machine and read what it is. ``(reached, message, detected)``.

    The reaching is the point and the reading is the bonus. A row that cannot be
    reached is not written, because an unreachable row in the registry is worse
    than an absent one: absent is a question the loop knows to ask, unreachable
    is a fact it acts on.
    """
    from raven.ops.connections import LOCAL, transport_of

    if transport_of(row) == LOCAL:
        code, out, err = _run(["sh", "-c", _PROBE], timeout)
        where = "this computer"
    else:
        argv = _ssh_argv(str(row["host"]), int(row["port"]), str(row["user"]), str(row["key"]), _PROBE)
        code, out, err = _run(argv, timeout)
        where = f"{row['user']}@{row['host']}:{row['port']}"
    if code != 0:
        return False, f"could not reach {where}: {(err or out).strip() or f'exit {code}'}", {}
    return True, f"reached {where}", _parse_probe(out)


def _parse_probe(out: str) -> dict[str, Any]:
    seen: dict[str, str] = {}
    for line in out.splitlines():
        key, _, value = line.partition("=")
        if value.strip():
            seen[key.strip()] = value.strip()
    found: dict[str, Any] = {}
    if seen.get("CORES", "").isdigit():
        found["cores"] = int(seen["CORES"])
    if seen.get("MEM", "").isdigit() and int(seen["MEM"]) > 0:
        found["memory"] = f"{seen['MEM']} GB"
    gpu = seen.get("GPU", "")
    if gpu:
        cards = [c.strip() for c in gpu.split("|") if c.strip()]
        found["device"] = " + ".join(cards)
        found["kind"] = "gpu"
    elif "cores" in found:
        found["kind"] = "cpu"
    if seen.get("LIBC"):
        found["note"] = seen["LIBC"]
    return found


def _write(row: dict[str, Any]) -> Path:
    """Append one machine, keeping whatever shape the file already had."""
    from raven.ops.connections import read, store_path

    path = store_path()
    found = read()
    if found.state == "unreadable":
        raise ValueError(f"{found.detail}\nFix or move that file before adding to it.")
    rows = list(found.rows)
    if any(str(r.get("id")) == row["id"] for r in rows):
        raise ValueError(f"a machine with id {row['id']!r} is already listed in {path}")
    rows.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"connections": rows}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _slug(text: str) -> str:
    import re

    out = re.sub(r"[^a-z0-9_-]+", "-", text.strip().lower()).strip("-")
    return out if out and out[0].isalnum() else ""


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
