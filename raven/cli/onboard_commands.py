"""Four-step onboarding wizard: LLM provider → sandbox → channel → memory.

Goal: get a new user from ``pip install`` to a working agent in a few
minutes, without ever opening ``~/.raven/config.json`` or
``~/.everos/config.toml``.

Steps (mirrors ``my_docs/temp/onboard-flow.mermaid``):
  0. Welcome
  1. LLM provider (required; multi-provider, in-step connectivity + test probe)
  2. Sandbox / run location (optional, single-select)
  3. Chat channel (optional, stackable)
  4. EverOS long-term memory (optional; llm/embedding required once enabled,
     rerank/multimodal optional)
  5. Done

All writes go through the ``update_providers`` / ``update_channels`` /
``update`` / ``update_everos`` ops libraries — this module owns the UX layer,
not config-schema knowledge.

Navigation: questionary 2.1.1 has no first-class cross-screen "back", so the
wizard is a screen state machine and back is expressed as a ``0) back``
sentinel choice. Ctrl+C exits at any point, keeping whatever was already
written.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, Optional

import typer
from rich.console import Console
from rich.panel import Panel

from raven.cli._helpers import (
    DEFAULT_PROBE_MESSAGE,
    print_probe_troubleshooting,
    send_probe,
)

console = Console()

_TOTAL_STEPS = 4

# Sentinel returned by a screen function to ask the runner to go back one
# screen; ``None`` from a picker means Ctrl+C (exit).
_BACK = object()


# ---------------------------------------------------------------------------
# Curated provider catalogue surfaced in Step 1's picker.
# ---------------------------------------------------------------------------


_CURATED_PROVIDERS: list[dict[str, Any]] = [
    {"name": "openrouter", "label": "OpenRouter (recommended, multi-provider)", "is_oauth": False},
    {"name": "openai", "label": "OpenAI", "is_oauth": False},
    {"name": "anthropic", "label": "Anthropic", "is_oauth": False},
    {"name": "gemini", "label": "Gemini", "is_oauth": False},
    {"name": "deepseek", "label": "DeepSeek", "is_oauth": False},
    {"name": "github_copilot", "label": "GitHub Copilot (OAuth)", "is_oauth": True},
    {"name": "openai_codex", "label": "Codex (OAuth)", "is_oauth": True},
    {"name": "custom", "label": "Other (OpenAI-compatible endpoint)", "is_oauth": False},
]

_QUESTIONARY_INSTALL_HINT = (
    "[red]Missing dependency:[/red] [cyan]questionary[/cyan] is required for "
    "interactive onboarding.\n"
    "Install it with: [cyan]uv add 'questionary>=2.0,<3.0'[/cyan]\n"
    "Or re-run with [cyan]--non-interactive[/cyan] plus the relevant flags."
)


def _require_questionary() -> Any:
    """Lazy-import :mod:`questionary` so missing-package errors stay scoped here."""
    try:
        import questionary
    except ModuleNotFoundError:
        console.print(_QUESTIONARY_INSTALL_HINT)
        raise typer.Exit(1)
    return questionary


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _step_header(n: int, title: str) -> None:
    console.print()
    console.print(Panel(f"[bold]Step {n}/{_TOTAL_STEPS}[/bold] · {title}", border_style="cyan"))
    console.print(
        "[dim]↑↓ select · Enter confirm · 0 back · Ctrl+C quit[/dim]"
    )


def _check_tty_or_die(non_interactive: bool) -> None:
    """Bail when stdout isn't a TTY and the user didn't opt into headless mode."""
    if non_interactive:
        return
    if not sys.stdout.isatty():
        console.print(
            "[red]Non-interactive terminal detected.[/red]\n"
            "Re-run with: "
            "[cyan]raven onboard --non-interactive --provider <name> --api-key <key>[/cyan]"
        )
        raise typer.Exit(2)


def _load_raw_config() -> dict[str, Any]:
    """Return the parsed on-disk config, or ``{}`` if absent/unreadable."""
    from raven.config.loader import get_config_path

    path = get_config_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text()) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def _configured_providers() -> list[str]:
    """Names of providers that currently have an api_key set on disk."""
    data = _load_raw_config()
    providers = data.get("providers") or {}
    return [
        name
        for name, p in providers.items()
        if isinstance(p, dict) and p.get("apiKey")
    ]


def _is_config_populated() -> bool:
    """True iff at least one provider has a key AND a default model is set.

    "Populated" for the startup gate means the required step (Step 1) is
    satisfied: a provider key plus ``agents.defaults.model``. Either alone is
    not enough to talk to a model.
    """
    data = _load_raw_config()
    providers = data.get("providers") or {}
    has_provider = any(
        isinstance(p, dict) and p.get("apiKey") for p in providers.values()
    )
    model = (data.get("agents", {}) or {}).get("defaults", {}).get("model")
    return bool(has_provider and model)


def _handle_existing_config(*, reset: bool, yes: bool, non_interactive: bool) -> None:
    """Ask the user what to do when a populated config already exists."""
    if reset:
        return
    if not _is_config_populated():
        return

    if non_interactive:
        if yes:
            console.print(
                "[dim]Existing config detected; --yes set, proceeding with overwrite.[/dim]"
            )
            return
        console.print(
            "[red]Existing config detected.[/red] Pass [cyan]--reset[/cyan] (or "
            "[cyan]--yes[/cyan]) to overwrite, or edit in place with "
            "[cyan]raven provider set[/cyan] / [cyan]raven channels enable[/cyan]."
        )
        raise typer.Exit(2)

    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    action = questionary.select(
        "An Raven config already exists. What would you like to do?",
        choices=[
            questionary.Choice("Skip onboarding — keep existing config", value="skip"),
            questionary.Choice("Re-run the wizard — overwrite", value="redo"),
            questionary.Choice("Quit", value="quit"),
        ],
        style=RAVEN_STYLE,
    ).ask()
    if action in (None, "quit"):
        raise typer.Exit(1)
    if action == "skip":
        console.print(
            "[green]✓[/green] Keeping existing config. Run "
            "[cyan]raven provider list[/cyan] to review."
        )
        raise typer.Exit(0)


def _bootstrap_empty_config() -> None:
    """Make sure ``~/.raven/config.json`` + workspace dir exist before we patch.

    On a fresh config we pin ``memory.backend = None`` (native Markdown).
    ``MemoryConfig.backend`` defaults to ``"everos"`` in the schema, so an
    absent ``memory`` section would activate EverOS at runtime with no
    llm/embedding configured. Pinning it disabled makes the safe baseline
    survive a Ctrl+C mid-wizard; Step 4 flips it to ``everos`` only once the
    required models are written.
    """
    from raven.config.loader import get_config_path, load_config, save_config
    from raven.config.paths import get_workspace_path
    from raven.utils.helpers import sync_workspace_templates

    path = get_config_path()
    if not path.exists():
        save_config(load_config())  # writes default Config() to disk
        _set_memory_backend(None)
    workspace = get_workspace_path()
    workspace.mkdir(parents=True, exist_ok=True)
    sync_workspace_templates(workspace)


# ---------------------------------------------------------------------------
# Step 1 — provider primitives (reused verbatim from the 3-step wizard)
# ---------------------------------------------------------------------------


def _provider_label(name: str) -> str:
    """Display label for a provider, falling back to the registry's display_name."""
    for entry in _CURATED_PROVIDERS:
        if entry["name"] == name:
            return entry["label"]
    try:
        from raven.providers.registry import find_by_name

        spec = find_by_name(name)
        return spec.label if spec else name
    except Exception:
        return name


def _validate_provider_name(name: str) -> str:
    """Resolve a user-supplied provider name (kebab or snake) to a registry key."""
    from raven.config.update_providers import provider_field_specs

    candidate = name.replace("-", "_")
    try:
        provider_field_specs(candidate)
    except KeyError as exc:
        raise typer.BadParameter(str(exc))
    return candidate


