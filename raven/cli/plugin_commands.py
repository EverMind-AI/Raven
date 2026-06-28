"""``raven plugins`` — inspect installed memory / context plugins.

Reads ``RavenConfig.plugins`` + the live :class:`PluginRegistry`
and prints a table of activated plugins, what each contributes, and
which memory backend the current config selects.

Use cases:

- "Where did this plugin come from?" — the command lists discovery
  sources (bundled / user / project / entry_points) so the user can see
  where each plugin was resolved from.
- "Why isn't my plugin loading?" — disabled / failed-to-activate
  entries surface here.
- "Which backend is actually active?" — shows the
  ``config.memory.backend`` selection resolved against the registry.

This is a **read-only** command — no plugin code is invoked beyond
manifest parsing. ``MemoryBackend.start()`` is not awaited, so no
network / disk I/O happens against the plugin's runtime systems.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from raven.cli._helpers import load_runtime_config

console = Console()


def register(app: typer.Typer) -> None:
    """Attach the ``plugins`` command to ``app``."""

    @app.command()
    def plugins(
        config_path: Optional[str] = typer.Option(
            None, "--config", "-c",
            help="Path to config file (default: ~/.raven/config.json)",
        ),
        verbose: bool = typer.Option(
            False, "--verbose", "-v",
            help="Show manifest paths + factory references.",
        ),
    ) -> None:
        """List installed plugins + the active memory backend."""
        # Import lazily so ``raven --help`` doesn't pay for plugin
        # discovery on every invocation.
        from raven.cli._plugin_stack import plugin_discovery_sources
        from raven.config.raven import load_raven_config
        from raven.plugin import (
            PluginDiscovery,
            PluginRegistry,
            Source,
        )

        ec_config = _load_ec_config(config_path)

        # Discover separately from activation so the table can show
        # both shadowed (lower-priority) plugins AND disabled ones,
        # not just the live set. Same four sources the live boot scans.
        discovery = PluginDiscovery(**plugin_discovery_sources())
        discovered = discovery.discover()

        registry = PluginRegistry()
        disabled = frozenset(ec_config.plugins.disabled)
        registry.activate(discovered, disabled=disabled)

        _render_plugin_table(discovered, registry, disabled, verbose=verbose)
        _render_backend_selection(ec_config, registry)


def _load_ec_config(config_path: str | None):
    """Load RavenConfig with the same fallback the other CLI
    commands use. Lazy import so module import is cheap."""
    from raven.config.raven import load_raven_config

    # ``load_runtime_config`` is the canonical base-config loader; we
    # need the extension blocks too, so pull via the dedicated
    # raven loader. ``load_runtime_config`` is invoked for parity
    # with other CLI commands (sets ``set_config_path`` so downstream
    # readers see the same file).
    load_runtime_config(config_path)
    return load_raven_config(
        Path(config_path) if config_path else None,
    )


def _render_plugin_table(
    discovered,
    registry,
    disabled,
    *,
    verbose: bool,
) -> None:
    """Print one row per discovered plugin with its status."""
    from raven.plugin import Source

    if not discovered:
        console.print(
            "[yellow]No plugins discovered.[/yellow] The everos backend "
            "ships bundled — run [bold]uv sync[/bold] — or drop a manifest "
            "under [bold]~/.raven/plugin/[/bold].",
        )
        return

    table = Table(
        title="Raven plugins",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Plugin ID")
    table.add_column("Version")
    table.add_column("Source")
    table.add_column("Status")
    table.add_column("Memory backends")
    if verbose:
        table.add_column("Factory")

    activated_ids = set(registry.activated_ids())
    for record in discovered:
        mf = record.manifest
        pid = mf.id
        if pid in disabled:
            status = "[red]disabled[/red]"
        elif pid in activated_ids:
            status = "[green]activated[/green]"
        elif not mf.enabled_by_default:
            status = "[dim]inactive (opt-in)[/dim]"
        else:
            status = "[yellow]not activated[/yellow]"
        backends = ", ".join(
            c.name for c in mf.contributes.memory_backends
        ) or "(none)"

        row = [
            pid,
            mf.version,
            _source_label(record.source),
            status,
            backends,
        ]
        if verbose:
            factories = "; ".join(
                c.factory for c in mf.contributes.memory_backends
            )
            row.append(factories or "(none)")
        table.add_row(*row)
    console.print(table)


def _source_label(source) -> str:
    """Friendly label for a :class:`Source` enum value."""
    from raven.plugin import Source

    return {
        Source.ENTRY_POINTS: "entry_points",
        Source.PROJECT: "project",
        Source.USER: "user",
        Source.BUNDLED: "bundled",
    }.get(source, str(source))


def _render_backend_selection(ec_config, registry) -> None:
    """Show which memory backend the current config activates."""
    selected = ec_config.memory.backend
    if selected is None:
        console.print(
            "\n[bold]Active memory backend:[/bold] [dim]none[/dim] "
            "([italic]memory.backend is null — AgentLoop uses its "
            "legacy memory path[/italic])",
        )
        return

    available = registry.memory_backend_names()
    if selected not in available:
        console.print(
            f"\n[bold]Active memory backend:[/bold] [red]{selected}[/red] "
            f"[red](not available)[/red]",
        )
        console.print(
            f"  [dim]Registered: {', '.join(available) or '(none)'}[/dim]",
        )
        console.print(
            "  [dim]AgentLoop will fall back to the legacy memory path.[/dim]",
        )
        return

    # Find the plugin id that contributes the selected backend
    owner_id = None
    for pid in registry.activated_ids():
        mf = registry.manifest_for(pid)
        if mf is None:
            continue
        for c in mf.contributes.memory_backends:
            if c.name == selected:
                owner_id = pid
                break
        if owner_id:
            break

    console.print(
        f"\n[bold]Active memory backend:[/bold] [green]{selected}[/green] "
        f"[dim](from plugin: {owner_id})[/dim]",
    )
    console.print(
        f"  [dim]User id:  {ec_config.memory.user_id}[/dim]",
    )
    console.print(
        f"  [dim]Agent id: {ec_config.memory.agent_id}[/dim]",
    )
