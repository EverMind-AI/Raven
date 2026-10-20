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
from raven.ops.connection_add import NODE_FIELD, probe
from raven.ops.connection_add import forget_password as _forget_password
from raven.ops.connection_add import forget_unowned_password as _forget_unowned_password
from raven.ops.connection_add import parse_probe as _parse_probe  # noqa: F401 -- re-export, the tests read it here
from raven.ops.connection_add import password_auth as _password_auth
from raven.ops.connection_add import remove as _remove
from raven.ops.connection_add import remove_ssh_alias as _remove_ssh_alias
from raven.ops.connection_add import store_password as _store_password
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
    auth: str = typer.Option(
        "",
        "--auth",
        help="key (the default) or password: how Raven logs in. A password is kept on this computer, readable by "
        "you alone, and used on every connection.",
    ),
    password_stdin: bool = typer.Option(
        False,
        "--password-stdin",
        help="Read the password from the first line of stdin, before any question; implies --auth password.",
    ),
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
    import sys

    import click

    from raven.ops.connections import LOCAL, SSH, UNREADABLE, read, row_problems, store_path

    if password_stdin and (auth or "password").strip().lower() != "password":
        # Refused rather than ignored: the piped line would otherwise answer the
        # first question, and the password would land in the registry as the
        # machine's name.
        console.print("[red]--password-stdin goes with --auth password. Nothing was written.[/red]")
        raise typer.Exit(1)
    auth = auth or ("password" if password_stdin else "")
    kind = (transport or SSH).strip().lower()
    login = (auth or "key").strip().lower()
    password = ""
    if login == "password" and password_stdin:
        # Read here, before the first question: piped in, the password must not
        # be left to answer a question of its own -- it used to answer the
        # machine's name (reviewed 2026-10-19). Never an option: a command line
        # is readable by every account here.
        password = click.get_text_stream("stdin").readline().rstrip("\r\n")
    if not non_interactive:
        console.print(f"[dim]writing to {store_path()}[/dim]")
        name = name or _ask("What do you call this machine")
        kind = (transport or _ask("Reached over ssh, or is it this computer? (ssh/local)", SSH)).lower()
        conn_id = conn_id or _ask("Short id for it", _slug(name))
        if kind != LOCAL:
            host = host or _ask("Address")
            port = port or int(_ask("Port", "22"))
            user = user or _ask("Username", "root")
            login = (auth or _ask("Log in with a key or a password? (key/password)", "key")).strip().lower()
            if login == "password" and not password and sys.platform != "win32":
                password = typer.prompt("Password (kept on this computer, readable by you alone)", hide_input=True)
            elif login == "key":
                key = key or _ask("Private key path", "~/.ssh/id_rsa")
    conn_id = conn_id or _slug(name)
    row: dict[str, Any] = {"id": conn_id, "display_name": name or conn_id, "transport": kind}
    if kind != LOCAL:
        if login not in ("key", "password"):
            console.print(f"[red]--auth must be key or password, not {login!r}. Nothing was written.[/red]")
            raise typer.Exit(1)
        if login == "password":
            if sys.platform == "win32":
                console.print(
                    "[red]registering with a password is not supported on Windows; use a key instead. "
                    "Nothing was written.[/red]"
                )
                raise typer.Exit(1)
            if not password:
                console.print(
                    "[red]registering with a password needs the password: pass --password-stdin, or run without "
                    "--non-interactive to be asked. Nothing was written.[/red]"
                )
                raise typer.Exit(1)
            row.update({"host": host, "port": port, "user": user, "auth": "password"})
        else:
            row.update({"host": host, "port": port, "user": user, "key": key})

    # Judged before anything is kept or reached, in the order machines.add
    # uses: the row's own fields (its id above all, since a password file is
    # named by it), a registry that reads, and an id not in use. A failed
    # registration used to leave a password file behind -- under a path-shaped
    # id even outside keys/ -- or to delete or replace the registered machine's
    # (reviewed 2026-10-19). Remove, then register again.
    early = [f for f in row_problems(row) if f.blocking]
    if early:
        for fault in early:
            console.print(f"  [red]- {fault}[/]")
        console.print("[red]Nothing was written.[/red]")
        raise typer.Exit(1)
    registry = read()
    if registry.state == UNREADABLE:
        console.print(f"[red]{registry.detail}[/red]")
        console.print("[red]Nothing was written: fix the registry file first.[/red]")
        raise typer.Exit(1)
    if any(str(r.get("id") or "").strip() == conn_id for r in registry.rows):
        console.print(
            f"[red]a machine with id {conn_id!r} is already listed in {store_path()}; remove it first "
            f"(raven ops connection remove {conn_id}). Nothing was written.[/red]"
        )
        raise typer.Exit(1)

    kept = False
    written = None
    try:
        if password:
            # Kept before the probe, which reaches the machine the way every later connection will.
            try:
                _store_password(conn_id, password)
            except ValueError as exc:
                console.print(f"[red]{exc}. Nothing was written.[/red]")
                raise typer.Exit(1) from None
            kept = True
        if skip_probe:
            console.print(
                "[yellow]--skip-probe: this machine was not contacted, so nothing here is confirmed.[/yellow]"
            )
        else:
            reached, message, found = probe(row)
            if not reached:
                if password:
                    message = message.replace(password, "the password")
                console.print(f"[red]{message}[/red]")
                console.print("Nothing was written. Fix the address, port, user, key or password and run this again.")
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
    finally:
        if kept and written is None:
            # Refused, aborted at a question, or the write failed: no row of
            # this call's holds the password, so it is not kept either --
            # unless a registration of the same id landed meanwhile, whose row
            # it is now (`forget_unowned_password` has the rule).
            _forget_unowned_password(conn_id, password)
    console.print(f"[green]wrote {row['id']} to {written}[/green]")
    if password:
        console.print(f"[green]password kept for {row['id']} (readable by you alone; used on every connection)[/green]")
        console.print(
            "No Host alias goes into ~/.ssh/config for a password machine: scp and rsync from a terminal ask for the "
            "password, and an agent reaches its files through Raven's own file tools."
        )
    try:
        alias = _write_ssh_alias(row)
    except (OSError, ValueError) as exc:
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
        if _password_auth(row) and reach != "local":
            reach += " (password)"
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