def _select_provider() -> Optional[str]:
    """Interactive provider picker built from the curated catalogue.

    Returns the provider name, ``_BACK`` if the user chose the back sentinel,
    or ``None`` on Ctrl+C.
    """
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    choices: list[Any] = []
    last_was_oauth_boundary = False
    for entry in _CURATED_PROVIDERS:
        if entry["is_oauth"] and not last_was_oauth_boundary:
            choices.append(questionary.Separator())
            last_was_oauth_boundary = True
        if not entry["is_oauth"] and last_was_oauth_boundary:
            choices.append(questionary.Separator())
            last_was_oauth_boundary = False
        choices.append(questionary.Choice(entry["label"], value=entry["name"]))
    choices.append(questionary.Separator())
    choices.append(questionary.Choice("0) Back", value=_BACK))

    picked = questionary.select(
        "Provider:",
        choices=choices,
        style=RAVEN_STYLE,
    ).ask()
    return picked  # None on Ctrl+C


def _prompt_api_key(provider: str) -> str:
    """Ask for an API key (hidden input)."""
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    key = questionary.password(
        f"API key for {provider}:",
        validate=lambda v: True if len(v) >= 8 else "Key looks too short (≥ 8 chars).",
        style=RAVEN_STYLE,
    ).ask()
    if not key:
        raise typer.Exit(1)
    return key


def _prompt_base_url(default: str = "https://") -> str:
    """Ask for an OpenAI-compatible base URL (used by the 'custom' provider)."""
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    url = questionary.text(
        "Base URL (must include /v1):",
        default=default,
        validate=lambda v: True if v.startswith(("http://", "https://")) else "URL must start with http:// or https://",
        style=RAVEN_STYLE,
    ).ask()
    if not url:
        raise typer.Exit(1)
    return url


def _prompt_custom_model() -> str:
    """Ask for the model name when using a custom OpenAI-compatible endpoint."""
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    model = questionary.text(
        "Default model id (e.g. 'gpt-3.5-turbo' or 'qwen-max'):",
        validate=lambda v: True if v.strip() else "Model id is required for custom endpoints.",
        style=RAVEN_STYLE,
    ).ask()
    if not model:
        raise typer.Exit(1)
    return model.strip()


def _run_oauth_login(provider: str) -> None:
    """Dispatch the OAuth login handler registered by ``provider_commands``."""
    from raven.cli.provider_commands import _LOGIN_HANDLERS
    from raven.providers.registry import find_by_name

    spec = find_by_name(provider)
    if not spec or not spec.is_oauth:
        console.print(f"[red]✗ {provider} is not an OAuth provider.[/red]")
        raise typer.Exit(1)
    handler = _LOGIN_HANDLERS.get(spec.name)
    if not handler:
        console.print(f"[red]✗ No login handler registered for {provider}.[/red]")
        raise typer.Exit(1)
    console.print(f"[cyan]Starting OAuth login for {spec.label}...[/cyan]\n")
    handler()


def _verify_provider(provider: str) -> tuple[bool, str, Optional[list[str]]]:
    """Hit ``GET /v1/models`` to verify the credentials we just stored.

    Returns ``(ok, status, model_ids)``. ``status`` is one of the ops-library
    failure codes (``invalid_key`` / ``no_credits`` / ``rate_limited`` /
    ``network_error`` / …) and drives the failure submenu's wording.
    """
    from raven.config.update_providers import test_provider as probe

    console.print("  [dim]⏳ Verifying via GET /v1/models ...[/dim]")
    result = probe(provider)
    if result["ok"]:
        models = result.get("models_count")
        suffix = f" ({models} models available)" if models else ""
        console.print(f"  [green]✓ Connected!{suffix}[/green]")
        return True, "valid", result.get("model_ids")

    status = result.get("status", "unknown")
    hint_map = {
        "invalid_key": "Auth failed: the API key is invalid — check for typos / stray spaces.",
        "no_credits": "Account out of credits or not provisioned — top up and retry.",
        "rate_limited": "Rate limited — wait a bit and retry, or switch provider.",
        "network_error": "Network error reaching the provider — check network / proxy / VPN.",
        "oauth_token_missing": f"Run: raven provider login {provider.replace('_', '-')}",
    }
    msg = hint_map.get(status, f"Verification failed: {status}")
    console.print(
        f"  [yellow]✗ {msg}[/yellow]"
        + (f"  [dim]{result['error']}[/dim]" if result.get("error") else "")
    )
    return False, status, None


def _load_current_default_model() -> Optional[str]:
    """Read ``agents.defaults.model`` from the on-disk config, if it exists."""
    data = _load_raw_config()
    return (data or {}).get("agents", {}).get("defaults", {}).get("model") or None


def _model_routes_to_provider(model: str, spec: Any) -> bool:
    """True if ``model`` would auto-route to ``spec`` under ``provider='auto'``."""
    if not model or not spec:
        return False
    model_lower = model.lower()
    model_normalized = model_lower.replace("-", "_")
    if "/" in model_lower:
        prefix = model_lower.split("/", 1)[0].replace("-", "_")
        return prefix == spec.name
    return any(
        kw.lower() in model_lower or kw.lower().replace("-", "_") in model_normalized
        for kw in (getattr(spec, "keywords", None) or ())
    )


def _format_model_for_provider(spec: Any, model_id: str) -> str:
    """Apply ``spec.litellm_prefix`` to a raw ``/v1/models`` id when needed."""
    if not model_id:
        return model_id
    prefix = getattr(spec, "litellm_prefix", "") or ""
    if not prefix:
        return model_id
    if model_id.startswith(f"{prefix}/"):
        return model_id
    for skip in getattr(spec, "skip_prefixes", ()) or ():
        if model_id.startswith(skip):
            return model_id
    return f"{prefix}/{model_id}"


def _pick_model(
    spec: Any,
    *,
    current_model: Optional[str],
    model_ids: Optional[list[str]],
    user_provided_model: Optional[str],
    non_interactive: bool,
) -> str:
    """Decide the model string to write into ``agents.defaults.model``."""
    if user_provided_model:
        return user_provided_model

    if non_interactive:
        if not spec.default_model:
            raise typer.BadParameter(
                f"--model is required for provider '{spec.name}' "
                "(no built-in default model in registry)."
            )
        return spec.default_model

    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    if current_model and _model_routes_to_provider(current_model, spec):
        default_value = current_model
    else:
        default_value = spec.default_model or ""

    if model_ids:
        choices = [_format_model_for_provider(spec, mid) for mid in model_ids]
        if default_value and default_value not in choices:
            choices.insert(0, default_value)
        prompt_label = (
            f"Default model ({len(choices)} available — type to filter, Tab to complete):"
        )
        chosen = questionary.autocomplete(
            prompt_label,
            choices=choices,
            default=default_value,
            style=RAVEN_STYLE,
            ignore_case=True,
            match_middle=True,
        ).ask()
    elif default_value:
        chosen = questionary.text(
            f"Default model (press Enter for [{default_value}]):",
            default=default_value,
            style=RAVEN_STYLE,
        ).ask()
    else:
        chosen = questionary.text(
            f"Default model id for {spec.name}:",
            validate=lambda v: True if v.strip() else "Model id is required.",
            style=RAVEN_STYLE,
        ).ask()

    if not chosen:
        raise typer.Exit(1)
    return chosen.strip()


def _write_provider_fields(provider: str, fields: dict[str, Any]) -> None:
    """Thin wrapper that surfaces ops-library errors with friendly hints."""
    from pydantic import ValidationError

    from raven.config.update_providers import set_provider_fields

    try:
        set_provider_fields(provider, fields)
    except KeyError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(1)
    except RuntimeError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(1)
    except ValidationError as exc:
        console.print(f"[red]✗ Validation failed:[/red]\n{exc}")
        raise typer.Exit(1)


def _persist_default_model(model: Optional[str]) -> None:
    """Patch ``agents.defaults.model`` if we picked one."""
    if not model:
        return
    from raven.config.update import set_default_model

    set_default_model(model)


# ---------------------------------------------------------------------------
# Step 1 — connectivity-failure submenu + test probe
# ---------------------------------------------------------------------------


