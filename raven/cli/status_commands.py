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
            from raven.config.update_providers import list_providers

            console.print(f"Model: {config.agents.defaults.model}")

            # Names from the listing, values from the loaded config. The listing
            # covers vendors Raven carries no spec for -- reached by name alone,
            # and a registry walk reports a working setup as unconfigured -- while
            # the loaded config is the only view that includes credentials
            # supplied by environment variable, which the file on disk does not
            # hold. Reading either one alone shows a configured provider as unset.
            for info in list_providers():
                label = info["display_name"] or info["name"]
                section = config.providers.get(info["name"])
                if info["is_oauth"]:
                    # The token lives in a file, so the listing is the only source.
                    state = "[green]✓ (OAuth)[/green]" if info["configured"] else "[dim]not set (OAuth)[/dim]"
                elif info["is_local"]:
                    # Local deployments show api_base instead of api_key
                    api_base = (section.api_base if section else None) or info["api_base"]
                    state = f"[green]✓ {api_base}[/green]" if api_base else "[dim]not set[/dim]"
                else:
                    # `providers.auth`, like every other gate. Reading `api_key`
                    # here made this the one place that called Azure configured
                    # with a key and no address, and missed a Gemini section
                    # holding only `api_key_list`.
                    from raven.providers.auth import credential_status

                    status = credential_status(info["name"], section, include_external=True)
                    state = "[green]✓[/green]" if status.ok else "[dim]not set[/dim]"
                console.print(f"{label}: {state}")


__all__ = ["register"]
