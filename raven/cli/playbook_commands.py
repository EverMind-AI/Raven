"""Playbook subcommands — owns the ``playbook_app`` Typer instance.

Bundles all ``raven playbook ...`` subcommands over the two-layer library
(packaged builtins under a writable user layer) and the ``playbooks.disabled``
switch in config:

- ``playbook list``               — both layers, with origin and switch state
- ``playbook get <name>``        — raw playbook.md to stdout, source to stderr
- ``playbook validate <target>`` — check a library name or a file path, no run
- ``playbook create <name>``     — generate into the user layer, disabled
- ``playbook enable <name>``     — remove from the disabled list
- ``playbook disable <name>``    — add to the disabled list (builtins too)
- ``playbook run <name> k=v``    — explicit synchronous run
- ``playbook delete <name>``     — user layer only; builtins can only disable

``commands.py`` imports :data:`playbook_app` and registers it on the
top-level ``app`` via ``app.add_typer(playbook_app, name="playbook")``.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from collections.abc import Awaitable
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from raven.cli.stint_commands import stint_app

console = Console()
err_console = Console(stderr=True)

playbook_app = typer.Typer(help="Manage and run stored playbooks")


def _load_config():
    from raven.config.loader import load_config

    return load_config()


def _store(config):
    from raven.playbook import PlaybookStore

    user_layer = Path(config.playbooks.dir) if config.playbooks.dir else (config.workspace_path / "playbooks")
    return PlaybookStore(user_layer)


def _require_known(store, name: str) -> str:
    origin = store.origin_of(name)
    if origin is None:
        known = ", ".join(store.list_ids()) or "(none)"
        err_console.print(f"[red]No playbook named {escape(repr(name))}. Known: {escape(known)}[/red]")
        raise typer.Exit(code=1)
    return origin


@playbook_app.command("list")
def playbook_list():
    """List both library layers, with origin and switch state."""
    config = _load_config()
    store = _store(config)
    names = store.list_ids()
    if not names:
        console.print("[dim]No playbooks installed.[/dim]")
        return
    disabled = set(config.playbooks.disabled)
    table = Table(title=f"Playbooks ({len(names)})")
    table.add_column("Name", style="cyan")
    table.add_column("Origin", style="green")
    table.add_column("State")
    table.add_column("Description", overflow="fold")
    for name in names:
        try:
            description = escape(store.load(name).description)
        except Exception as exc:  # noqa: BLE001 - a broken file is a row, not a crash
            description = f"[red]unreadable: {escape(str(exc))}[/red]"
        origin = store.origin_of(name) or "?"
        if store.is_shadowing(name):
            origin = "user (shadows builtin)"
        state = "[yellow]disabled[/yellow]" if name in disabled else "enabled"
        table.add_row(escape(name), origin, state, description)
    console.print(table)


@playbook_app.command("get")
def playbook_get(name: str = typer.Argument(..., help="Playbook name, as listed")):
    """Print the raw playbook.md (redirectable); the source path goes to stderr."""
    config = _load_config()
    store = _store(config)
    origin = _require_known(store, name)
    path = store.path_for(name)
    err_console.print(f"[dim]{origin}: {escape(str(path))}[/dim]")
    typer.echo(path.read_text(encoding="utf-8"), nl=False)


@playbook_app.command("validate")
def playbook_validate(
    target: str = typer.Argument(..., metavar="NAME|PATH", help="Library name, or a path to a playbook.md"),
):
    """Validate without running: spec shape plus the field definition's rules."""
    from pydantic import ValidationError

    from raven.playbook.validate import validate_structure

    config = _load_config()
    as_path = Path(target)
    errors: list[str] = []
    spec = None
    if as_path.suffix == ".md" or as_path.exists():
        path = as_path if as_path.is_file() else as_path / "playbook.md"
        if not path.is_file():
            err_console.print(f"[red]{escape(target)}: no playbook.md here[/red]")
            raise typer.Exit(code=1)
        # Loading through a store keeps this the same parser the runtime
        # uses; the directory name is the name under validation.
        from raven.playbook import PlaybookStore

        probe = PlaybookStore(path.parent.parent, builtin_root=path.parent.parent)
        try:
            spec = probe.load(path.parent.name)
        except (ValidationError, ValueError) as exc:
            errors.append(str(exc))
    else:
        store = _store(config)
        _require_known(store, target)
        path = store.path_for(target)
        try:
            spec = store.load(target)
        except (ValidationError, ValueError) as exc:
            errors.append(str(exc))

    if spec is not None:
        # Validated against this machine's agent table. A playbook is a
        # distribution unit, so a name missing from the table is a real finding
        # here, reported as "not registered" rather than as a malformed file.
        #
        # The table is never empty -- the package's built-in rows are seeds, not
        # config -- so there is no "could not load a table" case to handle on this
        # path. ``validate_structure`` still accepts ``None`` for a caller that
        # genuinely has no table; this is not one.
        from raven.agent.subagent.registry import AgentRegistry

        registry = AgentRegistry()
        registry.apply(config.subagents.agents)
        errors.extend(validate_structure(spec, known_agents=registry.all_names()))
    if errors:
        text = path.read_text(encoding="utf-8")
        for error in errors:
            # soft_wrap: an error line anchors on a file path, and a wrap in
            # the middle of the path breaks copy-paste and any caller that
            # greps the output (a long tmp path did exactly that in CI).
            console.print(f"[red]{escape(_located(path, text, error))}[/red]", soft_wrap=True)
        raise typer.Exit(code=1)
    console.print(f"[green]OK[/green] {escape(str(path))}", soft_wrap=True)