def _failure_choice(options: list[tuple[str, str]], *, non_interactive: bool) -> str:
    """Render a numbered failure submenu, return the chosen value.

    ``options`` is a list of ``(label, value)``. In non-interactive mode the
    last option (always "continue anyway") is auto-chosen so headless runs
    never block.
    """
    if non_interactive:
        return options[-1][1]
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    chosen = questionary.select(
        "What next?",
        choices=[questionary.Choice(label, value=value) for label, value in options],
        style=RAVEN_STYLE,
    ).ask()
    if chosen is None:
        raise typer.Exit(1)
    return chosen


def _run_test_probe(provider: str, *, non_interactive: bool, warnings: list[str]) -> str:
    """Send a one-shot test message; on failure offer retry/repick/continue.

    Returns one of ``"ok"`` / ``"continue"`` / ``"repick"``. ``"repick"`` asks
    the caller to re-run the model picker.
    """
    console.print(f'  [dim]Sending test message: "{DEFAULT_PROBE_MESSAGE}"[/dim]')
    try:
        text, tokens, elapsed = send_probe()
    except Exception as exc:
        console.print(f"  [red]✗ Test failed:[/red] {exc}")
        console.print(
            "  [dim]Run 'raven provider test' to re-check, or confirm the model is "
            "served by this provider.[/dim]"
        )
        print_probe_troubleshooting(provider)
        choice = _failure_choice(
            [
                ("1) Retry", "retry"),
                ("2) Re-pick model", "repick"),
                ("3) Continue anyway", "continue"),
            ],
            non_interactive=non_interactive,
        )
        if choice == "retry":
            return _run_test_probe(provider, non_interactive=non_interactive, warnings=warnings)
        if choice == "repick":
            return "repick"
        warnings.append("provider test message")
        return "continue"

    console.print(f"  [bold]▶ Agent:[/bold] {text}")
    extras: list[str] = []
    if tokens:
        extras.append(f"{tokens} tokens")
    extras.append(f"{elapsed:.1f}s")
    console.print(f"  [green]✓ {', '.join(extras)}[/green]")
    return "ok"


# ---------------------------------------------------------------------------
# Step 1 — add one provider (used by both first-run and the "add" entry)
# ---------------------------------------------------------------------------


def _configure_one_provider(
    *,
    provider: Optional[str],
    api_key: Optional[str],
    base_url: Optional[str],
    model: Optional[str],
    non_interactive: bool,
    warnings: list[str],
) -> Optional[dict[str, Any]]:
    """Drive one provider through pick → credentials → verify → model → test.

    Returns ``{"provider", "model"}`` on success, or ``None`` if the user
    chose to go back from the interactive provider picker.
    """
    from raven.providers.registry import find_by_name

    # Loop so "Switch provider" on a connectivity failure rewinds to the
    # picker instead of tearing the whole wizard down (keeps steps 2/3/4).
    # A provider passed by flag is used once; switching then requires the
    # interactive picker (or, in non-interactive mode, is impossible).
    flag_provider = provider
    while True:
        if flag_provider:
            provider = _validate_provider_name(flag_provider)
        else:
            if non_interactive:
                raise typer.BadParameter("--provider is required in non-interactive mode")
            picked = _select_provider()
            if picked is None:
                raise typer.Exit(1)
            if picked is _BACK:
                return None
            provider = picked

        spec = find_by_name(provider)
        is_oauth = bool(spec and spec.is_oauth)
        is_custom = provider == "custom"
        console.print(f"  [dim]Provider:[/dim] [cyan]{_provider_label(provider)}[/cyan]")

        custom_model = _collect_credentials(
            provider,
            is_oauth=is_oauth,
            is_custom=is_custom,
            api_key=api_key,
            base_url=base_url,
            model=model,
            non_interactive=non_interactive,
        )

        chosen_model = _resolve_model_with_test(
            spec,
            is_custom=is_custom,
            custom_model=custom_model,
            user_model_flag=model,
            non_interactive=non_interactive,
            warnings=warnings,
        )
        if chosen_model is None:
            # "Switch provider" — re-run the picker (drop the flag so the
            # second pass prompts rather than reusing the failed flag value).
            flag_provider = None
            continue
        _persist_default_model(chosen_model)
        return {"provider": provider, "model": chosen_model}


def _collect_credentials(
    provider: str,
    *,
    is_oauth: bool,
    is_custom: bool,
    api_key: Optional[str],
    base_url: Optional[str],
    model: Optional[str],
    non_interactive: bool,
) -> Optional[str]:
    """Auth setup: OAuth browser flow or api_key write. Returns the custom
    model id when the provider is ``custom`` (locked in here), else ``None``."""
    if is_oauth:
        if non_interactive:
            console.print(
                "[red]OAuth providers require an interactive browser flow.[/red]\n"
                "Run [cyan]raven provider login "
                f"{provider.replace('_', '-')}[/cyan] separately, then re-run "
                "onboard."
            )
            raise typer.Exit(2)
        _run_oauth_login(provider)
        return None

    if not api_key:
        if non_interactive:
            raise typer.BadParameter("--api-key is required in non-interactive mode")
        api_key = _prompt_api_key(provider)

    fields: dict[str, Any] = {"api_key": api_key}
    custom_model: Optional[str] = None
    if is_custom:
        if not base_url:
            if non_interactive:
                raise typer.BadParameter(
                    "--base-url is required when --provider=custom in non-interactive mode"
                )
            base_url = _prompt_base_url()
        fields["api_base"] = base_url
        if not model:
            if non_interactive:
                raise typer.BadParameter(
                    "--model is required when --provider=custom in non-interactive mode"
                )
            model = _prompt_custom_model()
        custom_model = model
    elif base_url:
        fields["api_base"] = base_url

    _write_provider_fields(provider, fields)
    return custom_model


def _resolve_model_with_test(
    spec: Any,
    *,
    is_custom: bool,
    custom_model: Optional[str],
    user_model_flag: Optional[str],
    non_interactive: bool,
    warnings: list[str],
) -> Optional[str]:
    """Verify connectivity → pick the default model → send a test probe.

    On a verify failure, offers a numbered submenu (retry / switch / continue).
    Only failures stop; success auto-advances. Returns the chosen model, or
    ``None`` to signal "switch provider" (the caller rewinds to the picker).
    """
    while True:
        ok, status, model_ids = _verify_provider(spec.name)
        if not ok:
            options = (
                [("1) Retry", "retry"), ("2) Continue anyway", "continue")]
                if status == "network_error"
                else [
                    ("1) Re-enter key", "rekey"),
                    ("2) Switch provider", "switch"),
                    ("3) Continue anyway", "continue"),
                ]
            )
            choice = _failure_choice(options, non_interactive=non_interactive)
            if choice == "retry":
                continue
            if choice == "rekey" and not non_interactive:
                _write_provider_fields(spec.name, {"api_key": _prompt_api_key(spec.name)})
                continue
            if choice == "switch":
                return None
            warnings.append("provider connectivity")
            model_ids = None
        break

    if is_custom:
        assert custom_model is not None, "custom provider must have model set earlier"
        return custom_model

    current = _load_current_default_model()
    while True:
        chosen = _pick_model(
            spec,
            current_model=current,
            model_ids=model_ids,
            user_provided_model=user_model_flag,
            non_interactive=non_interactive,
        )
        _persist_default_model(chosen)
        result = _run_test_probe(spec.name, non_interactive=non_interactive, warnings=warnings)
        if result != "repick":
            return chosen
        current = chosen
        user_model_flag = None


# ---------------------------------------------------------------------------
# Step 1 — multi-provider entry (existing-config branch: done / add / edit)
# ---------------------------------------------------------------------------


