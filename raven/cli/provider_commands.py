"""Provider subcommands — owns the ``provider_app`` Typer instance.

Lifecycle commands:

- ``provider login <name>`` — interactive OAuth login for OAuth-based
  providers (OpenAI Codex, GitHub Copilot, MiniMax Global/CN)

Config subcommands:

- ``provider list``                 — overview of every provider's status
- ``provider get <name>``           — current config (secrets redacted)
- ``provider set <name> [...]``     — patch fields (--api-key, --api-base, ...)
- ``provider test <name>``          — verify creds via free ``GET /v1/models``
- ``provider reset <name>``         — restore schema defaults; OAuth providers
                                      also lose their token file
- ``provider show <name>``          — reflect available ``--flag`` fields

Endpoint subcommands (``provider endpoint ...``) manage a plain API-key
provider's ``endpoints`` list -- several full key/base/header groups under one
section, for a vendor reachable by more than one account or region. OAuth
providers and Azure OpenAI / OpenAI Codex reject this at startup; only vendors
reached through the plain LiteLLM client accept it:

- ``provider endpoint add <name> --label X --api-key ... [--api-base ...]``
- ``provider endpoint remove <name> --label X``
- ``provider endpoint list <name>``

Architecture: write operations go ONLY through
:mod:`raven.config.update_providers`. Command bodies do not import
``load_config`` / ``save_config`` / provider Pydantic classes.

``commands.py`` imports :data:`provider_app` and registers it on the top-level
``app`` via ``app.add_typer(provider_app, name="provider")``.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from raven import __logo__

console = Console()

# Where GitHub's device-code response sends the user; LiteLLM prints it but does
# not open it.
_GITHUB_DEVICE_URL = "https://github.com/login/device"


provider_app = typer.Typer(help="Manage providers")


_LOGIN_HANDLERS: dict[str, callable] = {}


def _register_login(name: str):
    def decorator(fn):
        _LOGIN_HANDLERS[name] = fn
        return fn

    return decorator


@provider_app.command("login")
def provider_login(
    provider: str = typer.Argument(..., help="OAuth provider (e.g. 'openai-codex', 'minimax-global')"),
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

    # Here rather than in each handler: two of the three drivers that write these
    # files are LiteLLM's and create them under the process umask, and a family
    # added later would be the third place to forget. What a sign-in can leave
    # behind is already answered once, for disconnect.
    from raven.config.paths import restrict_to_owner
    from raven.config.update_providers import oauth_credential_files

    restrict_to_owner(*oauth_credential_files(spec.name))


def _open_device_page(url: str) -> None:
    """Hand the user a browser on the page their device code goes into.

    The drivers that own these flows print the URL and stop there, so this is the
    only reason the login commands differ from just calling them.
    """
    if not _can_open_browser():
        return

    import webbrowser

    if webbrowser.open(url):
        console.print(f"[dim]opened {url}[/dim]")
    else:
        console.print(f"visit {url} and enter the code below")


def _can_open_browser() -> bool:
    """Whether this session has a browser to hand the user off to.

    A headless Linux box has no display to open one on; every other platform is
    assumed to have one.
    """
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True


@_register_login("openai_codex")
def _login_openai_codex() -> None:
    # LiteLLM's driver owns this flow: it requests the device code, prints the
    # page and the code, polls, and writes the credential where the request path
    # will look for it. Asking it for a token is the whole login.
    from raven.providers.litellm_setup import import_litellm

    import_litellm()
    from litellm.llms.chatgpt.authenticator import Authenticator
    from litellm.llms.chatgpt.common_utils import CHATGPT_DEVICE_VERIFY_URL

    from raven.providers.chatgpt_token import access_token_and_account, clear_abandoned_device_code

    # Asked whether a credential is stored, this said yes for a revoked one -- and
    # the error that sends the user here is raised by the same revocation, so the
    # two answers pointed at each other. Ask whether one still works instead;
    # that call refreshes but cannot start a login, so a dead credential falls
    # through to the flow the user came for.
    try:
        access_token_and_account()
    except Exception:
        pass
    else:
        console.print("[green]Already signed in to OpenAI Codex.[/green]")
        console.print("[dim]To sign in as someone else: raven provider reset openai-codex[/dim]")
        return

    # Otherwise the driver would wait for the earlier attempt to land rather than
    # start this one, silently, for as long as five minutes.
    if clear_abandoned_device_code():
        console.print("[dim]Discarded an unfinished sign-in from an earlier attempt.[/dim]")

    console.print("[cyan]Starting ChatGPT device flow...[/cyan]\n")
    _open_device_page(CHATGPT_DEVICE_VERIFY_URL)

    try:
        token = Authenticator().get_access_token()
    except Exception as exc:
        console.print(f"[red]Authentication error: {exc}[/red]")
        raise typer.Exit(1)

    if not token:
        console.print("[red]✗ Authentication failed[/red]")
        raise typer.Exit(1)

    console.print("[green]✓ Authenticated with OpenAI Codex[/green]")


@_register_login("github_copilot")
def _login_github_copilot() -> None:
    import asyncio

    console.print("[cyan]Starting GitHub Copilot device flow...[/cyan]\n")

    # The page is the same URL GitHub's device-code response returns, and the code
    # has to be typed into it either way -- opening it before the code appears
    # costs the user nothing.
    _open_device_page(_GITHUB_DEVICE_URL)

    async def _trigger():
        from raven.providers.litellm_setup import import_litellm

        litellm = import_litellm()
        await litellm.acompletion(
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


def _login_minimax(region: str, label: str) -> None:
    from raven.providers.minimax_oauth import login

    console.print("[cyan]Starting MiniMax device flow...[/cyan]\n")
    try:
        token = login(
            region,
            print_fn=lambda message: console.print(message),
            open_browser=_can_open_browser(),
        )
    except Exception as exc:
        console.print(f"[red]Authentication error: {exc}[/red]")
        raise typer.Exit(1)
    if not token.access:
        console.print("[red]Authentication failed[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Authenticated with {label}[/green]")


@_register_login("minimax_global")
def _login_minimax_global() -> None:
    _login_minimax("global", "MiniMax Global")


@_register_login("minimax_cn")
def _login_minimax_cn() -> None:
    _login_minimax("cn", "MiniMax CN")


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
    - ``--<flag> true``     -> ``{"<flag>": "true"}``     (string; the schema coerces)
    - ``--no-<flag>``       -> ``{"<flag>": False}``      (bool negative)
    - ``--<flag>`` alone    -> ``{"<flag>": True}``       (bool positive)

    Values come back as written; only the two valueless forms produce a bool
    here, and the schema coerces the rest on validation. The bool forms are
    named generically because no provider declares a bool field today -- the one
    that did (Gemini's ``vertex``) described a mechanism that never existed and
    was removed. They stay, matching ``_parse_channel_flags``, so a provider
    gaining one needs no parser change.

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


def _load_section(name: str) -> dict | None:
    """This provider's stored fields, or None when it has no section yet."""
    from raven.config.update_providers import get_provider_config

    try:
        return get_provider_config(name, redact_secrets=False)
    except KeyError:
        return None


