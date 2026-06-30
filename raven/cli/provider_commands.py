"""Provider subcommands — owns the ``provider_app`` Typer instance.

Lifecycle commands:

- ``provider login <name>`` — interactive OAuth login for OAuth-based
  providers (openai-codex, github-copilot)

Config subcommands:

- ``provider list``                 — overview of every provider's status
- ``provider get <name>``           — current config (secrets redacted)
- ``provider set <name> [...]``     — patch fields (--api-key, --api-base, ...)
- ``provider test <name>``          — verify creds via free ``GET /v1/models``
- ``provider reset <name>``         — restore schema defaults; OAuth providers
                                      also lose their token file
- ``provider show <name>``          — reflect available ``--flag`` fields

Architecture: write operations go ONLY through
:mod:`raven.config.update_providers`. Command bodies do not import
``load_config`` / ``save_config`` / provider Pydantic classes.

``commands.py`` imports :data:`provider_app` and registers it on the top-level
``app`` via ``app.add_typer(provider_app, name="provider")``.
"""

from __future__ import annotations

from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from raven import __logo__

console = Console()


provider_app = typer.Typer(help="Manage providers")


_LOGIN_HANDLERS: dict[str, callable] = {}


def _register_login(name: str):
    def decorator(fn):
        _LOGIN_HANDLERS[name] = fn
        return fn

    return decorator


@provider_app.command("login")
def provider_login(
    provider: str = typer.Argument(..., help="OAuth provider (e.g. 'openai-codex', 'github-copilot')"),
):
    """Authenticate with an OAuth provider."""
    from raven.providers.registry import PROVIDERS

    key = provider.replace("-", "_")
    spec = next((s for s in PROVIDERS if s.name == key and s.is_oauth), None)
    if not spec:
        names = ", ".join(s.name.replace("_", "-") for s in PROVIDERS if s.is_oauth)
        console.print(f"[red]Unknown OAuth provider: {provider}[/red]  Supported: {names}")
        raise typer.Exit(1)

    handler = _LOGIN_HANDLERS.get(spec.name)
    if not handler:
        console.print(f"[red]Login not implemented for {spec.label}[/red]")
        raise typer.Exit(1)

    console.print(f"{__logo__} OAuth Login - {spec.label}\n")
    handler()


@_register_login("openai_codex")
def _login_openai_codex() -> None:
    try:
        from oauth_cli_kit import get_token, login_oauth_interactive

        token = None
        try:
            token = get_token()
        except Exception:
            pass
        if not (token and token.access):
            console.print("[cyan]Starting interactive OAuth login...[/cyan]\n")
            token = login_oauth_interactive(
                print_fn=lambda s: console.print(s),
                prompt_fn=lambda s: typer.prompt(s),
            )
        if not (token and token.access):
            console.print("[red]✗ Authentication failed[/red]")
            raise typer.Exit(1)
        console.print(f"[green]✓ Authenticated with OpenAI Codex[/green]  [dim]{token.account_id}[/dim]")
    except ImportError:
        console.print("[red]oauth_cli_kit not installed. Run: pip install oauth-cli-kit[/red]")
        raise typer.Exit(1)


@_register_login("github_copilot")
def _login_github_copilot() -> None:
    import asyncio

    console.print("[cyan]Starting GitHub Copilot device flow...[/cyan]\n")

    async def _trigger():
        from litellm import acompletion

        await acompletion(
            model="github_copilot/gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1,
        )

    try:
        asyncio.run(_trigger())
        console.print("[green]✓ Authenticated with GitHub Copilot[/green]")
    except Exception as e:
        console.print(f"[red]Authentication error: {e}[/red]")
        raise typer.Exit(1)


def _help_requested(extra_args: list[str]) -> bool:
    """Detect ``--help`` / ``-h`` inside a free-form ``ctx.args`` list."""
    return any(t in ("--help", "-h") or t.startswith("--help=") for t in extra_args)


def _print_schema_table(name: str) -> None:
    """Render a provider's field-spec table.

    Shared by ``show``, ``set --help`` interception, and the empty-flag
    fallback in ``set``.
    """
    from raven.config.update_providers import provider_field_specs

    try:
        specs = provider_field_specs(name)
    except KeyError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(1)

    table = Table(title=f"Provider: {name}")
    table.add_column("Flag", style="cyan", no_wrap=True)
    table.add_column("Type", overflow="fold")
    table.add_column("Default", no_wrap=True)
    table.add_column("Secret?", no_wrap=True, justify="center")
    table.add_column("Description", overflow="fold")
    for path, spec in specs.items():
        flag = "--" + path.replace("_", "-")
        default = spec["default"]
        default_str = "" if default in (None, "", [], {}) else str(default)
        table.add_row(
            flag,
            spec["type"],
            default_str,
            "✓" if spec["is_secret"] else "",
            spec.get("description", "") or "",
        )
    console.print(table)