def _manage_existing_providers(*, non_interactive: bool) -> None:
    """Edit/remove submenu for already-configured providers (interactive only)."""
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    while True:
        configured = _configured_providers()
        if not configured:
            return
        choices = [questionary.Choice(_provider_label(n), value=n) for n in configured]
        choices.append(questionary.Choice("0) Back", value=_BACK))
        target = questionary.select(
            "Pick a provider to manage:", choices=choices, style=RAVEN_STYLE
        ).ask()
        if target is None or target is _BACK:
            return

        action = questionary.select(
            f"What would you like to do with {_provider_label(target)}?",
            choices=[
                questionary.Choice("1) Update API key", value="update"),
                questionary.Choice("2) Remove (clear this provider's key)", value="remove"),
                questionary.Choice("0) Back", value=_BACK),
            ],
            style=RAVEN_STYLE,
        ).ask()
        if action is None or action is _BACK:
            continue
        if action == "update":
            _write_provider_fields(target, {"api_key": _prompt_api_key(target)})
            console.print(f"  [green]✓ Updated {_provider_label(target)}.[/green]")
        elif action == "remove":
            current = _load_current_default_model()
            from raven.providers.registry import find_by_name

            spec = find_by_name(target)
            if current and spec and _model_routes_to_provider(current, spec):
                confirm = questionary.confirm(
                    f"The current default model comes from {_provider_label(target)}; "
                    "removing it means you'll need to pick a new default. Remove anyway?",
                    default=False,
                    style=RAVEN_STYLE,
                ).ask()
                if not confirm:
                    continue
            _write_provider_fields(target, {"api_key": ""})
            console.print(
                f"  [green]✓ Removed {_provider_label(target)}'s configuration.[/green]"
            )


def _step1_provider(
    *,
    provider: Optional[str],
    api_key: Optional[str],
    base_url: Optional[str],
    model: Optional[str],
    non_interactive: bool,
    warnings: list[str],
) -> object:
    """Step 1 screen. Returns ``_BACK`` only when the user backs out of the
    first-run picker on the welcome screen (handled by the runner)."""
    _step_header(1, "Choose your LLM provider")

    configured = _configured_providers()
    if non_interactive or not configured:
        result = _configure_one_provider(
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            model=model,
            non_interactive=non_interactive,
            warnings=warnings,
        )
        if result is None:
            return _BACK
        return None

    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    while True:
        names = ", ".join(_provider_label(n) for n in _configured_providers())
        console.print(f"  [dim]Configured:[/dim] {names}")
        action = questionary.select(
            "LLM providers:",
            choices=[
                questionary.Choice("1) Done — continue", value="done"),
                questionary.Choice("2) Add another provider", value="add"),
                questionary.Choice("3) Edit / remove a provider", value="edit"),
            ],
            style=RAVEN_STYLE,
        ).ask()
        if action in (None, "done"):
            # Re-pick a default model if the prior one was removed.
            if not _load_current_default_model() and _configured_providers():
                console.print(
                    "[yellow]No default model set — add or re-pick a provider.[/yellow]"
                )
                continue
            return None
        if action == "add":
            _configure_one_provider(
                provider=None,
                api_key=None,
                base_url=None,
                model=None,
                non_interactive=False,
                warnings=warnings,
            )
        elif action == "edit":
            _manage_existing_providers(non_interactive=non_interactive)


# ---------------------------------------------------------------------------
# Step 2 — sandbox / run location
# ---------------------------------------------------------------------------


def _current_sandbox_backend() -> str:
    """Read ``tools.sandbox.backend`` from disk; defaults to ``none``."""
    data = _load_raw_config()
    return ((data.get("tools") or {}).get("sandbox") or {}).get("backend") or "none"


def _persist_sandbox_backend(backend: str) -> None:
    """Patch ``sandbox.backend`` on the on-disk config via the ops layer."""
    from raven.config.update import set_sandbox_backend

    set_sandbox_backend(backend)


def _probe_boxlite() -> tuple[bool, str]:
    """Probe boxlite availability. Returns ``(ok, reason)``.

    ``reason`` ∈ ``"ok"`` / ``"missing"`` / ``"error"``. The runtime import is
    the same availability gate ``build_executor`` uses for the boxlite backend.
    """
    console.print("  [dim]⏳ Probing sandbox availability...[/dim]")
    try:
        import boxlite  # noqa: F401
    except ImportError:
        return False, "missing"
    except Exception:
        return False, "error"
    return True, "ok"


def _step2_sandbox(*, skip: bool, non_interactive: bool) -> object:
    """Step 2 — choose run location (host / boxlite sandbox)."""
    _step_header(2, "Choose where Raven runs code / commands")

    if skip or non_interactive:
        console.print("  [dim]Keeping run location: host (direct).[/dim]")
        return None

    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    current = _current_sandbox_backend()
    choices: list[Any] = []
    if current != "none":
        choices.append(questionary.Choice("Keep current: sandbox (boxlite)", value="keep"))
    choices.extend(
        [
            questionary.Choice("1) Host (direct) — simplest, runs on your machine", value="none"),
            questionary.Choice("2) Sandbox isolation (boxlite) — safer microVM", value="boxlite"),
            questionary.Choice("0) Back", value=_BACK),
        ]
    )

    picked = questionary.select("Run location:", choices=choices, style=RAVEN_STYLE).ask()
    if picked is None:
        raise typer.Exit(1)
    if picked is _BACK:
        return _BACK
    if picked == "keep":
        return None
    if picked == "none":
        _persist_sandbox_backend("none")
        console.print("  [green]✓ Running directly on the host.[/green]")
        return None

    # boxlite — probe before committing.
    while True:
        ok, reason = _probe_boxlite()
        if ok:
            _persist_sandbox_backend("boxlite")
            console.print(
                "  [green]✓ Sandbox available. Using default resources "
                "(2 CPU / 2 GB / network); tune in the config file if needed.[/green]"
            )
            return None
        console.print(
            "  [yellow]✗ Sandbox runtime (boxlite) not detected; install the "
            "dependency first.[/yellow]"
        )
        choice = _failure_choice(
            [
                ("1) Fall back to host", "host"),
                ("2) Retry after install", "retry"),
                ("0) Skip", "skip"),
            ],
            non_interactive=non_interactive,
        )
        if choice == "retry":
            continue
        if choice == "host":
            _persist_sandbox_backend("none")
            console.print("  [green]✓ Running directly on the host.[/green]")
        return None


# ---------------------------------------------------------------------------
# Step 3 — chat channel (stackable)
# ---------------------------------------------------------------------------


def _enabled_channels() -> list[str]:
    """Names of channels currently enabled on disk."""
    data = _load_raw_config()
    channels = data.get("channels") or {}
    return [
        name
        for name, c in channels.items()
        if isinstance(c, dict) and c.get("enabled")
    ]


# Curated channel order: China-domestic first, then overseas. Channels not
# listed (e.g. a newly added adapter) fall to the end in alphabetical order so
# the picker never silently hides one.
_CHANNEL_ORDER = (
    "weixin", "feishu", "dingtalk", "wecom", "qq", "mochat",
    "telegram", "discord", "whatsapp", "slack", "matrix", "email",
)


def _ordered_channel_names() -> list[str]:
    from raven.channels.registry import discover_channel_names

    rank = {name: i for i, name in enumerate(_CHANNEL_ORDER)}
    return sorted(discover_channel_names(), key=lambda n: (rank.get(n, len(rank)), n))


def _select_channel() -> Optional[str]:
    """List available channels via the registry and let the user pick one."""
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    names = _ordered_channel_names()
    choices = [questionary.Choice(n, value=n) for n in names]
    choices.append(questionary.Choice("0) Back", value=_BACK))
    picked = questionary.select("Channel:", choices=choices, style=RAVEN_STYLE).ask()
    return picked


def _prompt_channel_fields(channel: str) -> dict[str, Any]:
    """Reflect a channel's Pydantic schema and prompt for credential-like fields."""
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE
    from raven.config.update_channels import channel_field_specs

    try:
        specs = channel_field_specs(channel)
    except KeyError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(1)

    fields: dict[str, Any] = {}
    for path, spec in specs.items():
        if path == "enabled":
            continue
        if spec.get("type", "") != "str":
            continue
        default = spec.get("default")
        if default not in ("", None):
            continue
        description = spec.get("description", "")
        prompt_label = f"{path}" + (f" — {description}" if description else "") + ":"
        if spec.get("is_secret"):
            value = questionary.password(prompt_label, style=RAVEN_STYLE).ask()
        else:
            value = questionary.text(prompt_label, style=RAVEN_STYLE).ask()
        if value is None:
            raise typer.Exit(1)
        if value:
            fields[path] = value
    return fields