def _located(path: Path, text: str, error: str) -> str:
    """Best-effort line anchor: the first line mentioning the failing field.

    Precise positions would need a location-preserving YAML parser; anchoring
    on the field name is right whenever the field appears once, and degrades
    to the bare path rather than to a wrong number when it does not.
    """
    field = error.split(":", 1)[0].split(".")[-1].split("[")[0].strip()
    hits = [i for i, line in enumerate(text.splitlines(), start=1) if line.lstrip().startswith(f"{field}:")]
    if len(hits) == 1:
        return f"{path}:{hits[0]}: {error}"
    return f"{path}: {error}"


@playbook_app.command("create")
def playbook_create(
    name: str = typer.Argument(..., help="Name for the new playbook (its directory name)"),
    input_text: str = typer.Option(None, "--input", "-i", help="Describe the workflow to capture"),
    from_files: list[Path] = typer.Option(None, "--from", help="File(s) whose content describes the workflow"),
):
    """Generate a playbook into the user layer; it starts disabled for review."""
    pieces = [input_text] if input_text else []
    for f in from_files or []:
        pieces.append(f.read_text(encoding="utf-8"))
    if not pieces:
        err_console.print("[red]Nothing to generate from: pass --input and/or --from FILE.[/red]")
        raise typer.Exit(code=1)

    import re

    from raven.playbook.types import NAME_RE

    if not re.fullmatch(NAME_RE, name):
        err_console.print(f"[red]Playbook names are kebab-case ({escape(NAME_RE)}); got {escape(repr(name))}.[/red]")
        raise typer.Exit(code=1)
    config = _load_config()
    store = _store(config)
    if store.origin_of(name) is not None:
        err_console.print(f"[red]Playbook {escape(repr(name))} already exists ({store.origin_of(name)}).[/red]")
        raise typer.Exit(code=1)

    from raven.agent.subagent.registry import AgentRegistry
    from raven.playbook import PlaybookGenerator, agent_profiles_from_registry, live_inventory
    from raven.providers.factory import make_provider

    registry = AgentRegistry()
    registry.apply(config.subagents.agents)
    generator = PlaybookGenerator(
        make_provider(config),
        None,
        lambda: agent_profiles_from_registry(registry),
        # No tool registry is built on this path, so only the mcp half is known;
        # an unknown *tool* was never checked here anyway (``check_assets`` reads
        # skills and mcps).
        live_inventory(config.tools.mcp_servers),
        model=config.playbooks.model,
    )
    generated = asyncio.run(generator.generate("\n\n".join(pieces)))
    spec = generated.spec.model_copy(update={"name": name})
    path = store.save(spec, notes=generated.notes)
    console.print(f"[green]Created[/green] {escape(str(path))}")
    if generated.notes:
        console.print("Open questions for your review:")
        for note in generated.notes:
            console.print(f"  - {escape(note)}")
    # Not switched off on arrival, matching the tool entry: the deny list is
    # read live now, so writing the name onto it here would make a playbook the
    # user just created immediately invisible to a running agent.
    console.print(f"Usable now. Review the file, and `raven playbook disable {name}` if you want it held back.")


