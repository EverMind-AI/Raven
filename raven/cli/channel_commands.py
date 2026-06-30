"""Channel subcommands — owns the ``channels_app`` Typer instance.

This module bundles all ``raven channels ...`` subcommands:

Lifecycle commands:

- ``channels status``              — show enabled/disabled state for every channel
- ``channels login``               — link device via QR code (WhatsApp bridge today;
                                     generalized in a follow-up commit)

Config subcommands:

- ``channels enable <name> [...]`` — flip ``enabled=True`` + optionally patch fields
- ``channels disable <name>``      — flip ``enabled=False`` (credentials preserved)
- ``channels set <name> [...]``    — patch specific fields
- ``channels get <name>``          — print current config (secrets redacted)
- ``channels reset <name>``        — restore schema defaults (key preserved)
- ``channels show <name>``         — reflect available ``--flag`` fields
- ``channels list``                — list every registered channel name

Architecture: write operations go ONLY through
:mod:`raven.config.update_channels`. Command bodies do not import
``load_config`` / ``save_config`` / channel Pydantic classes.

``commands.py`` imports :data:`channels_app` and registers it on the top-level
``app`` via ``app.add_typer(channels_app, name="channels")``.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from raven import __logo__

console = Console()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _help_requested(extra_args: list[str]) -> bool:
    """Detect ``--help`` / ``-h`` inside a free-form ``ctx.args`` list."""
    return any(t in ("--help", "-h") or t.startswith("--help=") for t in extra_args)


def _print_schema_table(name: str) -> None:
    """Render a channel's field-spec table.

    Shared by ``show``, ``enable --help`` / ``set --help`` interception, and
    the empty-flag fallback in ``enable`` / ``set``.
    """
    from raven.config.update_channels import channel_field_specs

    try:
        specs = channel_field_specs(name)
    except KeyError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(1)

    table = Table(title=f"Channel: {name}")
    table.add_column("Flag", style="cyan", no_wrap=True)
    table.add_column("Type", overflow="fold")
    table.add_column("Default", no_wrap=True)
    table.add_column("Secret?", no_wrap=True, justify="center")
    table.add_column("Description", overflow="fold")
    for path, spec in specs.items():
        flag = "--" + path.replace("_", "-")
        default = spec["default"]
        default_str = "" if default in (None, "") else str(default)
        table.add_row(
            flag,
            spec["type"],
            default_str,
            "✓" if spec["is_secret"] else "",
            spec.get("description", "") or "",
        )
    console.print(table)


def _warn_empty_credentials(name: str) -> None:
    """After a successful ``enable``, list any secret fields that are still empty.

    Plan §3.2.1: enable is registration of intent, not a health check —
    leave these as warnings, not errors.
    """
    from raven.config.update_channels import (
        channel_field_specs,
        get_channel_config,
    )

    specs = channel_field_specs(name)
    cfg = get_channel_config(name, redact_secrets=False)
    empty = [k for k, s in specs.items() if s["is_secret"] and cfg.get(k) in ("", None, [])]
    if empty:
        flags = ", ".join("--" + k.replace("_", "-") for k in empty)
        console.print(f"  [yellow]⚠ Empty credential fields:[/yellow] {flags}")


def _parse_channel_flags(extra_args: list[str], channel_name: str) -> dict:
    """Parse arbitrary ``--flag value`` pairs against a channel's Pydantic schema.

    Accepts six forms:

    - ``--token abc``           -> ``{"token": "abc"}``
    - ``--token=abc``           -> ``{"token": "abc"}``
    - ``--app-id X``            -> ``{"app_id": "X"}``       (kebab -> snake)
    - ``--dm.policy open``      -> ``{"dm.policy": "open"}`` (dotted path for nested)
    - ``--no-enabled``          -> ``{"enabled": False}``    (bool negative)
    - ``--enabled`` (no value)  -> ``{"enabled": True}``     (bool positive)

    Field names are validated against the channel's Pydantic schema via
    :func:`raven.config.update_channels.channel_field_specs`. Unknown
    fields raise ``typer.BadParameter`` pointing at ``channels show <name>``.
    """
    from raven.config.update_channels import channel_field_specs

    try:
        specs = channel_field_specs(channel_name)
    except KeyError as exc:
        raise typer.BadParameter(str(exc))

    def _normalize(flag: str) -> str:
        return ".".join(seg.replace("-", "_") for seg in flag.split("."))

    out: dict = {}
    i = 0
    while i < len(extra_args):
        tok = extra_args[i]
        if not tok.startswith("--"):
            raise typer.BadParameter(f"Expected --flag, got: {tok}")

        if "=" in tok:
            flag, value = tok[2:].split("=", 1)
            i += 1
        else:
            flag = tok[2:]
            nxt = extra_args[i + 1] if i + 1 < len(extra_args) else None
            if nxt is not None and not nxt.startswith("--"):
                value = nxt
                i += 2
            else:
                value = None
                i += 1

        if flag.startswith("no-") and value is None:
            key = _normalize(flag[3:])
            if key not in specs:
                raise typer.BadParameter(
                    f"Unknown field '--no-{flag[3:]}'. Run 'raven channels show {channel_name}' for available flags."
                )
            out[key] = False
            continue

        key = _normalize(flag)
        if key not in specs:
            raise typer.BadParameter(
                f"Unknown field '--{flag}' for channel '{channel_name}'. "
                f"Run 'raven channels show {channel_name}' for available flags."
            )

        if value is None:
            if specs[key]["type"] == "bool":
                out[key] = True
            else:
                raise typer.BadParameter(f"Missing value for --{flag}")
        else:
            out[key] = value

    return out


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------


def _register_config_commands(channels_app: typer.Typer) -> None:
    """Attach the channel config subcommands to the given ``channels_app``.

    Call once at module-init time from ``raven/cli/commands.py`` after
    ``channels_app`` is constructed. Not safe to call twice: typer's
    ``@command`` decorator silently appends to ``registered_commands``, so a
    second call shadows the first set with duplicates.
    """
    # `raven channels` (no subcommand) should print help, not "Missing command".
    channels_app.info.no_args_is_help = True

    base_help = channels_app.info.help or "Manage channels"
    if "channels list" not in base_help:
        channels_app.info.help = f"{base_help}\n\nRun 'raven channels list' to see available channels."

    @channels_app.command(
        "enable",
        context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    )
    def channels_enable_cmd(
        ctx: typer.Context,
        name: str = typer.Argument(..., help="Channel name (e.g. telegram, feishu)"),
    ):
        """Enable a channel; optionally fill credential fields inline.

        Examples:
            raven channels enable telegram --token 123:AAAA
            raven channels enable feishu --app-id X --app-secret Y
            raven channels enable slack --bot-token X --app-token Y --dm.policy open
        """
        if _help_requested(ctx.args):
            _print_schema_table(name)
            raise typer.Exit(0)

        from pydantic import ValidationError

        from raven.config.update_channels import enable_channel

        fields = _parse_channel_flags(ctx.args, name)
        if not fields:
            _print_schema_table(name)
            console.print("  [dim]Tip: re-run with one or more --flag value pairs to enable + configure.[/dim]")
            raise typer.Exit(0)

        try:
            enable_channel(name, fields)
        except KeyError as exc:
            console.print(f"[red]✗[/red] {exc}")
            raise typer.Exit(1)
        except ValidationError as exc:
            console.print(f"[red]✗ Validation failed:[/red]\n{exc}")
            raise typer.Exit(1)

        console.print(f"[green]✓[/green] {name} enabled")
        updated = [k for k in fields if k != "enabled"]
        if updated:
            console.print(f"  [dim]Updated: {', '.join(updated)}[/dim]")
        _warn_empty_credentials(name)
        console.print("  [dim]Restart gateway to apply.[/dim]")

    @channels_app.command("disable")
    def channels_disable_cmd(
        name: str = typer.Argument(..., help="Channel name to disable"),
    ):
        """Disable a channel. Credentials are preserved for later re-enable."""
        from raven.config.update_channels import disable_channel

        try:
            disable_channel(name)
        except KeyError as exc:
            console.print(f"[red]✗[/red] {exc}")
            raise typer.Exit(1)
        console.print(f"[green]✓[/green] {name} disabled (credentials kept)")
        console.print("  [dim]Restart gateway to apply.[/dim]")

    @channels_app.command(
        "set",
        context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    )
    def channels_set_cmd(
        ctx: typer.Context,
        name: str = typer.Argument(..., help="Channel name"),
    ):
        """Update specific fields. Same --flag syntax as ``enable``.

        Examples:
            raven channels set telegram --token NEW --proxy http://127.0.0.1:7890
            raven channels set slack --dm.policy open
        """
        if _help_requested(ctx.args):
            _print_schema_table(name)
            raise typer.Exit(0)

        from pydantic import ValidationError

        from raven.config.update_channels import set_channel_fields

        fields = _parse_channel_flags(ctx.args, name)
        if not fields:
            _print_schema_table(name)
            console.print("  [dim]Tip: re-run with one or more --flag value pairs to update.[/dim]")
            raise typer.Exit(0)
        try:
            prev = set_channel_fields(name, fields)
        except KeyError as exc:
            console.print(f"[red]✗[/red] {exc}")
            raise typer.Exit(1)
        except ValidationError as exc:
            console.print(f"[red]✗ Validation failed:[/red]\n{exc}")
            raise typer.Exit(1)
        console.print(f"[green]✓[/green] {name} updated: {', '.join(prev)}")
        console.print("  [dim]Restart gateway to apply.[/dim]")

    @channels_app.command("get")
    def channels_get_cmd(
        name: str = typer.Argument(..., help="Channel name"),
        show_secrets: bool = typer.Option(False, "--show-secrets", help="Show secret values in plaintext (dangerous)"),
    ):
        """Print current configuration for a channel. Secrets redacted by default."""
        from raven.config.update_channels import get_channel_config

        try:
            cfg = get_channel_config(name, redact_secrets=not show_secrets)
        except KeyError as exc:
            console.print(f"[red]✗[/red] {exc}")
            raise typer.Exit(1)

        table = Table(title=f"Channel: {name}")
        table.add_column("Flag", style="cyan", no_wrap=True)
        table.add_column("Value", overflow="fold")
        for k, v in cfg.items():
            flag = "--" + k.replace("_", "-")
            if v in ("", None, []):
                display = "[dim](empty)[/dim]"
            else:
                display = str(v)
            table.add_row(flag, display)
        console.print(table)

    @channels_app.command("reset")
    def channels_reset_cmd(
        name: str = typer.Argument(..., help="Channel name"),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    ):
        """Reset all fields of a channel to schema defaults. Key preserved.

        Prompts for confirmation by default; pass ``--yes`` for unattended runs.
        """
        from raven.config.update_channels import get_channel_config, reset_channel

        try:
            current = get_channel_config(name, redact_secrets=False)
        except KeyError as exc:
            console.print(f"[red]✗[/red] {exc}")
            raise typer.Exit(1)

        non_default = [k for k, v in current.items() if v not in (False, "", None, [], {})]

        if not yes:
            console.print(f"This will reset [cyan]{name}[/cyan] to schema defaults.")
            if non_default:
                preview = ", ".join(non_default[:5])
                more = f" (+{len(non_default) - 5} more)" if len(non_default) > 5 else ""
                console.print(f"  Currently non-default: [yellow]{preview}{more}[/yellow]")
            if not typer.confirm("Continue?", default=False):
                console.print("[yellow]Aborted.[/yellow]")
                raise typer.Exit(0)

        reset_channel(name)
        console.print(f"[green]✓[/green] {name} reset to defaults (key preserved, values cleared)")
        console.print("  [dim]Restart gateway to apply.[/dim]")

    @channels_app.command("show")
    def channels_show_cmd(
        name: str = typer.Argument(..., help="Channel name to describe"),
    ):
        """Show available ``--flag`` fields for a channel (reflection-driven).

        Output is in CLI form: e.g. ``--app-secret`` (kebab) or ``--dm.policy``
        (dotted for nested). Use these flags verbatim with ``channels enable``
        or ``channels set``.
        """
        _print_schema_table(name)

    @channels_app.command("list")
    def channels_list_cmd():
        """List every registered channel with its enabled state.

        Use ``channels show <name>`` to see configurable fields for a channel.
        """
        from raven.channels.registry import discover_channel_names
        from raven.config.loader import load_config

        config = load_config()
        names = sorted(discover_channel_names())
        table = Table(title="Available Channels")
        table.add_column("Channel", style="cyan", no_wrap=True)
        table.add_column("Display name", style="dim", overflow="fold")
        table.add_column("Enabled", no_wrap=True, justify="center")
        for n in names:
            section = getattr(config.channels, n, None)
            enabled = bool(section and getattr(section, "enabled", False))
            display = _display_name(n)
            table.add_row(
                n,
                display,
                "[green]✓[/green]" if enabled else "",
            )
        console.print(table)
        console.print()
        console.print(
            "[dim]Use the [cyan]Channel[/cyan] column name with "
            "'channels enable/show/set <name>'. "
            "Run 'channels show <name>' to see configurable fields.[/dim]"
        )


__all__ = ["channels_app"]


# ---------------------------------------------------------------------------
# channels_app: top-level Typer group for `raven channels ...`
# ---------------------------------------------------------------------------

channels_app = typer.Typer(help="Manage channels")


def _display_name(name: str) -> str:
    """Human display name for a channel: its declarative spec's display_name,
    else a titlecased fallback for a package without a spec."""
    from raven.channels.registry import discover_specs

    spec = discover_specs().get(name)
    return spec.display_name if spec else name.title()


@channels_app.command("status")
def channels_status():
    """Show channel status."""
    from raven.channels.registry import discover_channel_names
    from raven.config.loader import load_config

    config = load_config()

    table = Table(title="Channel Status")
    table.add_column("Channel", style="cyan")
    table.add_column("Enabled", style="green")

    for modname in sorted(discover_channel_names()):
        section = getattr(config.channels, modname, None)
        enabled = section and getattr(section, "enabled", False)
        display = _display_name(modname)
        table.add_row(
            display,
            "[green]✓[/green]" if enabled else "[dim]✗[/dim]",
        )

    console.print(table)


@channels_app.command("login")
def channels_login(
    channel_name: str = typer.Argument(..., help="Channel name (e.g. weixin, whatsapp)"),
    force: bool = typer.Option(False, "--force", "-f", help="Force re-authentication even if already logged in"),
):
    """Authenticate with a channel via QR code or other interactive login.

    Routes by the channel's declared ``interactive_login`` capability: channels
    without one are pointed at ``channels set`` instead of a no-op login.
    """
    import asyncio

    from raven.channels.registry import discover_specs
    from raven.config.loader import load_config

    specs = discover_specs()
    if channel_name not in specs:
        console.print(f"[red]Unknown channel: {channel_name}[/red]  Available: {', '.join(sorted(specs))}")
        raise typer.Exit(1)

    config = load_config()
    channel_cfg = getattr(config.channels, channel_name, None)
    if channel_cfg is None:
        console.print(f"[red]No config section for channel: {channel_name}[/red]")
        raise typer.Exit(1)

    spec = specs[channel_name]
    if not spec.capabilities.interactive_login:
        console.print(
            f"[yellow]{channel_name} needs no interactive login.[/yellow] "
            f"Configure it with: [cyan]raven channels set {channel_name}[/cyan]"
        )
        return
    console.print(f"{__logo__} {spec.display_name} Login\n")
    channel = spec.factory(channel_cfg)

    success = asyncio.run(channel.login(force=force))
    if not success:
        raise typer.Exit(1)


_register_config_commands(channels_app)
