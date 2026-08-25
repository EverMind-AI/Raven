"""Top-level ``status`` command — show config / workspace / provider status."""

from __future__ import annotations

import typer
from rich.console import Console

from raven import __logo__

console = Console()


def register(app: typer.Typer) -> None:
    """Attach the ``status`` command to ``app``."""

    @app.command()
    def status():
        """Show Raven status."""
        from raven.config.loader import get_config_path, load_config

        config_path = get_config_path()
        config = load_config()
        workspace = config.workspace_path

        console.print(f"{__logo__} Raven Status\n")

        console.print(f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}")
        console.print(f"Workspace: {workspace} {'[green]✓[/green]' if workspace.exists() else '[red]✗[/red]'}")

        if config_path.exists():
            from raven.providers.registry import PROVIDERS

            console.print(f"Model: {config.agents.defaults.model}")

            # Check API keys from registry
            for spec in PROVIDERS:
                p = getattr(config.providers, spec.name, None)
                if p is None:
                    continue
                if spec.is_oauth:
                    console.print(f"{spec.label}: [green]✓ (OAuth)[/green]")
                elif spec.is_local:
                    # Local deployments show api_base instead of api_key
                    if p.api_base:
                        console.print(f"{spec.label}: [green]✓ {p.api_base}[/green]")
                    else:
                        console.print(f"{spec.label}: [dim]not set[/dim]")
                else:
                    has_key = bool(p.api_key)
                    console.print(f"{spec.label}: {'[green]✓[/green]' if has_key else '[dim]not set[/dim]'}")


__all__ = ["register"]