@playbook_app.command("enable")
def playbook_enable(name: str = typer.Argument(..., help="Playbook name, as listed")):
    """Remove a playbook from the disabled list, making it matchable."""
    from raven.config.update import set_playbook_disabled

    config = _load_config()
    _require_known(_store(config), name)
    changed = set_playbook_disabled(name, False)
    console.print(f"{escape(name)}: {'enabled' if changed else 'already enabled'}")


@playbook_app.command("disable")
def playbook_disable(name: str = typer.Argument(..., help="Playbook name, as listed")):
    """Add a playbook to the disabled list (builtins too); it stays explicitly runnable."""
    from raven.config.update import set_playbook_disabled

    config = _load_config()
    _require_known(_store(config), name)
    changed = set_playbook_disabled(name, True)
    console.print(f"{escape(name)}: {'disabled' if changed else 'already disabled'}")


secret_app = typer.Typer(help="The secret params this machine holds for a playbook's carried MCP servers")
playbook_app.add_typer(secret_app, name="secret")


@secret_app.command("set")
def playbook_secret_set(
    name: str = typer.Argument(..., help="Playbook name, as listed"),
    param: str = typer.Argument(..., help="A param the playbook declares with type: secret"),
    value: Optional[str] = typer.Option(
        None,
        "--value",
        help="The value. Omit to be prompted without echo -- a value on the command line is visible in `ps`.",
    ),
):
    """Store one secret param for a playbook on this machine.

    The value lands in the Raven credentials root (RAVEN_HOME) under
    playbooks/<name>/, mode 0600, and is read at load, so a run no longer has to
    be handed it -- and it never enters a conversation or the playbook file.
    """
    from raven.playbook.credentials import set_secret_param

    config = _load_config()
    store = _store(config)
    _require_known(store, name)
    spec = store.load(name)
    declared = spec.params.get(param)
    if declared is None or declared.type != "secret":
        err_console.print(f"[red]{escape(name)} declares no secret param named {escape(param)}.[/red]")
        raise typer.Exit(code=1)
    if value is None:
        value = typer.prompt(f"{param}", hide_input=True)
    set_secret_param(name, param, value)
    console.print(f"stored {escape(param)} for {escape(name)}")


@secret_app.command("clear")
def playbook_secret_clear(
    name: str = typer.Argument(..., help="Playbook name, as listed"),
    param: str = typer.Argument(..., help="The param to forget"),
):
    """Forget one stored secret param."""
    from raven.playbook.credentials import clear_secret_param

    config = _load_config()
    _require_known(_store(config), name)
    clear_secret_param(name, param)
    console.print(f"cleared {escape(param)} for {escape(name)}")


@playbook_app.command("auth")
def playbook_auth(
    name: str = typer.Argument(..., help="Playbook name, as listed"),
    server: str = typer.Argument(..., help="A server the playbook carries with auth: oauth"),
):
    """Run the browser OAuth flow for a server this playbook carries.

    Tokens land under the playbook's own credential scope, never under the
    host's, so a carried server and a host server of one name stay apart.
    """
    from raven.playbook.credentials import credential_scope

    config = _load_config()
    store = _store(config)
    _require_known(store, name)
    spec = store.load(name)
    cfg = (spec.mcp_servers or {}).get(server)
    if cfg is None:
        err_console.print(f"[red]{escape(name)} carries no MCP server named {escape(server)}.[/red]")
        raise typer.Exit(code=1)
    if cfg.auth != "oauth":
        err_console.print(f"[red]{escape(server)} has auth={cfg.auth!r}; only an oauth server is authorized.[/red]")
        raise typer.Exit(code=1)

    async def _run() -> dict:
        from raven.agent.tools.registry import ToolRegistry
        from raven.mcp.manager import MCPConnectionManager

        mgr = MCPConnectionManager(ToolRegistry(), credential_scope=credential_scope(name))
        try:
            return await mgr.connect(server, cfg, interactive=True)
        finally:
            await mgr.aclose()

    console.print(f"authorizing {escape(server)} for {escape(name)} -- your browser will open...")
    snap = asyncio.run(_run())
    if snap.get("state") == "connected":
        console.print(f"[green]authorized[/green] -- {snap.get('tool_count', 0)} tools, tokens saved")
    else:
        err_console.print(
            f"[red]authorization failed[/red] ({snap.get('state')}): {escape(str(snap.get('error') or ''))}"
        )
        raise typer.Exit(code=1)