def _ineffective_because(provider: str, model: str) -> list[str]:
    """Reasons this model will not actually be used, despite being written.

    A stale ``agents.defaults.provider`` used to be the other one, and the note
    for it told the user to edit a field no command wrote. Both surfaces that
    change a model now write it by the same rule, so the note would be advice
    about a state neither of them produces.
    """
    from raven.config.loader import load_config

    try:
        config = load_config()
    except Exception:
        return []

    notes: list[str] = []
    section = config.providers.get(provider)
    deployment = getattr(section, "deployment", "") if section else ""
    if deployment:
        notes.append(
            f"providers.{provider}.deployment is set to {deployment!r}, which decides the "
            f"deployment regardless of the model id."
        )
    return notes


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
            raven provider set gemini --api-key-list k1,k2
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
        if result["status"] == "no_probe_endpoint":
            # Not a failure: this probe pings `/models`, and these vendors do not
            # publish one at an address we hold. Saying "failed" here told seven
            # correctly configured providers they were broken.
            console.print(f"[yellow]?[/yellow] {name} not probed: {result['error']}")
            console.print("  [dim]Credentials are set; run a turn to exercise them.[/dim]")
            return

        hint = hints.get(result["status"], "")
        console.print(f"[red]✗[/red] {name} failed: {result['status']}")
        if hint:
            console.print(f"  [dim]{hint}[/dim]")
        if result.get("error"):
            console.print(f"  [dim]Detail: {result['error']}[/dim]")
        raise typer.Exit(1)

    @app.command("use")
    def provider_use_cmd(
        model: str = typer.Argument(..., help="Model id, e.g. claude-sonnet-5"),
        provider: str = typer.Option("", "--provider", "-p", help="Required: the provider whose credential serves it"),
    ):
        """Make this the model the agent runs on. Requires --provider.

        The pair, not the id alone: `openrouter` serving
        `anthropic/claude-haiku-4-5` and `anthropic` serving
        `claude-haiku-4-5` are both real and bill different accounts, and an id
        cannot tell them apart. `raven provider list` shows the configured ones.

        Changing it used to mean re-running the whole wizard: the TUI picker and
        onboarding could both switch models and the CLI could not, so a user on a
        headless box had six setup steps to walk to change one field.

        The id is stored the way every other surface stores it -- naming its
        provider -- so the three cannot disagree about what was chosen.
        """
        from raven.config.update import set_default_model
        from raven.providers.auth import credential_status
        from raven.providers.catalog import describe
        from raven.providers.wire import stored_model_id

        # Named, not derived. An id does not say whose credential serves it --
        # `openrouter` serving `anthropic/claude-haiku-4-5` and `anthropic`
        # serving `claude-haiku-4-5` are both real and bill different accounts,
        # and a prefix is LiteLLM routing syntax rather than evidence about a
        # key. The same rule the TUI's `/model` enforces.
        if not provider:
            console.print(f"[red]✗[/red] --provider is required: {model!r} does not name whose credential serves it.")
            console.print(f"  [dim]raven provider use {model} --provider <name>[/dim]")
            console.print("  [dim]raven provider list  # the ones you have configured[/dim]")
            raise typer.Exit(1)

        name = provider

        stored = stored_model_id(name, model) if name else model
        previous = set_default_model(stored, provider=name)

        row = describe(name, stored)
        label = f"{row.label} ([dim]{stored}[/dim])" if row.described else stored
        console.print(f"[green]✓[/green] default model: {label}")
        if previous and previous != stored:
            console.print(f"  [dim]was {previous}[/dim]")

        # Reported rather than refused: choosing a model before configuring its
        # provider is a normal order to do things in, and the startup gate says
        # the same thing again if it is still missing then.
        status = credential_status(name, _load_section(name), include_external=True)
        if not status.ok:
            console.print(f"  [yellow]![/yellow] {status.summary}")

        # An Azure deployment can still make this write have no effect, and it
        # fails silently otherwise: the command reports success, the file
        # changes, and requests keep going where they went before. The stale-pin
        # case used to be the other one; this command now writes the pin itself.
        for note in _ineffective_because(name, stored):
            console.print(f"  [yellow]![/yellow] {note}")

    @app.command("reset")
    def provider_reset_cmd(
        name: str = typer.Argument(..., help="Provider name"),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    ):
        """Restore a provider to schema defaults. Key preserved, values reset.

        For OAuth providers the credential files under ``~/.raven/oauth`` are
        deleted too, so the user is effectively logged out and must re-run
        ``provider login`` to use it.
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

        from raven.config.update_providers import serves_default_model
        from raven.providers.registry import (
            CRED_ENDPOINT,
            CRED_LOCAL,
            CRED_OAUTH,
            credential_kind,
        )

        # Asked before the confirmation, because it is the part worth confirming:
        # the model id survives the reset and still names this provider, so the
        # next command finds a default nothing can answer.
        serves_default = serves_default_model(name)

        if not yes:
            console.print(f"This will reset [cyan]{name}[/cyan] to schema defaults.")
            if non_default:
                preview = ", ".join(non_default[:5])
                more = f" (+{len(non_default) - 5} more)" if len(non_default) > 5 else ""
                console.print(f"  Currently non-default: [yellow]{preview}{more}[/yellow]")
            if serves_default:
                console.print("  [yellow]This provider serves your current default model[/yellow] -- pick another")
                console.print("  [dim]afterwards with /model in the TUI, or sign in to it again.[/dim]")
            if not typer.confirm("Continue?", default=False):
                console.print("[yellow]Aborted.[/yellow]")
                raise typer.Exit(0)

        reset_provider(name)
        console.print(f"[green]✓[/green] {name} reset to defaults (key preserved, values cleared)")
        if serves_default:
            # By credential kind, because each kind is set up by a different
            # command and a different field: `provider login` exits 1 for anyone
            # who is not an OAuth family, and a local deployment has no key to
            # give -- naming the wrong one sends the user to a command that
            # refuses them or a flag that does nothing.
            dashed = name.replace("_", "-")
            kind = credential_kind(name)
            if kind == CRED_OAUTH:
                back = f"raven provider login {dashed}"
            elif kind == CRED_LOCAL:
                back = f"raven provider set {dashed} --api-base <URL>"
            elif kind == CRED_ENDPOINT:
                back = f"raven provider set {dashed} --api-key <KEY> --api-base <URL>"
            else:
                back = f"raven provider set {dashed} --api-key <KEY>"
            console.print("  [yellow]Your default model is served by this provider and no longer works.[/yellow]")
            console.print(f"  [dim]Pick another with /model in the TUI, or set it up again: {back}[/dim]")

    @app.command("show")
    def provider_show_cmd(
        name: str = typer.Argument(..., help="Provider name to describe"),
    ):
        """Show available ``--flag`` fields for a provider (reflection-driven)."""
        _print_schema_table(name)


_register_config_commands(provider_app)


endpoint_app = typer.Typer(
    help=(
        "Manage a provider's endpoints -- several full key/base/header groups "
        "under one section, for a vendor reachable by more than one account or "
        "region. Only plain API-key providers accept this: a provider using "
        "OAuth, or Azure OpenAI / OpenAI Codex, is rejected at startup if it has "
        "any configured."
    )
)


def _parse_extra_headers(value: str) -> dict[str, str] | None:
    """Parse ``--extra-headers`` JSON into a dict, or None when unset."""
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        raise typer.BadParameter('--extra-headers must be a JSON object, e.g. \'{"X-Foo": "bar"}\'')
    if not isinstance(parsed, dict):
        raise typer.BadParameter("--extra-headers must be a JSON object")
    return parsed


@endpoint_app.command("add")
def endpoint_add_cmd(
    name: str = typer.Argument(..., help="Provider name (e.g. openrouter)"),
    label: str = typer.Option(..., "--label", help="Idempotency key: an existing label is replaced, not merged"),
    api_key: str = typer.Option(
        "", "--api-key", help="API key for this endpoint (omit only for a local, keyless deployment)"
    ),
    api_base: str = typer.Option("", "--api-base", help="Base URL for this endpoint"),
    extra_headers: str = typer.Option("", "--extra-headers", help='Extra headers as JSON, e.g. {"X-Foo": "bar"}'),
):
    """Add or replace one endpoint on a provider, keyed by ``--label``.

    Only meaningful for plain API-key providers reached through the LiteLLM
    client -- a provider using OAuth, or Azure OpenAI / OpenAI Codex, refuses
    to start with any endpoints configured.
    """
    from pydantic import ValidationError

    from raven.config.update_providers import add_provider_endpoint

    headers = _parse_extra_headers(extra_headers)
    try:
        endpoints = add_provider_endpoint(
            name,
            label=label,
            api_key=api_key,
            api_base=api_base or None,
            extra_headers=headers,
        )
    except (KeyError, RuntimeError) as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(1)
    except ValidationError as exc:
        console.print(f"[red]✗ Validation failed:[/red]\n{exc}")
        raise typer.Exit(1)

    console.print(f"[green]✓[/green] {name} endpoint {label!r} saved ({len(endpoints)} total)")


@endpoint_app.command("remove")
def endpoint_remove_cmd(
    name: str = typer.Argument(..., help="Provider name"),
    label: str = typer.Option(..., "--label", help="Label of the endpoint to remove"),
):
    """Remove one endpoint by ``--label`` (no-op if the label is not present)."""
    from pydantic import ValidationError

    from raven.config.update_providers import remove_provider_endpoint

    try:
        endpoints = remove_provider_endpoint(name, label)
    except KeyError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(1)
    except ValidationError as exc:
        console.print(f"[red]✗ Validation failed:[/red]\n{exc}")
        raise typer.Exit(1)

    console.print(f"[green]✓[/green] {name} endpoint {label!r} removed ({len(endpoints)} remaining)")


@endpoint_app.command("list")
def endpoint_list_cmd(
    name: str = typer.Argument(..., help="Provider name"),
):
    """List a provider's endpoints. API keys redacted."""
    from pydantic import ValidationError

    from raven.config.update_providers import list_provider_endpoints

    try:
        endpoints = list_provider_endpoints(name)
    except KeyError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(1)
    except ValidationError as exc:
        console.print(f"[red]✗ Validation failed:[/red]\n{exc}")
        raise typer.Exit(1)

    table = Table(title=f"Provider endpoints: {name}")
    table.add_column("Label", style="cyan", no_wrap=True)
    table.add_column("API Key")
    table.add_column("API Base", overflow="fold")
    table.add_column("Extra Headers", overflow="fold")
    for ep in endpoints:
        table.add_row(
            ep["label"],
            ep["api_key"],
            ep["api_base"] or "",
            str(ep["extra_headers"]) if ep["extra_headers"] else "",
        )
    console.print(table)


provider_app.add_typer(endpoint_app, name="endpoint")


__all__ = ["provider_app"]