def _enable_channel(channel: str, fields: dict[str, Any]) -> None:
    """Thin wrapper for ``enable_channel`` that surfaces ops errors with hints."""
    from pydantic import ValidationError

    from raven.config.update_channels import enable_channel

    try:
        enable_channel(channel, fields)
    except KeyError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(1)
    except ValidationError as exc:
        console.print(f"[red]✗ Validation failed:[/red]\n{exc}")
        raise typer.Exit(1)


def _channel_uses_interactive_login(channel: str) -> bool:
    """True for scancode/QR channels (WeChat / WhatsApp) that pair via a live
    login flow rather than reflected credential fields."""
    try:
        from raven.channels.registry import discover_specs

        spec = discover_specs().get(channel)
        return bool(spec and spec.capabilities.interactive_login)
    except Exception:
        return False


# Scancode channels whose QR login is served by a Node.js bridge — these need
# Node/npm present before login can even start. The whatsapp adapter's
# ``login`` checks ``shutil.which("npm")`` and merely logs+returns False when
# it's absent, so we detect the missing-runtime case up front to show a
# meaningful "install Node / skip" menu rather than a pointless "re-show QR".
_NODE_BRIDGE_CHANNELS = {"whatsapp"}


def _node_runtime_missing(channel: str) -> bool:
    """True iff ``channel`` needs a Node bridge and ``npm`` isn't on PATH."""
    if channel not in _NODE_BRIDGE_CHANNELS:
        return False
    import shutil

    return shutil.which("npm") is None


def _handle_missing_node(channel: str) -> str:
    """Show the Node-missing submenu (install-then-retry / skip).

    Returns ``"retry"`` (re-check after install) or ``"skip"`` (leave the
    channel enabled-but-unauthenticated). A pointless "re-show QR" is
    intentionally absent — there's no bridge to render a QR without Node.
    """
    console.print(
        f"  [yellow]✗ Node.js / npm not found (the {channel} bridge needs it). "
        "Install Node.js, then retry.[/yellow]"
    )
    choice = _failure_choice(
        [
            ("1) Retry after install", "retry"),
            ("0) Skip", "skip"),
        ],
        non_interactive=False,
    )
    return choice


def _scancode_login(channel: str) -> None:
    """Run a scancode channel's real QR login (reuses ``channel.login``).

    Mirrors ``raven channels login``: enable the channel so its config section
    persists, build the adapter via its spec factory, then drive
    ``await channel.login()`` (which for WhatsApp builds the bridge, displays
    the QR, and waits). A failed / timed-out login drops into a numbered
    submenu (re-show QR / skip / continue). Node-bridge channels missing
    Node/npm get a dedicated install-then-retry menu instead (a "re-show QR"
    choice is meaningless with no bridge).
    """
    import asyncio

    from raven.channels.registry import discover_specs

    # Enable first so the config section exists for the factory to read and so
    # the channel is wired even if the user later finishes login out-of-band.
    _enable_channel(channel, {})

    specs = discover_specs()
    spec = specs.get(channel)
    if spec is None:
        console.print(f"[red]✗ Unknown channel: {channel}[/red]")
        return

    while True:
        # Node-bridge channels: gate on the runtime up front so a missing
        # Node/npm shows a useful install menu, not a "re-show QR" no-op.
        if _node_runtime_missing(channel):
            if _handle_missing_node(channel) == "retry":
                continue
            console.print(
                f"  [dim]Skipped {channel}; install Node.js then run "
                f"raven channels login {channel}.[/dim]"
            )
            return

        from raven.config.loader import load_config

        channel_cfg = getattr(load_config().channels, channel, None)
        if channel_cfg is None:
            console.print(f"[red]✗ No config section for channel: {channel}[/red]")
            return
        adapter = spec.factory(channel_cfg)
        console.print(f"  [dim]Starting {spec.display_name} QR login...[/dim]")
        try:
            ok = asyncio.run(adapter.login(force=True))
        except Exception as exc:
            console.print(f"  [yellow]✗ Login failed: {exc}[/yellow]")
            ok = False
        if ok:
            console.print(f"  [green]✓ Logged in; {channel} connected.[/green]")
            return
        choice = _failure_choice(
            [
                ("1) Re-show QR code", "retry"),
                ("2) Skip this channel", "skip"),
                ("3) Continue", "continue"),
            ],
            non_interactive=False,
        )
        if choice == "retry":
            continue
        if choice == "skip":
            # Leave the channel enabled but unauthenticated — the user can run
            # `raven channels login <name>` later to finish pairing.
            console.print(
                f"  [dim]Skipped scan; finish later with "
                f"raven channels login {channel}.[/dim]"
            )
        return


def _add_one_channel() -> None:
    """Pick + (scancode login | reflect-prompt) + enable one channel."""
    channel = _select_channel()
    if channel is None or channel is _BACK:
        return
    if _channel_uses_interactive_login(channel):
        _scancode_login(channel)
        return
    fields = _prompt_channel_fields(channel)
    _enable_channel(channel, fields)
    console.print(f"  [green]✓ {channel} enabled.[/green]")


def _manage_existing_channels() -> None:
    """Edit/disable submenu for already-enabled channels."""
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE
    from raven.config.update_channels import disable_channel, set_channel_fields

    while True:
        enabled = _enabled_channels()
        if not enabled:
            return
        choices = [questionary.Choice(n, value=n) for n in enabled]
        choices.append(questionary.Choice("0) Back", value=_BACK))
        target = questionary.select(
            "Pick a channel to manage:", choices=choices, style=RAVEN_STYLE
        ).ask()
        if target is None or target is _BACK:
            return
        action = questionary.select(
            f"What would you like to do with {target}?",
            choices=[
                questionary.Choice("1) Edit config (re-enter fields)", value="edit"),
                questionary.Choice("2) Disable (keep credentials)", value="disable"),
                questionary.Choice("0) Back", value=_BACK),
            ],
            style=RAVEN_STYLE,
        ).ask()
        if action is None or action is _BACK:
            continue
        if action == "edit":
            fields = _prompt_channel_fields(target)
            if fields:
                set_channel_fields(target, fields)
            console.print(f"  [green]✓ {target} config updated.[/green]")
        elif action == "disable":
            disable_channel(target)
            console.print(
                f"  [green]✓ Disabled {target} (credentials kept; re-enable later "
                f"with raven channels enable {target}).[/green]"
            )


def _step3_channel(*, channel: Optional[str], skip: bool, non_interactive: bool) -> object:
    """Step 3 — optionally enable chat channel(s)."""
    _step_header(3, "(Optional) Connect a chat tool so you can talk to Raven in an IM")

    if skip:
        console.print("  [dim]Skipped via --skip-channel.[/dim]")
        return None

    if non_interactive:
        if channel:
            console.print(
                f"[red]--channel {channel} given but non-interactive mode can't "
                "prompt for credential fields.[/red]\n"
                f"Run [cyan]raven channels enable {channel} --<field> <value> ...[/cyan] "
                "after onboard finishes."
            )
            raise typer.Exit(2)
        console.print("  [dim]Skipped (non-interactive, --channel not given).[/dim]")
        return None

    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    if channel:
        if _channel_uses_interactive_login(channel):
            _scancode_login(channel)
        else:
            fields = _prompt_channel_fields(channel)
            _enable_channel(channel, fields)
            console.print(f"  [green]✓ {channel} enabled.[/green]")
        return None

    while True:
        enabled = _enabled_channels()
        if not enabled:
            action = questionary.select(
                "Connect a chat channel?",
                choices=[
                    questionary.Choice("1) Add a channel", value="add"),
                    questionary.Choice("0) Skip (add later with raven channels enable)", value="skip"),
                ],
                style=RAVEN_STYLE,
            ).ask()
            if action in (None, "skip"):
                console.print("  [dim]Skipped.[/dim]")
                return None
            _add_one_channel()
            continue

        console.print(f"  [dim]Enabled:[/dim] {', '.join(enabled)}")
        action = questionary.select(
            "Chat channels:",
            choices=[
                questionary.Choice("0) Done — next step", value="done"),
                questionary.Choice("1) Add a channel", value="add"),
                questionary.Choice("2) Edit / remove a channel", value="edit"),
            ],
            style=RAVEN_STYLE,
        ).ask()
        if action in (None, "done"):
            return None
        if action == "add":
            _add_one_channel()
        elif action == "edit":
            _manage_existing_channels()


