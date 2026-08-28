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
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

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
    from raven.cli._helpers import make_provider
    from raven.playbook import PlaybookGenerator, agent_profiles_from_registry, live_inventory

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
    from raven.cli._helpers import make_provider
    from raven.playbook import PlaybookExecutor, PlaybookRuntime, agent_profiles_from_registry

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
    plan = asyncio.run(runtime.load(name, values, fills, allow_disabled=True))
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