@playbook_app.command("run")
def playbook_run(
    name: str = typer.Argument(..., help="Playbook name, as listed"),
    params: list[str] = typer.Argument(None, metavar="[K=V ...]", help="Parameter values"),
    fill: list[str] = typer.Option(
        None,
        "--fill",
        metavar="NODE.FIELD=VALUE",
        help="Fill a node field the playbook left blank, e.g. --fill draft.promptTemplate='...'",
    ),
):
    """Run one playbook to completion, in this process, and print the result.

    The explicit entry: it works on disabled playbooks too, because disabling only
    takes a playbook out of what the model is offered and this is the user's own
    hand. Runs synchronously -- for long graphs prefer asking the agent, which
    dispatches in the background.

    ``--fill`` is how this path supplies what a playbook left for a model to
    write. Without it, such a playbook simply cannot run here: there is no model
    in the room to compose the missing prompt, and running a graph with a blank
    step would dispatch a sub-agent with nothing to do. The gap is reported with
    the exact flags to pass instead.
    """
    values: dict[str, str] = {}
    for pair in params or []:
        key, eq, value = pair.partition("=")
        if not eq or not key:
            err_console.print(f"[red]Parameters are K=V pairs, got {escape(repr(pair))}.[/red]")
            raise typer.Exit(code=1)
        values[key] = value

    fills: dict[str, dict[str, str]] = {}
    for pair in fill or []:
        target, eq, value = pair.partition("=")
        node_id, dot, field_name = target.partition(".")
        if not (eq and dot and node_id and field_name):
            err_console.print(f"[red]--fill takes NODE.FIELD=VALUE, got {escape(repr(pair))}.[/red]")
            raise typer.Exit(code=1)
        fills.setdefault(node_id, {})[field_name] = value

    config = _load_config()
    store = _store(config)
    _require_known(store, name)

    from raven.agent.subagent.dag_tool import SubAgentDagTool
    from raven.agent.subagent.manager import SubagentManager
    from raven.playbook import PlaybookExecutor, PlaybookRuntime, agent_profiles_from_registry
    from raven.playbook.credentials import credential_scope, stored_secret_params
    from raven.playbook.mcp import (
        declared_mcp_names,
        playbook_mcp_servers,
        preflight_mcp_source,
        unusable_servers,
    )
    from raven.playbook.params import resolve_params, secret_param_names
    from raven.providers.factory import make_provider

    provider = make_provider(config)
    manager = SubagentManager(
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        exec_config=config.tools.exec,
        agents=config.subagents.agents,
    )
    # The manager's table, so a node here resolves to the same agent it would in
    # a conversation -- built-in rows included, which is what lets the CLI run a
    # playbook at all now that no synthetic per-node backend is built for it.
    dag_tool = SubAgentDagTool(
        workspace=config.workspace_path,
        registry=manager.registry,
        guide_skill_id=None,
        state_for=manager.instance_state,
    )
    executor = PlaybookExecutor(
        dag_tool=dag_tool,
        provider=provider,
        compose_model=config.playbooks.model,
        # The project a multi-round plan works, which is the repository the
        # person is standing in -- not the agent home above, which is where runs
        # and plan files are kept and is nobody's project.
        workspace=Path.cwd(),
        background=False,
        # This path composes a prompt-mode graph itself. In a conversation the
        # caller is a model and gets the guidance to compose from; here there is
        # nobody to hand it to, so without this the CLI would lose the ability to
        # run a prompt-mode playbook at all.
        compose_prompt_mode=True,
    )
    executor.set_agent_profiles(lambda: agent_profiles_from_registry(manager.registry))
    runtime = PlaybookRuntime(
        store=store,
        executor=executor,
        disabled=config.playbooks.disabled,
    )

    # Nothing wired an MCP source on this path, so a node's `mcps` resolved to
    # "not connected on the host" however well the machine was configured. The
    # spec is read here rather than taken from the runtime because the pre-flight
    # has to know what to dial before the graph starts; a file that cannot be
    # parsed is left to the runtime, which reports it below.
    try:
        spec = store.load(name)
    except Exception:  # noqa: BLE001 - the runtime reports an unloadable file
        spec = None

    async def run_with_mcp():
        if spec is None:
            return await runtime.load(name, values, fills, allow_disabled=True)
        # The same precedence the conversation path applies: a secret this
        # machine stores stands in for one the command line did not supply.
        param_values, _ = resolve_params(spec, {**stored_secret_params(spec), **values})
        secrets = secret_param_names(spec)
        declared = declared_mcp_names(spec, fills)
        async with preflight_mcp_source(
            host_servers=config.tools.mcp_servers,
            playbook_servers=playbook_mcp_servers(spec, param_values),
            declared=declared,
            disabled_tools=lambda: frozenset(config.tools.disabled_tools),
            sandbox_config=config.tools.sandbox,
            workspace=config.workspace_path,
            secret_values=[param_values.get(pname, "") for pname in secrets],
            credential_scope=credential_scope(spec.name) if spec.mcp_servers else None,
        ) as source:
            for server, state in unusable_servers(source, declared or ()):
                hint = ""
                if state == "auth_required":
                    fix = (
                        f"raven playbook auth {name} {server}"
                        if server in spec.mcp_servers
                        else f"raven plugin auth {server}"
                    )
                    hint = f" -- run: {fix}"
                err_console.print(f"[yellow]MCP server {escape(server)}: {escape(state)}{escape(hint)}[/yellow]")
            manager.set_mcp_source(source)
            try:
                plan = await runtime.load(name, values, fills, allow_disabled=True)
                # Inside the pre-flight, not after it: a stint's roles reach the
                # servers it declared, and holding the process open outside this
                # scope would hold it open with the connections already closed.
                if plan is not None and getattr(spec, "mode", "") == "stint":
                    console.print(plan.reply, markup=False, soft_wrap=True)
                    console.print(
                        "[dim]Holding this terminal: a stint's rounds run in this process, and nothing "
                        "else here would advance them. Ctrl-C stops it where it is; "
                        "`raven playbook stints resume` takes it up.[/dim]"
                    )
                    await hold_until_the_stints_end(executor.rounds)
                    return None
                return plan
            finally:
                # The source outlives nothing: its connections close with the
                # pre-flight, and a backend still holding it would resolve
                # grants against a manager that has already let go.
                manager.set_mcp_source(None)

    plan = asyncio.run(run_with_mcp())
    if plan is None:
        err_console.print(f"[red]Playbook {escape(repr(name))} did not load; see the log for the parse error.[/red]")
        raise typer.Exit(code=1)
    if plan.kind == "gaps":
        # Named as an unrunnable-here condition rather than a generic failure: the
        # playbook is fine, this entry point just has nobody to fill it in.
        err_console.print("[yellow]This playbook expects values to be supplied at run time.[/yellow]", soft_wrap=True)
    # Payload, not styling: a graph result legitimately contains brackets.
    console.print(plan.reply, markup=False, soft_wrap=True)
    if plan.kind != "dag":
        raise typer.Exit(code=1)


