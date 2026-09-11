"""Resolve the wire protocol used by one configured model."""

from __future__ import annotations

from typing import Any, Literal, TypeAlias, cast

ApiProtocol: TypeAlias = Literal["chat", "responses", "anthropic"]
ALL_PROTOCOLS: tuple[ApiProtocol, ...] = ("chat", "responses", "anthropic")

_ALIASES: dict[str, ApiProtocol] = {
    "chat": "chat",
    "completion": "chat",
    "chat_completions": "chat",
    "responses": "responses",
    "response": "responses",
    "openai_response": "responses",
    "openai_responses": "responses",
    "anthropic": "anthropic",
    "messages": "anthropic",
    "anthropic_messages": "anthropic",
}


def normalize_protocol(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    key = value.strip().lower().replace("-", "_").replace(" ", "_")
    return _ALIASES.get(key, value)


def _model_id(model: str | None) -> str:
    return (model or "").rsplit("/", 1)[-1].lower()


def _same_model(left: str, right: str) -> bool:
    return left.lower() == right.lower() or _model_id(left) == _model_id(right)


def configured_protocol(section: Any, model: str | None) -> ApiProtocol | None:
    """Return the explicit protocol for this model, if configured."""

    def read(name: str, default: Any = None) -> Any:
        if isinstance(section, dict):
            return section.get(name, default)
        return getattr(section, name, default)

    overrides = read("model_protocols", {}) or {}
    if isinstance(overrides, dict) and model:
        for candidate, value in overrides.items():
            if _same_model(str(candidate), model):
                normalized = normalize_protocol(value)
                if normalized in ALL_PROTOCOLS:
                    return cast(ApiProtocol, normalized)

    configured = normalize_protocol(read("protocol"))
    if configured in ALL_PROTOCOLS:
        return cast(ApiProtocol, configured)

    return None


def inferred_protocol(model: str | None) -> ApiProtocol:
    """The protocol a model's family name suggests, before any provider is consulted.

    Only Anthropic's own family is inferred onto the Anthropic wire. ``glm`` was
    in that list too, and it is not an Anthropic-family model: on the Messages
    transport its reasoning effort had to be turned into a ``thinking``
    budget_tokens number, which the gateway serving it does not honour -- 42
    measured calls produced 261,980 characters of reasoning against a budget of
    1024 tokens. Over chat the effort travels as an effort and the vendor sizes
    it. A vendor with its own Anthropic-compatible address is still reachable by
    naming ``protocol`` explicitly; it is the guess that was wrong.
    """
    model_id = _model_id(model)
    if model_id.startswith("claude"):
        return "anthropic"
    if model_id.startswith(("qwen", "seed", "doubao-seed", "minimax", "deepseek", "gpt", "kimi")):
        return "responses"
    return "chat"


def configured_bases(section: Any) -> list[str | None]:
    """The address each request from ``section`` would carry: one per endpoint,
    else the flat one, else ``None``. The factory builds one client per entry
    of this list, so a protocol has to be servable for every entry, not for the
    first one that happens to carry an address."""
    if section is None:
        return [None]
    from raven.providers.endpoints import provider_endpoints

    endpoints = provider_endpoints(section)
    if endpoints:
        return [ep.api_base or None for ep in endpoints]
    flat = section.get("api_base") if isinstance(section, dict) else getattr(section, "api_base", None)
    return [flat or None]


def effective_protocol(section: Any, model: str | None, provider_name: str | None = None) -> ApiProtocol:
    """Return explicit model override, inferred vendor protocol, or fallback.

    A protocol the user chose is final: choosing ``responses`` for a provider
    with no address for it is refused downstream, as it should be. A protocol
    merely inferred from the model's family is not a choice, so when
    ``provider_name`` is known and not every endpoint of the section has an
    address for it, the inference steps aside and the model goes over chat --
    the route that served these models before the inference existed. Without a
    provider name the inference stands, which is what the protocol tables read.
    The factory and the model picker both resolve through here, off the same
    endpoint set, so what the picker shows is what the runtime builds.
    """
    configured = configured_protocol(section, model)
    if configured is not None:
        return configured

    inferred = inferred_protocol(model)
    if inferred == "chat" or provider_name is None:
        return inferred
    if all(native_api_base(provider_name, inferred, base) for base in configured_bases(section)):
        return inferred
    return "chat"


def native_api_base(provider_name: str | None, protocol: ApiProtocol, explicit: str | None = None) -> str | None:
    """Resolve only addresses declared for the selected native protocol.

    A gateway or local server that declares no per-protocol address serves
    every protocol at its one address (openrouter, custom), so that address
    answers for any of them. One that declares addresses for some protocols
    has said where they are served -- and, by omission, that the others are
    not: volcengine names a ``responses`` address only, and handing that
    address to an ``anthropic`` request sent ``/v1/messages`` to an endpoint
    that does not speak it.
    """
    if explicit:
        return explicit
    from raven.providers.registry import find_by_name

    spec = find_by_name(provider_name) if provider_name else None
    if spec is None:
        return None
    declared = dict(spec.native_api_bases)
    base = declared.get(protocol)
    if base is None and not declared and (spec.is_gateway or spec.is_local):
        base = spec.usable_default_api_base
    return base


__all__ = [
    "ALL_PROTOCOLS",
    "ApiProtocol",
    "configured_bases",
    "configured_protocol",
    "effective_protocol",
    "inferred_protocol",
    "native_api_base",
    "normalize_protocol",
]