# ---------------------------------------------------------------------------
# Step 4 — EverOS long-term memory
# ---------------------------------------------------------------------------


def _set_memory_backend(backend: Optional[str]) -> None:
    """Set ``memory.backend`` (``"everos"`` / ``None``) via the ops layer."""
    from raven.config.update import set_memory_backend

    set_memory_backend(backend)


def _everos_section(section: str) -> dict[str, Any]:
    """Current values of an EverOS section, or ``{}``."""
    from raven.config.update_everos import load_everos_config

    return load_everos_config().get(section, {}) or {}


def _memory_enabled() -> bool:
    """True iff ``memory.backend`` is ``everos`` on disk."""
    data = _load_raw_config()
    return (data.get("memory") or {}).get("backend") == "everos"


# Providers whose main model can be reused as the EverOS memory LLM: they
# speak the OpenAI chat-completions protocol that EverOS's bare OpenAI client
# requires. OAuth providers (github_copilot / openai_codex) and non-OpenAI
# wire protocols (anthropic / gemini) are excluded.
_OPENAI_COMPATIBLE_PROVIDERS = {"openrouter", "openai", "deepseek", "custom"}

# Fallback OpenAI-compatible base URLs for providers whose registry
# ``default_api_base`` is empty (they rely on the SDK's built-in default,
# which EverOS's bare client doesn't know). EverOS needs an explicit base_url.
_PROVIDER_BASE_URL_FALLBACK = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
}


def _resolve_model_provider(model: str) -> Optional[str]:
    """Best-effort: which configured provider does ``model`` belong to?

    Prefixed models (``openrouter/...`` / ``openai/gpt-4o``) read off the head.
    A custom endpoint stores its model as a BARE id (e.g. ``qwen-max``) with no
    prefix, so an unrecognized head falls back to ``"custom"`` when a custom
    provider is actually configured with a key. Returns ``None`` when no match.
    """
    if not model:
        return None
    head = model.split("/", 1)[0].replace("-", "_")
    if "/" in model:
        from raven.config.update_providers import provider_field_specs

        try:
            provider_field_specs(head)
            return head
        except KeyError:
            pass
    # No usable prefix → could be a bare custom-endpoint model.
    custom = (_load_raw_config().get("providers") or {}).get("custom") or {}
    if custom.get("apiKey"):
        return "custom"
    # A bare id that still matches a known provider head (rare; e.g. a direct
    # provider's bare default before prefixing) — accept the head if known.
    return head if head in _OPENAI_COMPATIBLE_PROVIDERS else None


def _model_is_openai_compatible(model: Optional[str]) -> bool:
    """Heuristic: can the main chat model's provider be reused for memory LLM?

    EverOS's memory LLM uses a bare OpenAI client, so the main model is
    reusable only when its provider speaks the OpenAI chat protocol. Custom
    endpoints are OpenAI-compatible by definition (the wizard only offers
    ``custom`` for OpenAI-compatible endpoints).
    """
    if not model:
        return False
    return _resolve_model_provider(model) in _OPENAI_COMPATIBLE_PROVIDERS


def _resolve_reuse_llm_creds(main_model: str) -> dict[str, Optional[str]]:
    """Map a litellm-style main model to bare EverOS LLM settings.

    EverOS sends ``EVEROS_LLM__MODEL`` to ``base_url`` via a bare OpenAI
    client, so:
      - strip the provider's litellm prefix to the bare model id the upstream
        endpoint expects (``openrouter/anthropic/claude-x`` → ``anthropic/claude-x``;
        a custom endpoint's bare id is used as-is);
      - resolve the provider's real ``base_url`` (configured ``apiBase`` →
        registry ``default_api_base`` → a known fallback);
      - carry the provider's stored api_key.
    """
    from raven.providers.registry import find_by_name

    provider = _resolve_model_provider(main_model) or main_model.split("/", 1)[0].replace("-", "_")
    spec = find_by_name(provider)
    prov_cfg = (_load_raw_config().get("providers") or {}).get(provider, {})

    # Strip the litellm routing prefix to the bare model id the upstream
    # endpoint expects. Direct providers (openai / deepseek / gemini) carry a
    # ``{provider}/`` route prefix that litellm consumes but the raw OpenAI
    # client must not see; gateways (openrouter) carry their litellm_prefix.
    # Custom endpoints store a bare id already, so no prefix matches → unchanged.
    bare_model = main_model
    litellm_prefix = getattr(spec, "litellm_prefix", "") if spec else ""
    for prefix in (litellm_prefix, provider):
        if prefix and bare_model.startswith(f"{prefix}/"):
            bare_model = bare_model.split("/", 1)[1]
            break

    base_url = (
        prov_cfg.get("apiBase")
        or (getattr(spec, "default_api_base", "") if spec else "")
        or _PROVIDER_BASE_URL_FALLBACK.get(provider)
    )
    return {
        "model": bare_model,
        "api_key": prov_cfg.get("apiKey"),
        "base_url": base_url,
    }


def _prompt_text(label: str, *, secret: bool = False, default: str = "") -> str:
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    if secret:
        value = questionary.password(label, style=RAVEN_STYLE).ask()
    else:
        value = questionary.text(label, default=default, style=RAVEN_STYLE).ask()
    if value is None:
        raise typer.Exit(1)
    return value.strip()


def _probe_everos_endpoint(
    label: str, *, model: Optional[str], api_key: Optional[str], base_url: Optional[str]
) -> tuple[bool, str]:
    """Lightweight ``GET {base_url}/models`` connectivity check for an EverOS
    model endpoint. Returns ``(ok, detail)``; never raises."""
    import httpx

    if not base_url:
        return False, "no base_url configured"
    url = base_url.rstrip("/") + "/models"
    if "/v1" not in base_url:
        url = base_url.rstrip("/") + "/v1/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return False, f"network error: {exc}"
    if resp.status_code == 200:
        return True, "ok"
    return False, f"HTTP {resp.status_code}"


def _verify_everos_model(
    label: str,
    *,
    model: Optional[str],
    api_key: Optional[str],
    base_url: Optional[str],
    non_interactive: bool,
    warnings: list[str],
) -> bool:
    """Probe one EverOS model endpoint, offering retry/continue on failure.

    Returns ``True`` if the caller should keep the just-written config, or
    ``False`` to re-prompt (the "Re-enter" branch). Failures that the user
    chooses to ignore are recorded in ``warnings`` for the screen-5 summary.
    """
    console.print(f"  [dim]⏳ Verifying {label}...[/dim]")
    ok, detail = _probe_everos_endpoint(
        label, model=model, api_key=api_key, base_url=base_url
    )
    if ok:
        console.print(f"  [green]✓ {label} connected.[/green]")
        return True
    console.print(f"  [yellow]✗ Couldn't reach {label}: {detail}[/yellow]")
    choice = _failure_choice(
        [
            ("1) Re-enter", "rekey"),
            ("2) Continue anyway", "continue"),
        ],
        non_interactive=non_interactive,
    )
    if choice == "rekey":
        return False
    warnings.append(f"memory {label}")
    return True