def _parse_provider_flags(extra_args: list[str], provider_name: str) -> dict[str, Any]:
    """Parse arbitrary ``--flag value`` pairs against a provider's Pydantic schema.

    Mirrors ``_parse_channel_flags`` (raven/cli/channel_commands.py:109) — the
    same six forms supported there work here:

    - ``--api-key abc``     -> ``{"api_key": "abc"}``
    - ``--api-key=abc``     -> ``{"api_key": "abc"}``
    - ``--api-base X``      -> ``{"api_base": "X"}``     (kebab -> snake)
    - ``--vertex true``     -> ``{"vertex": True}``       (bool string coerced)
    - ``--no-vertex``       -> ``{"vertex": False}``      (bool negative)
    - ``--vertex`` alone    -> ``{"vertex": True}``       (bool positive)

    Unknown fields raise ``typer.BadParameter`` pointing at ``provider show``.
    """
    from raven.config.update_providers import provider_field_specs

    try:
        specs = provider_field_specs(provider_name)
    except KeyError as exc:
        raise typer.BadParameter(str(exc))

    def _normalize(flag: str) -> str:
        return ".".join(seg.replace("-", "_") for seg in flag.split("."))

    out: dict[str, Any] = {}
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
                    f"Unknown field '--no-{flag[3:]}'. Run 'raven provider show {provider_name}' for available flags."
                )
            out[key] = False
            continue

        key = _normalize(flag)
        if key not in specs:
            raise typer.BadParameter(
                f"Unknown field '--{flag}' for provider '{provider_name}'. "
                f"Run 'raven provider show {provider_name}' for available flags."
            )

        if value is None:
            if specs[key]["type"] == "bool":
                out[key] = True
            else:
                raise typer.BadParameter(f"Missing value for --{flag}")
        else:
            out[key] = value

    return out


