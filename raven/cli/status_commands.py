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
        from raven.config.loader import (
            ConfigReadError,
            get_config_path,
            load_config,
            read_raw_or_raise,
        )

        config_path = get_config_path()
        config = load_config()
        workspace = config.workspace_path

        console.print(f"{__logo__} Raven Status\n")

        # Three states, not two: a present-but-unparseable file used to show the
        # same checkmark as a healthy one, right before the provider walk below
        # aborted on it. The detailed remedy still comes from that walk.
        if not config_path.exists():
            config_state = "[red]missing[/red]"
        else:
            try:
                read_raw_or_raise(config_path)
            except ConfigReadError:
                config_state = "[red]invalid[/red]"
            else:
                config_state = "[green]✓[/green]"
        console.print(f"Config: {config_path} {config_state}")
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
            # Only configured providers get a row; the rest fold into one count
            # so a single-provider setup is not buried under 20 'not set' rows.
            unconfigured = 0
            for info in list_providers():
                label = info["display_name"] or info["name"]
                section = config.providers.get(info["name"])
                if info["is_oauth"]:
                    # The token lives in a file, so the listing is the only source.
                    if not info["configured"]:
                        unconfigured += 1
                        continue
                    state = "[green]✓ (OAuth)[/green]"
                elif info["is_local"]:
                    # Local deployments show api_base instead of api_key
                    api_base = (section.api_base if section else None) or info["api_base"]
                    if not api_base:
                        unconfigured += 1
                        continue
                    state = f"[green]✓ {api_base}[/green]"
                else:
                    # `providers.auth`, like every other gate. Reading `api_key`
                    # here made this the one place that called Azure configured
                    # with a key and no address, and missed a Gemini section
                    # holding only `api_key_list`.
                    from raven.providers.auth import credential_status

                    if not credential_status(info["name"], section, include_external=True).ok:
                        unconfigured += 1
                        continue
                    state = "[green]✓[/green]"
                console.print(f"{label}: {state}")
            if unconfigured:
                console.print(
                    f"[dim]{unconfigured} providers not configured (raven provider list to see all)[/dim]"
                )


__all__ = ["register"]