def _config_memory_llm(*, main_model: Optional[str], non_interactive: bool, warnings: list[str]) -> None:
    """4.1 — memory LLM (required once memory is enabled)."""
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE
    from raven.config.update_everos import set_everos_section

    current = _everos_section("llm").get("model")
    reuse_ok = _model_is_openai_compatible(main_model)

    if current:
        choices = [questionary.Choice(f"Keep current: {current}", value="keep")]
        if reuse_ok:
            choices.append(questionary.Choice("Reuse main chat model", value="reuse"))
        choices.append(questionary.Choice("Configure separately", value="separate"))
        action = questionary.select("Memory LLM:", choices=choices, style=RAVEN_STYLE).ask()
        if action is None:
            raise typer.Exit(1)
        if action == "keep":
            return
    elif reuse_ok:
        action = questionary.select(
            "Configure the memory LLM:",
            choices=[
                questionary.Choice(f"Reuse main chat model ({main_model})", value="reuse"),
                questionary.Choice("Configure separately", value="separate"),
            ],
            style=RAVEN_STYLE,
        ).ask()
        if action is None:
            raise typer.Exit(1)
    else:
        console.print(
            "  [dim]Your main model isn't OpenAI-compatible; the memory LLM needs a "
            "separate OpenAI-compatible endpoint.[/dim]"
        )
        action = "separate"

    if action == "reuse":
        # Reuse the main provider's credentials: strip the litellm prefix to
        # the bare model id and resolve the provider's real base_url/key so
        # EverOS's bare OpenAI client can call it.
        creds = _resolve_reuse_llm_creds(main_model or "")
        set_everos_section("llm", creds)
        _verify_everos_model(
            "memory LLM",
            model=creds["model"],
            api_key=creds["api_key"],
            base_url=creds["base_url"],
            non_interactive=non_interactive,
            warnings=warnings,
        )
        return

    while True:
        model = _prompt_text("Memory LLM model id:")
        api_key = _prompt_text("API key (hidden):", secret=True)
        base_url = _prompt_text("base_url (must include /v1):")
        set_everos_section("llm", {"model": model, "api_key": api_key, "base_url": base_url})
        if _verify_everos_model(
            "memory LLM",
            model=model,
            api_key=api_key,
            base_url=base_url,
            non_interactive=non_interactive,
            warnings=warnings,
        ):
            return


def _config_memory_embedding(*, non_interactive: bool, warnings: list[str]) -> None:
    """4.2 — embedding (required once memory is enabled)."""
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE
    from raven.config.update_everos import set_everos_section

    current = _everos_section("embedding").get("model")
    if current:
        action = questionary.select(
            "Memory embedding:",
            choices=[
                questionary.Choice(f"Keep current: {current}", value="keep"),
                questionary.Choice("Reconfigure", value="redo"),
            ],
            style=RAVEN_STYLE,
        ).ask()
        if action is None:
            raise typer.Exit(1)
        if action == "keep":
            return

    while True:
        model = _prompt_text("Embedding model id:")
        api_key = _prompt_text("API key (hidden):", secret=True)
        base_url = _prompt_text("base_url:")
        set_everos_section(
            "embedding", {"model": model, "api_key": api_key, "base_url": base_url}
        )
        if _verify_everos_model(
            "embedding",
            model=model,
            api_key=api_key,
            base_url=base_url,
            non_interactive=non_interactive,
            warnings=warnings,
        ):
            return


def _maybe_reuse_memory_llm_creds() -> tuple[Optional[str], Optional[str]]:
    """Offer to reuse the memory LLM's key/base_url; return ``(api_key, base_url)``.

    Default is to reuse (spec §617①). Returns ``(None, None)`` when the user
    opts to enter fresh credentials.
    """
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    llm = _everos_section("llm")
    if not (llm.get("api_key") and llm.get("base_url")):
        return None, None
    reuse = questionary.confirm(
        "Reuse the memory LLM's api_key / base_url?",
        default=True,
        style=RAVEN_STYLE,
    ).ask()
    if reuse is None:
        raise typer.Exit(1)
    if reuse:
        return llm.get("api_key"), llm.get("base_url")
    return None, None


def _config_memory_rerank(*, non_interactive: bool) -> None:
    """4.3 — rerank (optional)."""
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE
    from raven.config.update_everos import clear_everos_section, set_everos_section

    current = _everos_section("rerank").get("model")
    if current:
        action = questionary.select(
            "Memory rerank:",
            choices=[
                questionary.Choice(f"Keep current: {current}", value="keep"),
                questionary.Choice("Reconfigure", value="redo"),
                questionary.Choice("Disable", value="off"),
            ],
            style=RAVEN_STYLE,
        ).ask()
        if action is None:
            raise typer.Exit(1)
        if action == "keep":
            return
        if action == "off":
            clear_everos_section("rerank")
            console.print("  [dim]Rerank disabled.[/dim]")
            return
    else:
        action = questionary.select(
            "Rerank model (optional — improves ranking, slightly slower):",
            choices=[
                questionary.Choice("1) Configure rerank", value="redo"),
                questionary.Choice("0) Skip (no reranking)", value="skip"),
            ],
            style=RAVEN_STYLE,
        ).ask()
        if action in (None, "skip"):
            console.print("  [dim]Skipped reranking; memory retrieval still works.[/dim]")
            return

    provider = questionary.select(
        "Rerank service type:",
        choices=[
            questionary.Choice("1) deepinfra", value="deepinfra"),
            questionary.Choice("2) vllm", value="vllm"),
        ],
        style=RAVEN_STYLE,
    ).ask()
    if provider is None:
        raise typer.Exit(1)
    model = _prompt_text("Rerank model id:")
    api_key, base_url = _maybe_reuse_memory_llm_creds()
    if api_key is None:
        api_key = _prompt_text("API key (hidden):", secret=True)
        base_url = _prompt_text("base_url:")
    set_everos_section(
        "rerank",
        {"provider": provider, "model": model, "api_key": api_key, "base_url": base_url},
    )
    console.print("  [green]✓ rerank configured.[/green]")


def _config_memory_multimodal(*, non_interactive: bool) -> None:
    """4.4 — multimodal (optional)."""
    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE
    from raven.config.update_everos import clear_everos_section, set_everos_section

    current = _everos_section("multimodal").get("model")
    if current:
        action = questionary.select(
            "Memory multimodal:",
            choices=[
                questionary.Choice(f"Keep current: {current}", value="keep"),
                questionary.Choice("Reconfigure", value="redo"),
                questionary.Choice("Disable", value="off"),
            ],
            style=RAVEN_STYLE,
        ).ask()
        if action is None:
            raise typer.Exit(1)
        if action == "keep":
            return
        if action == "off":
            clear_everos_section("multimodal")
            console.print("  [dim]Multimodal disabled.[/dim]")
            return
    else:
        action = questionary.select(
            "Multimodal model (optional — only for image / PDF / audio memory):",
            choices=[
                questionary.Choice("1) Configure multimodal", value="redo"),
                questionary.Choice("0) Skip", value="skip"),
            ],
            style=RAVEN_STYLE,
        ).ask()
        if action in (None, "skip"):
            console.print("  [dim]Skipped; text-only memory is unaffected.[/dim]")
            return

    model = _prompt_text("Multimodal model id:")
    api_key, base_url = _maybe_reuse_memory_llm_creds()
    if api_key is None:
        api_key = _prompt_text("API key (hidden):", secret=True)
        base_url = _prompt_text("base_url:")
    set_everos_section("multimodal", {"model": model, "api_key": api_key, "base_url": base_url})
    console.print("  [green]✓ multimodal configured.[/green]")