@playbook_app.command("delete")
def playbook_delete(
    name: str = typer.Argument(..., help="Playbook name, as listed"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirm prompt"),
):
    """Delete a user playbook. Builtins cannot be deleted — disable them instead."""
    from raven.config.update import set_playbook_disabled

    config = _load_config()
    store = _store(config)
    origin = _require_known(store, name)
    if origin == "builtin":
        err_console.print(
            f"[red]{escape(repr(name))} is a builtin and cannot be deleted. "
            f"Use: raven playbook disable {escape(name)}[/red]"
        )
        raise typer.Exit(code=1)
    directory = store.path_for(name).parent
    if not yes and not typer.confirm(f"Delete {directory}?"):
        raise typer.Exit(code=1)
    shutil.rmtree(directory)
    # A gone name has no business on the deny list; dropping it also means a
    # future playbook reusing the name starts enabled, like any new one.
    set_playbook_disabled(name, False)
    if store.origin_of(name) == "builtin":
        console.print(f"Deleted the user playbook {escape(repr(name))}; the builtin of the same name is visible again.")
    else:
        console.print(f"Deleted {escape(repr(name))}.")


stints_app = typer.Typer(help="Multi-round runs a `mode: stint` playbook started")
playbook_app.add_typer(stints_app, name="stints")

# The project side of the same feature, under the singular: `stints` answers
# "what has run on this machine", and `stint` acts on one project's own layout
# and the backlog its roles share. One family, because a person who found
# `raven playbook run` has no reason to guess at a second top-level noun.
playbook_app.add_typer(stint_app, name="stint")


def _stint_homes(config):
    """Every conversation on this machine, as (its directory, its stint store).

    A stint is kept beside the conversation that started it, and the person
    asking here has a terminal rather than a conversation. Reading only the
    keyless session's store is what made a stint started in the TUI invisible to
    the commands that exist to watch it -- and invisible in the way that reads
    as "nothing has run", not as "you are looking in the wrong place".

    The directory comes back beside the store because deriving it from the
    session key is the thing that does not work here. A conversation launched in
    a project is grouped under that project, and this process has no project, so
    the derivation lands in a directory nothing has ever written to. Searching
    finds the stint; whoever then opens a round is handed the directory rather
    than deriving it again and writing the round somewhere else.

    Still only files: a stint is read far more often than it is run -- what round
    is it on, what did it undo, what is it waiting for -- and constructing the
    sub-agent stack to answer would make `stint list` cost what a dispatch costs.
    """
    from raven.agent.subagent.history import dag_root
    from raven.session.manager import SessionManager
    from raven.stint.record import STINTS_DIRNAME, StintStore

    sessions = SessionManager(config.workspace_path).sessions_dir
    found = []
    for home in sorted(sessions.glob("*/*")):
        root = dag_root(home) / STINTS_DIRNAME
        if home.is_dir() and root.is_dir():
            found.append((home, StintStore(root)))
    return found


def _stint_stores(config):
    """Every stint store on this machine, for a caller with no use for the rest."""
    return [store for _home, store in _stint_homes(config)]


def _all_stints(config):
    """Every stint on the machine, newest first, across conversations.

    Stints whose holder has gone quiet are marked on the way past. A record is
    the only claim a stint makes about itself and a host that died mid-round
    writes nothing on its way out, so without this the list reports a corpse as
    work in progress -- and a person waits for a notification nobody will send.
    Marking only: taking one up again is `stints resume`, which is a person's
    call because it spends money and hours.
    """
    from raven.stint.record import mark_adrift

    stores = _stint_stores(config)
    mark_adrift(stores)
    found = [record for store in stores for record in store.list()]
    found.sort(key=lambda record: (record.started_at_ms, record.stint_id), reverse=True)
    return found


def _require_stint(config, stint_id: str):
    """The stint, the store holding it, and the conversation directory both are in.

    The store so a writer writes back where it read; the directory so a verb
    that opens a round puts the round where the stint's earlier ones are.
    """
    from raven.stint.record import mark_adrift

    homes = _stint_homes(config)
    mark_adrift([store for _home, store in homes])
    for home, store in homes:
        record = store.read(stint_id)
        if record is not None:
            return home, store, record
    err_console.print(f"[red]No stint {escape(stint_id)} on this machine.[/red]")
    raise typer.Exit(code=1)


@stints_app.command("list")
def plan_list():
    """Every stint this machine has started, newest first."""
    records = _all_stints(_load_config())
    if not records:
        console.print("[dim]No stints have been started here.[/dim]")
        return
    table = Table(title=f"Stints ({len(records)})")
    table.add_column("Stint", style="cyan")
    table.add_column("Playbook", style="green")
    table.add_column("Round")
    table.add_column("State")
    table.add_column("Why it stopped", overflow="fold")
    for record in records:
        # The status, not `live`. `live` answers "should something be advancing
        # this", which is true of an interrupted stint too -- printing it as
        # `running` is the lie this column exists to stop telling.
        state = f"[yellow]{record.status}[/yellow]" if record.live else record.status
        table.add_row(
            escape(record.stint_id),
            escape(record.playbook),
            str(record.round_index),
            state,
            escape(record.stop_reason or ""),
        )
    console.print(table)


@stints_app.command("get")
def plan_get(stint_id: str = typer.Argument(..., help="Stint id, as listed")):
    """One stint in full: its rounds, what was undone, and what it is waiting on."""
    *_, record = _require_stint(_load_config(), stint_id)
    console.print(f"[cyan]{escape(record.stint_id)}[/cyan]  {escape(record.playbook)}  [{escape(record.status)}]")
    console.print(f"working in {escape(record.workdir)}" + (f" on {escape(record.branch)}" if record.branch else ""))
    if record.stop_reason:
        console.print(f"stopped because {escape(record.stop_reason)}")
    rounds = Table(title="Rounds")
    rounds.add_column("N")
    rounds.add_column("Run", style="dim")
    rounds.add_column("State")
    rounds.add_column("Checks")
    rounds.add_column("Undone")
    for entry in record.rounds:
        checks = ", ".join(f"{row.get('name')}={row.get('status')}" for row in entry.verify) or "-"
        rounds.add_row(
            str(entry.index),
            escape(entry.run_id or "-"),
            escape(entry.status),
            escape(checks),
            str(len(entry.violations)),
        )
    console.print(rounds)
    for entry in record.rounds:
        for note in entry.violations:
            console.print(f"[yellow]round {entry.index}: {escape(note)}[/yellow]")
    for position, question in enumerate(record.questions):
        answered = str(question.get("answer") or "").strip()
        mark = "[green]answered[/green]" if answered else "[red]waiting[/red]"
        console.print(f"{position}. {mark} round {question.get('round')} {escape(str(question.get('text')))}")
        if answered:
            console.print(f"   -> {escape(answered)}")


@stints_app.command("stop")
def plan_stop(stint_id: str = typer.Argument(..., help="Stint id, as listed")):
    """Open no further rounds. A round already running finishes first."""
    from raven.stint.record import STOPPED

    _, store, record = _require_stint(_load_config(), stint_id)
    if not record.live:
        console.print(f"{escape(stint_id)} is already {escape(record.status)}.")
        return
    record.status = STOPPED
    record.stop_reason = "a person stopped the stint"
    store.write(record)
    console.print(
        f"Stopped {escape(stint_id)}. A round already in flight finishes and reports; no further round opens."
    )


@stints_app.command("pause")
def plan_pause(stint_id: str = typer.Argument(..., help="Stint id, as listed")):
    """Open no further rounds, and keep the stint so `resume` can take it up."""
    from raven.stint.record import PAUSED

    _, store, record = _require_stint(_load_config(), stint_id)
    if not record.unfinished:
        console.print(f"{escape(stint_id)} is already {escape(record.status)}.")
        return
    record.status = PAUSED
    record.stop_reason = "a person paused the stint"
    store.write(record)
    console.print(
        f"Paused {escape(stint_id)}. A round already in flight finishes; no further round opens. "
        f"Take it up again with `raven playbook stints resume {escape(stint_id)}`."
    )


@stints_app.command("answer")
def plan_answer(
    stint_id: str = typer.Argument(..., help="Stint id, as listed"),
    question: int = typer.Option(..., "--question", "-q", help="Which question, by its number in `stint get`"),
    text: str = typer.Option(..., "--text", "-t", help="The answer, as the next round should read it"),
):
    """Answer a question a round left. It reaches the round after this one."""
    _, store, record = _require_stint(_load_config(), stint_id)
    if not 0 <= question < len(record.questions):
        err_console.print(f"[red]{escape(stint_id)} has no question {question}; see `playbook stint get`.[/red]")
        raise typer.Exit(code=1)
    record.questions[question]["answer"] = text
    record.questions[question]["answered_at"] = int(time.time() * 1000)
    store.write(record)
    console.print(f"Answered. The next round of {escape(stint_id)} reads it.")


def _stint_driver(config, home: Path):
    """The rounds driver, with every root pointed at the conversation `home`.

    A stint verb that opens a round needs the whole sub-agent stack behind it and
    not just the stint file, because a round is an ordinary graph and goes out
    through the ordinary entry.

    ``session_dir`` is injected rather than left to the tool's own derivation.
    The tool derives a conversation's directory from its key through a slug-less
    session manager, which is the gateway's grouping and not the one a
    conversation launched in a project has -- so from a terminal the stint file,
    the round's runs and its node artifacts would each be looked for under
    ``sessions/<channel>/``, where nothing has ever written. `_stint_homes`
    searched and found the real one; this is that answer, handed over rather
    than derived a second time.
    """
    from raven.agent.subagent.dag_tool import SubAgentDagTool
    from raven.agent.subagent.manager import SubagentManager
    from raven.playbook.executor import PlaybookExecutor
    from raven.providers.factory import make_provider

    manager = SubagentManager(
        provider=make_provider(config),
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        exec_config=config.tools.exec,
        agents=config.subagents.agents,
    )
    dag_tool = SubAgentDagTool(
        workspace=config.workspace_path,
        registry=manager.registry,
        guide_skill_id=None,
        state_for=manager.instance_state,
        session_dir=lambda _key: home,
    )
    driver = PlaybookExecutor(dag_tool=dag_tool, workspace=Path.cwd()).rounds
    if driver is None:
        err_console.print("[red]This build has no multi-round driver.[/red]")
        raise typer.Exit(code=1)
    return driver


async def hold_until_the_stints_end(driver, *, every_sec: float = 2.0) -> None:
    """Keep this process alive while a stint it just started still has rounds.

    A stint dispatches each round in the background and opens the next one from
    a callback on the finished run's own task. On a gateway that is somebody
    else's problem -- the host outlives the turn. Here the command *is* the
    host, and returning from ``asyncio.run`` closes the loop out from under the
    round: the receipt says it started, the record says ``running``, and nothing
    ever ran. Held here rather than dispatched in the foreground because the
    hand-over that opens round two only exists on the background path.

    Ctrl-C leaves the stint where it is. The round in flight dies with this
    process and the record stops being touched, so the next reader marks it
    interrupted and `stints resume` takes it up from the node it reached.
    """
    import asyncio as _asyncio

    store = driver.store_for(None)
    while True:
        await _asyncio.sleep(every_sec)
        if not [record for record in store.list() if record.live]:
            return


def _held_here(driver, awaited: "Awaitable[str]") -> str:
    """Say why the terminal is not coming back yet, then hold it."""
    import asyncio as _asyncio

    async def go() -> str:
        text = await awaited
        if [record for record in driver.store_for(None).list() if record.live]:
            console.print(text, markup=False, soft_wrap=True)
            console.print(
                "[dim]Holding this terminal: a stint's rounds run in this process, and nothing else "
                "here would advance them. Ctrl-C stops it where it is; `stints resume` takes it up.[/dim]"
            )
            await hold_until_the_stints_end(driver)
            return ""
        return text

    return _asyncio.run(go())


def _stint_session(record) -> str | None:
    """The session the stint was started in, not this terminal's absence of one.

    The driver reads the stint from that conversation's store and submits into
    the same one, where the nodes its earlier rounds left still are.
    """
    return str(record.origin.get("session_key") or "") or None


@stints_app.command("extend")
def plan_extend(
    stint_id: str = typer.Argument(..., help="Stint id, as listed"),
    rounds: int = typer.Option(..., "--rounds", "-n", help="How many more rounds to allow"),
):
    """Give a stint more rounds. A stint that is over opens the next one here.

    For the stint that ran its budget out with work still worth doing. It keeps
    the checkout, the branch and the journal it already has -- starting a second
    stint instead would cut a fresh worktree from the project's HEAD and begin
    again from before this one's first commit.
    """

    config = _load_config()
    home, _, record = _require_stint(config, stint_id)
    driver = _stint_driver(config, home)
    answer = _held_here(driver, driver.extend(stint_id, rounds, _stint_session(record)))
    if answer:
        console.print(escape(answer))


@stints_app.command("sweep")
def stint_sweep():
    """Take up every stint here whose holder is gone.

    The one move for "the machine restarted and I want my work back". Reading a
    list marks them; this is the step that spends money, which is why it is a
    verb rather than something a read does on a person's behalf. A stint another
    process is still beating for is left where it is.
    """
    config = _load_config()
    taken: list[str] = []
    for home, _store in _stint_homes(config):
        driver = _stint_driver(config, home)
        taken.extend(asyncio.run(driver.sweep(None)))
    if not taken:
        console.print("[dim]Nothing here was left in flight.[/dim]")
        return
    for stint_id in taken:
        console.print(f"[green]took up[/green] {escape(stint_id)}")


@stints_app.command("resume")
def plan_resume(stint_id: str = typer.Argument(..., help="Stint id, as listed")):
    """Take a stint up again from the node it stopped at.

    For the stint whose gateway died mid-round. The roles that finished are named
    rather than re-run, so what they produced is still what the rest of the
    round reads.
    """

    config = _load_config()
    home, _, record = _require_stint(config, stint_id)
    # `unfinished` rather than `live`, which excludes a paused stint: pausing
    # says to take it up later with this command, and a guard that then refused
    # it would make that instruction false.
    if not record.unfinished:
        console.print(f"{escape(stint_id)} is {escape(record.status)} and has nothing left to take up.")
        return
    driver = _stint_driver(config, home)
    answer = _held_here(driver, driver.resume(stint_id, _stint_session(record)))
    if answer:
        console.print(escape(answer))