@connection_app.command("remove")
def remove_connection(
    conn_id: str = typer.Argument(..., help="The machine's id in the registry."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Remove without asking first."),
    force: bool = typer.Option(False, "--force", help="Remove although agent entries still name the machine."),
) -> None:
    """Take a machine out of the registry. Its ssh alias goes with it; the node installed on it stays there.

    Refused while a connected agent entry names the machine: such an entry
    fails at its next dispatch, after the spawn has answered that it started,
    so the entry is disconnected first (the Agent Hub, or the config's
    ``subagents.agents`` section) -- or ``--force`` removes the machine anyway
    and says which entries are left pointing at nothing. A disconnected entry
    does not refuse the removal; it is named, since connecting it again is
    refused until the machine is registered again. What the install put on the machine is not touched:
    this computer stops knowing the way there, and ``<node_dir>`` on the
    machine is the owner's to remove by hand. The password kept here for a
    machine registered with one is deleted with the row.
    """
    from pydantic import ValidationError

    from raven.config.update_subagents import agents_on_machine
    from raven.ops.connections import read, store_path

    wanted = conn_id.strip()
    found = read()
    if found.state == "unreadable":
        console.print(f"[red]{found.detail}[/red]")
        raise typer.Exit(1)
    rows = [r for r in found.rows if str(r.get("id") or "").strip() == wanted]
    if not rows:
        console.print(f"[red]machine {wanted!r} is not registered in {store_path()}[/red]")
        raise typer.Exit(1)
    name = str(rows[0].get("display_name") or wanted)
    label = f"{name!r} ({wanted})" if name != wanted else repr(wanted)
    try:
        named = agents_on_machine(wanted, only_enabled=True)
        left = [name for name in agents_on_machine(wanted) if name not in named]
    except ValidationError:
        named, left = [], []
    if named and not force:
        console.print(
            f"[red]{len(named)} connected agent entr{'y names' if len(named) == 1 else 'ies name'} machine {label}: "
            f"{', '.join(named)}.[/red]"
        )
        console.print("Disconnect them first, or pass --force to remove the machine anyway. Nothing was changed.")
        raise typer.Exit(1)
    if not yes and not typer.confirm(f"Remove machine {label} from the registry?", default=False):
        raise typer.Exit(1)
    try:
        path = _remove(wanted)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        console.print("[red]Nothing was changed.[/red]")
        raise typer.Exit(1) from None
    console.print(f"[green]removed {wanted} from {path}[/green]")
    try:
        if _remove_ssh_alias(wanted):
            console.print(f"[green]ssh alias '{wanted}' removed from ~/.ssh/config[/green]")
    except (OSError, ValueError) as exc:
        console.print(f"[yellow]ssh alias not removed ({exc}); take the 'Host {wanted}' block out by hand[/yellow]")
    console.print(
        "The Raven node installed on it, if any, stays there: remove its node directory on the machine by hand."
    )
    if _forget_password(wanted):
        # Whatever the row's login: a file kept under this id is nobody's once the row is gone.
        console.print("[green]the password kept here for it is deleted[/green]")
    if named:
        console.print(
            f"[yellow]{', '.join(named)} still name{'s' if len(named) == 1 else ''} this machine and "
            "fail at dispatch until disconnected, or until it is registered again under this id.[/yellow]"
        )
    if left:
        console.print(
            f"[yellow]The disconnected entr{'y' if len(left) == 1 else 'ies'} {', '.join(left)} still "
            f"name{'s' if len(left) == 1 else ''} this machine and cannot be connected until it is registered again "
            "under this id.[/yellow]"
        )


@connection_app.command("install-node")
def install_node(
    conn_id: str = typer.Argument(..., help="The machine's id in the registry."),
    node_dir: str = typer.Option(
        "", "--dir", help="Where on the machine to keep Raven nodes (default ~/.raven-node); kept in the registry."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Install without asking first."),
    prune: bool = typer.Option(
        False, "--prune", help="Also remove the nodes of other Raven versions from that directory."
    ),
) -> None:
    """Put this Raven's node on a registered machine, so files can be read and agents started there.

    The node is this Raven's own code in a small virtualenv of its own, under
    ``<dir>/<version>-<digest>``; nothing else on the machine changes. Asks
    before installing; a machine that already has this node is left as it is.
    """
    import asyncio

    from raven.node import install as node_install
    from raven.node.client import NodeError, NodePool

    chosen = node_dir.strip().rstrip("/")
    try:
        plan = node_install.plan(conn_id, node_dir=chosen or None)
    except node_install.InstallError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    if plan.present:
        console.print(f"Machine {plan.target.label} already has the Raven node {plan.install_name} in {plan.node_dir}.")
    else:
        if (why := node_install.refusal(plan)) is not None:
            console.print(f"[red]{why}[/red]")
            raise typer.Exit(1)
        console.print(plan.describe())
        if not yes and not typer.confirm("Install it?", default=False):
            raise typer.Exit(1)
        try:
            done = node_install.install(plan)
        except node_install.InstallError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from None
        size = f" ({done.size_kb // 1024} MB)" if done.size_kb else ""
        console.print(f"Installed the Raven node {done.install_name} in {done.node_dir}{size}.")
        if not done.rg:
            console.print(
                "[yellow]ripgrep-bin was not available from the machine's package index or PyPI; "
                "grep there runs Raven's own Python search, as it does on a computer without rg.[/yellow]"
            )
    if prune:
        try:
            removed = node_install.prune(plan)
        except node_install.InstallError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from None
        console.print(f"Removed {len(removed)} other node(s) from {plan.node_dir}: {', '.join(removed) or 'none'}.")
    from raven.ops.connection_add import set_fields

    if chosen and chosen != str(plan.target.field("node_dir") or "").strip().rstrip("/"):
        try:
            set_fields(conn_id, {"node_dir": chosen})
        except ValueError as exc:
            console.print(f"[red]The node is installed, but {chosen} could not be recorded: {exc}[/red]")
            raise typer.Exit(1) from None
        console.print(f"Recorded {chosen} as where machine {plan.target.label} keeps its Raven nodes.")
    # The node's install name goes on the row too: how a listing says the
    # machine has this Raven's node without reaching it. A row it cannot go on
    # costs the listing that knowledge, not the install, so it is a warning.
    if plan.install_name != str(plan.target.field(NODE_FIELD) or ""):
        try:
            set_fields(conn_id, {NODE_FIELD: plan.install_name})
        except ValueError as exc:
            console.print(
                f"[yellow]The node is installed, but could not be noted on the machine's row ({exc}); "
                "the Agent Hub lists the machine without a node until a link to it comes up.[/yellow]"
            )

    async def answer() -> dict:
        pool = NodePool()
        try:
            return await pool.hello(conn_id)
        finally:
            await pool.close_all()

    try:
        hello = asyncio.run(answer())
    except NodeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    console.print(f"[green]The node answers: Python {hello.get('python')}, code {hello.get('digest')}.[/green]")


# The host's `ops` group holds only the registry. The campaign commands stay in
# the on-call agent's own checkout: the host needs to know which machines exist
# and to write one down while the owner is in the conversation, not to run
# campaigns itself.
ops_app = typer.Typer(help="The machines the owner's agents can run work on.")
ops_app.add_typer(connection_app, name="connection")
