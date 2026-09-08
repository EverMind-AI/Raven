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


def effective_protocol(section: Any, model: str | None) -> ApiProtocol:
    """Return explicit model override, inferred vendor protocol, or fallback."""
    if section is None:
        return "chat"

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

    model_id = _model_id(model)
    if "gpt" in model_id:
        return "responses"
    if "claude" in model_id:
        return "anthropic"
    return "chat"


__all__ = ["ALL_PROTOCOLS", "ApiProtocol", "effective_protocol", "normalize_protocol"]
