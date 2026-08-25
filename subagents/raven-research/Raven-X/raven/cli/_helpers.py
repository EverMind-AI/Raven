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
from typing import Any

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
    provider is built lazily.

    Raises ``MissingCredentialsError`` rather than printing and exiting: more
    than one entry point calls this, and only one of them is a terminal. Each
    renders the failure in its own idiom -- the CLI as a red line and exit 1,
    the TUI as an RPC error carrying the same sentence.

    What counts as configured is `providers.auth`, the same declaration routing
    and `provider list` consult. Deciding it here as well is what produced three
    verdicts on one config: a Gemini section holding only `api_key_list` read as
    configured in `provider list` and refused to start, and Azure with a key and
    no address was routed and displayed as configured yet rejected here.
    """
    from raven.providers.auth import MissingCredentialsError, credential_status
    from raven.providers.registry import find_by_model, split_model_id

    model = config.agents.defaults.model
    provider_name = config.get_provider_name(model)
    if not provider_name:
        # Routing found no configured section, so name the provider the model id
        # points at rather than reporting on nothing.
        spec = find_by_model(model)
        provider_name = spec.name if spec else split_model_id(model)[0]
    if not provider_name:
        raise MissingCredentialsError(
            "no provider configured",
            remedy=(
                "Run: raven provider set <name> --api-key <key>, then raven provider use <name>/<model>\n"
                "Or run `raven onboard` for guided setup."
            ),
        )

    status = credential_status(provider_name, config.providers.get(provider_name), include_external=True)
    if status.ok:
        return

    # A first run fails this check while naming a provider the user never chose:
    # with nothing configured, routing falls back to the schema's default model,
    # whose vendor then gets reported as the thing to go fix. Sending someone who
    # only has an OpenRouter key to `provider set anthropic` is the wrong errand,
    # so answer the wizard instead. Both halves are required -- a user who picked
    # this model, or who has some other provider working, gets the specific
    # verdict, which for the OAuth families names a sign-in rather than a key.
    # Names come from the declared fields *and* the extras: an undeclared
    # provider key is a supported shape, and `ProvidersConfig.get` is the only
    # place allowed to resolve either kind, so route both through it rather than
    # reading `__dict__` -- which sees no extras and would call a user whose one
    # working credential lives there unconfigured.
    chose_a_model = config.agents.defaults.model != type(config.agents.defaults)().model
    configured = (*config.providers.__dict__, *(config.providers.model_extra or {}))
    if not chose_a_model and not any(
        credential_status(name, config.providers.get(name), include_external=True).ok for name in configured
    ):
        raise MissingCredentialsError(
            "no provider is configured yet -- run `raven onboard` for guided setup",
            remedy="Already have a key? raven provider set <name> --api-key <key>",
        )

    raise MissingCredentialsError(
        status.summary,
        provider=provider_name,
        remedy="Run `raven onboard` for guided setup.",
    )


def make_provider(config: Config):
    """Create the appropriate LLM provider from config."""
    from raven.providers.auth import MissingCredentialsError
    from raven.providers.azure_openai_provider import AzureOpenAIProvider
    from raven.providers.base import GenerationSettings
    from raven.providers.openai_codex_provider import OpenAICodexProvider

    check_provider_credentials(config)

    model = config.agents.defaults.model
    provider_name = config.get_provider_name(model)
    p = config.get_provider(model)

    defaults = config.agents.defaults

    from raven.providers.registry import endpoints_unsupported_reason, find_by_name

    spec = find_by_name(provider_name) if provider_name else None
    client = spec.client if spec else ""

    if p and p.endpoints:
        reason = endpoints_unsupported_reason(provider_name)
        if reason:
            raise MissingCredentialsError(reason, provider=provider_name or "")

    if client == "codex":
        provider = OpenAICodexProvider(default_model=model)
    elif client == "minimax_oauth":
        from raven.providers.minimax_oauth_provider import MiniMaxOAuthProvider

        provider = MiniMaxOAuthProvider(
            region="global" if provider_name == "minimax_global" else "cn",
            default_model=model,
        )
    elif client == "azure":
        provider = AzureOpenAIProvider(
            api_key=p.effective_api_key,
            api_base=p.api_base,
            default_model=model,
            deployment=getattr(p, "deployment", "") or "",
            api_version=getattr(p, "api_version", "") or "2024-10-21",
        )
    else:
        from raven.providers.capabilities import wire_overrides
        from raven.providers.endpoints import provider_endpoints
        from raven.providers.litellm_provider import LiteLLMProvider

        # Per-model wire facts live in ``capabilities.wire_overrides`` -- no
        # model-family branch belongs in this factory. The reasoning-off
        # override only applies when the user did NOT pin reasoningEffort:
        # the workaround exists for configs that never asked for reasoning,
        # and letting it ride along with an explicit reasoningEffort would put
        # "enabled": false and the requested effort on the same wire request -
        # the benchmark workaround silently overriding the stated configuration.
        extra_body: dict[str, Any] = dict(wire_overrides(provider_name, model))
        if defaults.reasoning_effort and extra_body.get("reasoning") == {"enabled": False}:
            del extra_body["reasoning"]
        # ``providers.<name>.routing`` pins the OpenRouter upstream — see
        # ProviderConfig.routing for why. Merged into extra_body rather than
        # assigned over it: the wire overrides above and this one are
        # independent conditions on the same request body, and a config that
        # happened to satisfy both would otherwise get whichever branch ran
        # last.
        routing = getattr(p, "routing", None) if p else None
        if routing:
            if provider_name == "openrouter":
                extra_body["provider"] = routing
            else:
                console.print(
                    f"[yellow]Note: providers.{provider_name}.routing is OpenRouter-only "
                    f"and was ignored.[/yellow]"
                )

        eps = provider_endpoints(p) if p else []

        def _litellm(api_key, api_base, extra_headers):
            return LiteLLMProvider(
                api_key=api_key,
                # ``api_base`` already carries the section's flat address when
                # the endpoint named none of its own (see ``provider_endpoints``);
                # the fallback here is only for a gateway/local provider whose
                # *flat* address is also empty, where ``get_api_base`` still has
                # the spec's default to offer.
                api_base=api_base or config.get_api_base(model),
                default_model=model,
                extra_headers=extra_headers,
                provider_name=provider_name,
                extra_body=extra_body or None,
                model_overrides=defaults.model_overrides,
            )

        if len(eps) > 1:
            from raven.providers.endpoint_rotor import EndpointRotorProvider

            def make_inner(ep):
                return _litellm(ep.api_key, ep.api_base, ep.extra_headers)

            provider = EndpointRotorProvider(
                eps,
                make_inner,
                default_model=model,
                strategy=p.endpoint_strategy if p else "sticky",
            )
        elif eps:
            provider = _litellm(eps[0].api_key, eps[0].api_base, eps[0].extra_headers)
        else:
            # No config section for this provider at all -- ``provider_endpoints``
            # always synthesizes one entry from a section, so [] means p is None.
            provider = _litellm(None, None, None)

    provider.generation = GenerationSettings(
        temperature=defaults.temperature,
        max_tokens=defaults.max_tokens,
        reasoning_effort=defaults.reasoning_effort,
        repetition_penalty=defaults.repetition_penalty,
        timeout=defaults.request_timeout_seconds,
    )
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


def exit_memory_service_unavailable(error: Exception) -> "typer.Exit":
    """Render an opted-in memory-service failure and exit 1.

    ``require_service=true`` is a configuration statement -- "this run exists
    to write memory, so a memory service that cannot take writes is a failed
    run" -- so it belongs with the other exit-1 causes rather than surfacing as
    a bare traceback out of ``asyncio.run``.

    Returns the exception for the caller to ``raise``, so the failure is
    visible at the call site instead of hidden inside a helper.
    """
    console.print(f"\n[red]Error: {error}[/red]")
    console.print(
        "  [dim]·[/dim] Start the service, or set "
        "[cyan]require_service=false[/cyan] in the backend's plugin config to "
        "degrade instead of failing."
    )
    return typer.Exit(1)


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
