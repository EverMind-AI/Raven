"""Shared CLI helpers used by multiple top-level command modules.

Extracted from commands.py so that per-command modules
(``agent_commands.py``, ``gateway_commands.py``, ``skill_commands.py``,
``sentinel_commands.py``) can import them directly instead of going
through lazy wrappers.

Function names drop the leading underscore: the file itself is marked
internal with the ``_helpers`` prefix, so members do not also need the
private-name convention.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import typer
from rich.console import Console

from raven.config.schema import Config

console = Console()


DEFAULT_PROBE_MESSAGE = "Hi! Say hello in one sentence."


def warn_about_pending_cli_reminders(cron_service, config: Config) -> None:
    """At REPL exit, list cron jobs pinned to channel="cli" that won't fire
    while the REPL is down. Hint at the config knob that forwards them to
    a durable channel at trigger time."""
    from datetime import datetime

    try:
        jobs = cron_service.list_jobs()
    except Exception:
        return
    now_ms = int(datetime.now().timestamp() * 1000)
    pending = [
        j
        for j in jobs
        if (j.payload.channel or "") == "cli" and j.state.next_run_at_ms and j.state.next_run_at_ms > now_ms
    ]
    if not pending:
        return

    console.print(f"\n[yellow]⚠  You have {len(pending)} pending CLI reminder(s):[/yellow]")
    for j in pending:
        fire = datetime.fromtimestamp(j.state.next_run_at_ms / 1000).strftime("%H:%M")
        mins = max(0, (j.state.next_run_at_ms - now_ms) // 60_000)
        console.print(f"   - '{j.name}' at {fire} (in {mins} min)")

    if config.cron.forward_channels == []:
        console.print(
            "[dim]   Tip: cron.forward_channels is empty — these reminders will "
            "be dropped silently when they fire. Run "
            "`raven cron config set forward_channels '*'` to broadcast to "
            "all enabled channels.[/dim]"
        )


def check_provider_credentials(config: Config) -> None:
    """Fail-fast when the configured provider is missing required credentials.

    Cheap (no litellm import), so it can run at startup even when the real
    provider is built lazily. Kept in sync with the branches of make_provider.
    """
    model = config.agents.defaults.model
    provider_name = config.get_provider_name(model)
    p = config.get_provider(model)

    if provider_name == "openai_codex" or model.startswith("openai-codex/"):
        return
    if provider_name == "azure_openai":
        if not p or not p.api_key or not p.api_base:
            console.print("[red]Error: Azure OpenAI requires api_key and api_base.[/red]")
            console.print("Set them in ~/.raven/config.json under providers.azure_openai section")
            console.print("Use the model field to specify the deployment name.")
            raise typer.Exit(1)
        return
    from raven.providers.registry import find_by_name

    spec = find_by_name(provider_name)
    if not model.startswith("bedrock/") and not (p and p.api_key) and not (spec and (spec.is_oauth or spec.is_local)):
        console.print("[red]Error: No API key configured.[/red]")
        console.print("Set one in ~/.raven/config.json under providers section")
        raise typer.Exit(1)


def make_provider(config: Config):
    """Create the appropriate LLM provider from config."""
    from raven.providers.azure_openai_provider import AzureOpenAIProvider
    from raven.providers.base import GenerationSettings
    from raven.providers.openai_codex_provider import OpenAICodexProvider

    check_provider_credentials(config)

    model = config.agents.defaults.model
    provider_name = config.get_provider_name(model)
    p = config.get_provider(model)

    if provider_name == "openai_codex" or model.startswith("openai-codex/"):
        provider = OpenAICodexProvider(default_model=model)
    elif provider_name == "azure_openai":
        provider = AzureOpenAIProvider(
            api_key=p.api_key,
            api_base=p.api_base,
            default_model=model,
        )
    else:
        from raven.providers.litellm_provider import LiteLLMProvider

        # OpenRouter routes qwen3.x-27B through providers that default to
        # reasoning mode (e.g. AtlasCloud): every chat completion emits
        # ~800 chain-of-thought tokens and takes ~30s wall — fatal for
        # interactive use and for high-volume benchmark runs. The
        # ``reasoning.enabled=false`` flag is OpenRouter-specific and
        # forwards through LiteLLM's ``extra_body``.
        extra_body = None
        if provider_name == "openrouter" and "qwen" in (model or "").lower():
            extra_body = {"reasoning": {"enabled": False}}
        provider = LiteLLMProvider(
            api_key=p.api_key if p else None,
            api_base=config.get_api_base(model),
            default_model=model,
            extra_headers=p.extra_headers if p else None,
            provider_name=provider_name,
            extra_body=extra_body,
        )

    defaults = config.agents.defaults
    provider.generation = GenerationSettings(
        temperature=defaults.temperature,
        max_tokens=defaults.max_tokens,
        reasoning_effort=defaults.reasoning_effort,
    )
    return provider


def make_lazy_provider(config: Config):
    """Provider that defers the real (litellm-importing) build to the first model
    call, so AgentLoop construction stays fast. Credentials are checked now
    (fail-fast preserved) and the real provider is pre-warmed in the background."""
    from raven.providers.base import GenerationSettings
    from raven.providers.lazy import LazyProvider

    check_provider_credentials(config)
    defaults = config.agents.defaults
    provider = LazyProvider(
        factory=lambda: make_provider(config),
        default_model=defaults.model,
        generation=GenerationSettings(
            temperature=defaults.temperature,
            max_tokens=defaults.max_tokens,
            reasoning_effort=defaults.reasoning_effort,
        ),
    )
    provider.prewarm()
    return provider


def send_probe(
    *,
    message: str = DEFAULT_PROBE_MESSAGE,
    timeout_s: int = 15,
    max_tokens: int = 200,
) -> tuple[str, int | None, float]:
    """Build provider from current config and exchange one chat message.

    Shared by ``onboard`` Step 3 and ``doctor --probe``. Bypasses the full
    ``AgentLoop`` so the probe only proves the provider answers, not that
    the agent runtime is healthy.

    Returns ``(response_text, tokens_used, elapsed_s)``. Raises ``RuntimeError``
    on provider error, ``asyncio.TimeoutError`` on timeout, or whatever
    ``load_config`` / ``make_provider`` raise on config failure.
    """
    from raven.config.loader import load_config

    config = load_config()
    provider = make_provider(config)

    start = time.monotonic()
    response = asyncio.run(
        asyncio.wait_for(
            provider.chat_with_retry(
                messages=[{"role": "user", "content": message}],
                max_tokens=max_tokens,
                temperature=0.3,
            ),
            timeout=timeout_s,
        )
    )
    elapsed = time.monotonic() - start

    if response.finish_reason == "error":
        raise RuntimeError(response.content or "provider returned an error")

    usage = response.usage or {}
    tokens = usage.get("total_tokens") or usage.get("completion_tokens")
    return (response.content or "").strip(), tokens, elapsed


def print_probe_troubleshooting(provider: str | None) -> None:
    """Common-case hints when a probe fails.

    Shared by ``onboard`` Step 3 and ``doctor --probe`` so the diagnostic
    advice stays in one place.
    """
    console.print("\n  [dim]Troubleshooting:[/dim]")
    if provider:
        console.print(
            f"  [dim]·[/dim] [cyan]raven provider test {provider}[/cyan] — re-check credentials without spending tokens"
        )
        console.print(
            f"  [dim]·[/dim] [cyan]raven provider get {provider}[/cyan] — inspect what's actually stored on disk"
        )
    console.print(
        "  [dim]·[/dim] Check the model id in [cyan]~/.raven/config.json[/cyan] "
        "under [cyan]agents.defaults.model[/cyan] — it should match a model the "
        "provider serves."
    )


def load_runtime_config(config: str | None = None, workspace: str | None = None) -> Config:
    """Load config and optionally override the active workspace."""
    from raven.config.loader import load_config, set_config_path

    config_path = None
    if config:
        config_path = Path(config).expanduser().resolve()
        if not config_path.exists():
            console.print(f"[red]Error: Config file not found: {config_path}[/red]")
            raise typer.Exit(1)
        set_config_path(config_path)
        Console(stderr=True).print(f"[dim]Using config: {config_path}[/dim]")

    loaded = load_config(config_path)
    if workspace:
        loaded.agents.defaults.workspace = workspace
    return loaded


def parse_fake_now(fake_now: str | None):
    """Parse an ISO-8601 timestamp into a frozen ``now_fn`` callable.

    Used by the eval harness to drive the Sentinel stack at a deterministic
    wall-clock time via subprocess invocation. The returned callable always
    returns the same parsed datetime, so every component that reads "now"
    through ``now_fn`` sees the same snapshot for the duration of the call.

    Returns ``None`` when the flag is not set, in which case constructors
    fall through to their default ``datetime.now`` behavior.
    """
    if fake_now is None:
        return None
    from datetime import datetime as _dt

    try:
        frozen = _dt.fromisoformat(fake_now)
    except ValueError as exc:
        raise typer.BadParameter(
            f"--fake-now must be an ISO-8601 timestamp (e.g. 2026-05-13T09:00:00); got {fake_now!r}: {exc}"
        ) from exc
    return lambda: frozen


def print_deprecated_memory_window_notice(config: Config) -> None:
    """Warn when running with old memoryWindow-only config."""
    if config.agents.defaults.should_warn_deprecated_memory_window:
        console.print(
            "[yellow]Hint:[/yellow] Detected deprecated `memoryWindow` without "
            "`contextWindowTokens`. `memoryWindow` is ignored; run "
            "[cyan]raven onboard[/cyan] to refresh your config template."
        )


__all__ = [
    "DEFAULT_PROBE_MESSAGE",
    "warn_about_pending_cli_reminders",
    "make_provider",
    "send_probe",
    "print_probe_troubleshooting",
    "load_runtime_config",
    "parse_fake_now",
    "print_deprecated_memory_window_notice",
]