def _register_config_commands(app: typer.Typer) -> None:
    """Attach config subcommands to ``provider_app``."""
    app.info.no_args_is_help = True

    @app.command("list")
    def provider_list_cmd():
        """Show status of every LLM provider declared on ``ProvidersConfig``."""
        from raven.config.update_providers import list_providers

        table = Table(title="LLM Providers")
        table.add_column("Name", style="cyan", no_wrap=True)
        table.add_column("Display", style="dim", overflow="fold")
        table.add_column("Type", no_wrap=True)
        table.add_column("Status", no_wrap=True)
        table.add_column("API Base", overflow="fold")
        for p in list_providers():
            if p["is_oauth"]:
                type_str = "OAuth"
            elif p["is_local"]:
                type_str = "Local"
            elif p["is_gateway"]:
                type_str = "Gateway"
            else:
                type_str = "API Key"
            status = "[green]✓ configured[/green]" if p["configured"] else "[dim]not set[/dim]"
            table.add_row(
                p["name"],
                p["display_name"],
                type_str,
                status,
                p.get("api_base") or "",
            )
        console.print(table)
        console.print()
        console.print(
            "[dim]Use the [cyan]Name[/cyan] column with "
            "'provider show/set/get <name>'. "
            "Run 'provider show <name>' to see configurable fields.[/dim]"
        )

    @app.command("get")
    def provider_get_cmd(
        name: str = typer.Argument(..., help="Provider name (e.g. openrouter)"),
        show_secrets: bool = typer.Option(False, "--show-secrets", help="Show secret values in plaintext (dangerous)"),
    ):
        """Print current configuration for a provider. Secrets redacted by default."""
        from raven.config.update_providers import get_provider_config

        try:
            cfg = get_provider_config(name, redact_secrets=not show_secrets)
        except KeyError as exc:
            console.print(f"[red]✗[/red] {exc}")
            raise typer.Exit(1)

        table = Table(title=f"Provider: {name}")
        table.add_column("Flag", style="cyan", no_wrap=True)
        table.add_column("Value", overflow="fold")
        for k, v in cfg.items():
            flag = "--" + k.replace("_", "-")
            if v in ("", None, [], {}):
                display = "[dim](empty)[/dim]"
            else:
                display = str(v)
            table.add_row(flag, display)
        console.print(table)

    @app.command(
        "set",
        context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    )
    def provider_set_cmd(
        ctx: typer.Context,
        name: str = typer.Argument(..., help="Provider name"),
    ):
        """Patch provider fields. ``--flag value`` syntax matches ``channels set``.

        Examples:

            raven provider set openrouter --api-key sk-or-v1-...
            raven provider set azure-openai --api-key X --api-base https://...
            raven provider set gemini --api-key K --vertex true
        """
        if _help_requested(ctx.args):
            _print_schema_table(name)
            raise typer.Exit(0)

        from pydantic import ValidationError

        from raven.config.update_providers import set_provider_fields

        fields = _parse_provider_flags(ctx.args, name)
        if not fields:
            _print_schema_table(name)
            console.print("  [dim]Tip: re-run with one or more --flag value pairs to update.[/dim]")
            raise typer.Exit(0)

        try:
            prev = set_provider_fields(name, fields)
        except KeyError as exc:
            console.print(f"[red]✗[/red] {exc}")
            raise typer.Exit(1)
        except RuntimeError as exc:
            console.print(f"[red]✗[/red] {exc}")
            raise typer.Exit(1)
        except ValidationError as exc:
            console.print(f"[red]✗ Validation failed:[/red]\n{exc}")
            raise typer.Exit(1)

        console.print(f"[green]✓[/green] {name} updated: {', '.join(prev)}")
        console.print(f"  [dim]Run 'raven provider test {name}' to verify the credentials.[/dim]")

    @app.command("test")
    def provider_test_cmd(
        name: str = typer.Argument(..., help="Provider name"),
        timeout: int = typer.Option(10, "--timeout", "-t", help="Timeout seconds"),
    ):
        """Verify a provider's credentials via a free ``GET /v1/models`` call.

        Does NOT consume inference quota — hits the provider's models metadata
        endpoint, which is free, fast, and tells you whether the key is valid,
        has credit, and isn't rate-limited.
        """
        from raven.config.update_providers import test_provider as probe

        console.print(f"[dim]Pinging {name}/v1/models ...[/dim]")
        try:
            result = probe(name, timeout_s=timeout)
        except KeyError as exc:
            console.print(f"[red]✗[/red] {exc}")
            raise typer.Exit(1)

        if result["ok"]:
            console.print(
                f"[green]✓[/green] {name} OK "
                f"([dim]{result['models_count']} models available, "
                f"responded in {result['elapsed_ms']}ms[/dim])"
            )
            return

        hints = {
            "not_configured": f"Run: raven provider set {name} --api-key <KEY>",
            "invalid_key": f"Run: raven provider set {name} --api-key <NEW-KEY>",
            "no_credits": "Fund your account at the provider's billing page",
            "rate_limited": "Wait a few minutes and retry, or switch provider",
            "oauth_token_missing": (f"Run: raven provider login {name.replace('_', '-')}"),
            "network_error": "Check network / firewall / VPN settings",
        }
        hint = hints.get(result["status"], "")
        console.print(f"[red]✗[/red] {name} failed: {result['status']}")
        if hint:
            console.print(f"  [dim]{hint}[/dim]")
        if result.get("error"):
            console.print(f"  [dim]Detail: {result['error']}[/dim]")
        raise typer.Exit(1)

    @app.command("reset")
    def provider_reset_cmd(
        name: str = typer.Argument(..., help="Provider name"),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    ):
        """Restore a provider to schema defaults. Key preserved, values reset.

        For OAuth providers (openai-codex, github-copilot) the on-disk token
        file written by ``oauth_cli_kit`` is also deleted, so the user is
        effectively logged out and must re-run ``provider login`` to use it.
        """
        from raven.config.update_providers import (
            get_provider_config,
            reset_provider,
        )

        try:
            current = get_provider_config(name, redact_secrets=False)
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

        reset_provider(name)
        console.print(f"[green]✓[/green] {name} reset to defaults (key preserved, values cleared)")

    @app.command("show")
    def provider_show_cmd(
        name: str = typer.Argument(..., help="Provider name to describe"),
    ):
        """Show available ``--flag`` fields for a provider (reflection-driven)."""
        _print_schema_table(name)


_register_config_commands(provider_app)


__all__ = ["provider_app"]
