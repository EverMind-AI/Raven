"""Provider construction from config: the providers shelf's own factory.

Which client class a provider section maps to, how endpoints rotate, how
credentials are judged before anything is built -- this is knowledge the
shelf owns, so it lives here and the assembly root composes it. The pool
and the resolving provider reach it as a sibling import, never up into
raven.core.
"""

from __future__ import annotations

from raven.config.schema import Config


def check_provider_credentials(config: Config, model: str | None = None) -> None:
    """Fail-fast when the configured provider is missing required credentials.

    Cheap (no litellm import), so it can run at startup even when the real
    provider is built lazily.

    Raises ``MissingCredentialsError`` rather than printing and exiting: three entry
    points call this, and only one of them is a terminal. Each renders the
    failure in its own idiom -- the CLI as a red line and exit 1, the TUI as an
    RPC error carrying the same sentence.

    What counts as configured is `providers.auth`, the same declaration routing
    and `provider list` consult. Deciding it here as well is what produced three
    verdicts on one config: a Gemini section holding only `api_key_list` read as
    configured in `provider list` and refused to start, and Azure with a key and
    no address was routed and displayed as configured yet rejected here.
    """
    from raven.providers.auth import MissingCredentialsError, credential_status
    from raven.providers.registry import find_by_model, split_model_id

    model = model or config.agents.defaults.model
    provider_name = config.get_provider_name(model)
    if not provider_name:
        # Routing found no configured section, so name the provider the model id
        # points at rather than reporting on nothing.
        spec = find_by_model(model)
        provider_name = spec.name if spec else split_model_id(model)[0]
    if not provider_name:
        raise MissingCredentialsError(
            "no provider configured",
            # A command, not a config path: the CLI exists to hide the layout.
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


def make_provider(config: Config, model: str | None = None):
    """Create the appropriate LLM provider from config, for ``model`` (default:
    the configured agent default)."""
    from raven.providers.auth import MissingCredentialsError
    from raven.providers.azure_openai_provider import AzureOpenAIProvider
    from raven.providers.base import GenerationSettings
    from raven.providers.openai_codex_provider import OpenAICodexProvider

    model = model or config.agents.defaults.model
    check_provider_credentials(config, model)
    provider_name = config.get_provider_name(model)
    p = config.get_provider(model)

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

        eps = provider_endpoints(p) if p else []
        if len(eps) > 1:
            from raven.providers.endpoint_rotor import EndpointRotorProvider

            def make_inner(ep):
                return LiteLLMProvider(
                    api_key=ep.api_key,
                    # ``ep.api_base`` already carries the section's flat address
                    # when the endpoint named none of its own (see
                    # ``provider_endpoints``); the fallback here is only for a
                    # gateway/local provider whose *flat* address is also empty,
                    # where ``get_api_base`` still has the spec's default to
                    # offer.
                    api_base=ep.api_base or config.get_api_base(model),
                    default_model=model,
                    extra_headers=ep.extra_headers,
                    provider_name=provider_name,
                    extra_body=wire_overrides(provider_name, model) or None,
                    model_overrides=config.agents.defaults.model_overrides,
                )

            provider = EndpointRotorProvider(
                eps,
                make_inner,
                default_model=model,
                strategy=p.endpoint_strategy if p else "sticky",
            )
        elif eps:
            extra_body = wire_overrides(provider_name, model) or None
            provider = LiteLLMProvider(
                api_key=eps[0].api_key,
                # Same fallback as ``make_inner`` above: only reached when the
                # flat address is empty too, for a gateway/local provider's
                # spec default.
                api_base=eps[0].api_base or config.get_api_base(model),
                default_model=model,
                extra_headers=eps[0].extra_headers,
                provider_name=provider_name,
                extra_body=extra_body,
                model_overrides=config.agents.defaults.model_overrides,
            )
        else:
            extra_body = wire_overrides(provider_name, model) or None
            provider = LiteLLMProvider(
                api_key=p.effective_api_key if p else None,
                api_base=config.get_api_base(model),
                default_model=model,
                extra_headers=p.extra_headers if p else None,
                provider_name=provider_name,
                extra_body=extra_body,
                model_overrides=config.agents.defaults.model_overrides,
            )

    defaults = config.agents.defaults
    provider.generation = GenerationSettings(
        temperature=defaults.temperature,
        reasoning_effort=defaults.reasoning_effort,
        timeout=defaults.llm_call_timeout,
    )
    return provider


def make_lazy_provider(config: Config):
    """Provider that defers the real (litellm-importing) build to the first model
    call, so AgentLoop construction stays fast. Credentials are checked now
    (fail-fast preserved) and the real provider is pre-warmed in the background."""
    from raven.providers.base import GenerationSettings
    from raven.providers.endpoints import provider_endpoints
    from raven.providers.lazy import LazyProvider

    check_provider_credentials(config)
    defaults = config.agents.defaults

    p = config.get_provider(defaults.model)
    eps = provider_endpoints(p) if p else []
    initial_endpoint_label = eps[0].label if len(eps) > 1 else None

    provider = LazyProvider(
        factory=lambda: make_provider(config),
        default_model=defaults.model,
        generation=GenerationSettings(
            temperature=defaults.temperature,
            reasoning_effort=defaults.reasoning_effort,
            timeout=defaults.llm_call_timeout,
        ),
        initial_endpoint_label=initial_endpoint_label,
    )
    provider.prewarm()
    return provider


def make_resolving_provider(config: Config, config_supplier=None):
    """Provider that resolves each call's vendor from its model name. Used by the
    gateway, where different sessions can be on different vendors at once.
    ``config_supplier`` makes its per-vendor credentials live -- see
    ``ResolvingProvider._refresh_credentials``."""
    from raven.providers.resolving_provider import ResolvingProvider

    check_provider_credentials(config)
    return ResolvingProvider(config, config_supplier=config_supplier)


__all__ = ["check_provider_credentials", "make_lazy_provider", "make_provider", "make_resolving_provider"]
