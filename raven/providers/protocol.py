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


def effective_protocol(section: Any, model: str | None) -> ApiProtocol:
    """Return explicit model override, inferred vendor protocol, or fallback."""
    configured = configured_protocol(section, model)
    if configured is not None:
        return configured

    model_id = _model_id(model)
    if model_id.startswith(("claude", "glm")):
        return "anthropic"
    if model_id.startswith(("qwen", "seed", "doubao-seed", "minimax", "deepseek", "gpt", "kimi")):
        return "responses"
    return "chat"


def native_api_base(provider_name: str | None, protocol: ApiProtocol, explicit: str | None = None) -> str | None:
    """Resolve only addresses declared for the selected native protocol."""
    if explicit:
        return explicit
    from raven.providers.registry import find_by_name

    spec = find_by_name(provider_name) if provider_name else None
    if spec is None:
        return None
    base = dict(spec.native_api_bases).get(protocol)
    if spec.is_gateway or spec.is_local:
        base = base or spec.usable_default_api_base
    return base


__all__ = ["ALL_PROTOCOLS", "ApiProtocol", "configured_protocol", "effective_protocol", "normalize_protocol"]