def _step4_memory(*, skip: bool, non_interactive: bool, main_model: Optional[str], warnings: list[str]) -> object:
    """Step 4 — EverOS long-term memory (enable + model sub-screens).

    Critical invariant: EVERY exit path must leave ``memory.backend`` written
    explicitly. The schema default is ``"everos"``, so any path that returns
    without setting it would activate EverOS at runtime with no llm/embedding
    configured (a broken state). We therefore default to ``None`` (native
    Markdown) on skip / non-interactive / decline, and only flip to ``everos``
    once the required models are configured. The one exception is the
    already-enabled "keep current" path: backend is already ``everos`` and the
    models are already on disk, so it's left untouched.
    """
    _step_header(4, "EverOS long-term memory")

    if skip or non_interactive:
        # Never configured the required models here → disable backend-driven
        # memory so runtime doesn't activate EverOS without an llm/embedding.
        # (Only flip an already-enabled+configured setup is preserved.)
        if not (_memory_enabled() and _everos_section("llm").get("model")):
            _set_memory_backend(None)
        console.print("  [dim]Keeping native Markdown memory.[/dim]")
        return None

    questionary = _require_questionary()
    from raven.cli._styles import RAVEN_STYLE

    if _memory_enabled():
        action = questionary.select(
            "EverOS long-term memory:",
            choices=[
                questionary.Choice("Keep current: EverOS enabled", value="keep"),
                questionary.Choice("Reconfigure", value="redo"),
            ],
            style=RAVEN_STYLE,
        ).ask()
        if action is None:
            raise typer.Exit(1)
        if action == "keep":
            return None  # backend already "everos" + models on disk; leave as-is
    else:
        action = questionary.select(
            "Enable EverOS long-term memory? (stronger memory; needs an llm + embedding)",
            choices=[
                questionary.Choice("1) Enable EverOS long-term memory", value="on"),
                questionary.Choice("0) Don't enable (use native Markdown memory)", value="off"),
            ],
            style=RAVEN_STYLE,
        ).ask()
        if action in (None, "off"):
            _set_memory_backend(None)
            console.print("  [dim]Using native Markdown memory.[/dim]")
            return None

    # Configure required models FIRST, then flip the backend on — so a Ctrl+C
    # mid-configuration leaves backend at its prior (disabled) value rather
    # than an enabled-but-modelless state.
    _config_memory_llm(main_model=main_model, non_interactive=non_interactive, warnings=warnings)
    _config_memory_embedding(non_interactive=non_interactive, warnings=warnings)
    _config_memory_rerank(non_interactive=non_interactive)
    _config_memory_multimodal(non_interactive=non_interactive)
    _set_memory_backend("everos")
    return None


# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------


def _print_next_steps(*, warnings: list[str]) -> None:
    if warnings:
        console.print(
            "\n[bold yellow]⚠ Setup finished, but these items didn't pass a "
            f"connectivity test: {', '.join(warnings)}.[/bold yellow]"
        )
        console.print(
            "[dim]Fix them before relying on the related features "
            "(run 'raven doctor' to re-check).[/dim]"
        )
    else:
        console.print("\n[bold green]🎉 Setup complete![/bold green]")

    console.print("\nGet started:")
    console.print('  [cyan]raven[/cyan]                                # launch the native TUI (default)')
    console.print('  [cyan]raven tui[/cyan]                            # same, explicit')
    console.print('  [cyan]raven gateway[/cyan]                        # run the gateway (serve channels)')
    console.print('  [cyan]raven agent -m "hello, world"[/cyan]        # one-shot question')


# ---------------------------------------------------------------------------
# Wizard runner (screen state machine) + reusable entry point
# ---------------------------------------------------------------------------


def run_wizard(
    *,
    provider: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    channel: Optional[str] = None,
    skip_sandbox: bool = False,
    skip_channel: bool = False,
    skip_memory: bool = False,
    non_interactive: bool = False,
    yes: bool = False,
    reset: bool = False,
) -> None:
    """Run the 4-step onboarding wizard end-to-end.

    The reusable entry point: the ``onboard`` CLI command and the startup gate
    both call this. Screens form a state machine so a ``0) Back`` choice can
    rewind one step; Ctrl+C exits keeping whatever was already written.
    """
    _check_tty_or_die(non_interactive)
    _handle_existing_config(reset=reset, yes=yes, non_interactive=non_interactive)
    _bootstrap_empty_config()

    console.print("\n[bold cyan]✨ Welcome to the Raven setup wizard[/bold cyan]")
    console.print(
        "We'll configure, in order: ① LLM  ② run location  ③ chat channel  ④ long-term memory\n"
        "[dim]Press Ctrl+C anytime to quit (anything already written is kept).[/dim]"
    )

    warnings: list[str] = []

    # Screen state machine. Each screen returns ``_BACK`` to rewind or anything
    # else to advance. Step 1 is required; backing out of it from the first
    # screen is a no-op (there's no earlier screen).
    screens: list[Callable[[], object]] = [
        lambda: _step1_provider(
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            model=model,
            non_interactive=non_interactive,
            warnings=warnings,
        ),
        lambda: _step2_sandbox(skip=skip_sandbox, non_interactive=non_interactive),
        lambda: _step3_channel(
            channel=channel, skip=skip_channel, non_interactive=non_interactive
        ),
        lambda: _step4_memory(
            skip=skip_memory,
            non_interactive=non_interactive,
            main_model=_load_current_default_model(),
            warnings=warnings,
        ),
    ]

    index = 0
    while index < len(screens):
        result = screens[index]()
        if result is _BACK:
            # Back never advances. On the first screen there's nowhere earlier
            # to go, so we re-display the same (required) screen rather than
            # skipping past it — skipping Step 1 would leave provider/model
            # unwritten and re-trip the startup gate into an infinite loop.
            index = max(0, index - 1)
        else:
            index += 1

    _print_next_steps(warnings=warnings)


# ---------------------------------------------------------------------------
# Startup gate — invoked by bare `raven` / `raven agent` / TUI entry points
# ---------------------------------------------------------------------------


def ensure_configured_or_onboard(*, non_interactive: bool = False) -> bool:
    """Run the wizard when the required config (provider + model) is missing.

    Returns ``True`` if config was already complete (caller proceeds straight
    to the session), ``False`` if the wizard ran (config is now populated). In
    a non-interactive context with missing config, the wizard's TTY check
    will raise — callers on non-TTY paths must guard before invoking.
    """
    if _is_config_populated():
        return True
    run_wizard(non_interactive=non_interactive)
    return False


# ---------------------------------------------------------------------------
# Typer entry point
# ---------------------------------------------------------------------------


def register(app: typer.Typer) -> None:
    """Attach the ``onboard`` command to ``app``."""

    @app.command()
    def onboard(
        provider: Optional[str] = typer.Option(
            None, "--provider", help="LLM provider name (skips Step 1's prompt)"
        ),
        api_key: Optional[str] = typer.Option(
            None, "--api-key", help="API key for the chosen provider"
        ),
        base_url: Optional[str] = typer.Option(
            None, "--base-url", help="Custom OpenAI-compatible base URL"
        ),
        model: Optional[str] = typer.Option(
            None, "--model", help="Default model id (e.g. 'openai/gpt-4o-mini')"
        ),
        channel: Optional[str] = typer.Option(
            None, "--channel", help="Channel to enable in Step 3"
        ),
        skip_sandbox: bool = typer.Option(
            False, "--skip-sandbox", help="Skip Step 2 (run location)"
        ),
        skip_channel: bool = typer.Option(
            False, "--skip-channel", help="Skip Step 3 (channel setup)"
        ),
        skip_memory: bool = typer.Option(
            False, "--skip-memory", help="Skip Step 4 (long-term memory)"
        ),
        non_interactive: bool = typer.Option(
            False,
            "--non-interactive",
            help="Run without prompts (requires flags for any missing field)",
        ),
        yes: bool = typer.Option(
            False, "--yes", "-y", help="Skip all confirm prompts"
        ),
        reset: bool = typer.Option(
            False, "--reset", help="Force re-run even if a config already exists"
        ),
    ) -> None:
        """Four-step setup wizard: LLM provider → sandbox → channel → memory."""
        run_wizard(
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            model=model,
            channel=channel,
            skip_sandbox=skip_sandbox,
            skip_channel=skip_channel,
            skip_memory=skip_memory,
            non_interactive=non_interactive,
            yes=yes,
            reset=reset,
        )


__all__ = ["register", "run_wizard", "ensure_configured_or_onboard"]
