"""Prompt token estimation: a provider's own counter first, tiktoken as the fallback.

``tiktoken`` is imported here and nowhere else in ``raven.utils``, so a leaf that
wants a safe filename does not pay for an encoding table.
"""

import json
from typing import Any

import tiktoken

from raven.utils.images import estimate_content_part_tokens


def estimate_prompt_tokens(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> int:
    """Estimate prompt tokens with tiktoken."""
    parts: list[str] = []
    extra_tokens = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    txt = part.get("text", "")
                    if txt:
                        parts.append(txt)
                elif (image_tokens := estimate_content_part_tokens(part)) is not None:
                    extra_tokens += image_tokens
                else:
                    parts.append(json.dumps(part, ensure_ascii=False))
        elif content is not None:
            parts.append(json.dumps(content, ensure_ascii=False))

        for key in ("name", "tool_call_id"):
            value = msg.get(key)
            if isinstance(value, str) and value:
                parts.append(value)
        if msg.get("tool_calls"):
            parts.append(json.dumps(msg["tool_calls"], ensure_ascii=False))
        reasoning = msg.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            parts.append(reasoning)
        if msg.get("thinking_blocks"):
            parts.append(json.dumps(msg["thinking_blocks"], ensure_ascii=False))

    if tools:
        parts.append(json.dumps(tools, ensure_ascii=False))

    payload = "\n".join(parts)
    if not payload:
        return extra_tokens
    try:
        enc = tiktoken.get_encoding("cl100k_base")
        text_tokens = len(enc.encode(payload))
    except Exception:
        text_tokens = len(payload) // 4
    return max(1, text_tokens + extra_tokens)


def estimate_message_tokens(message: dict[str, Any]) -> int:
    """Estimate prompt tokens contributed by one persisted message."""
    content = message.get("content")
    parts: list[str] = []
    extra_tokens = 0
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text", "")
                if text:
                    parts.append(text)
            elif (image_tokens := estimate_content_part_tokens(part)) is not None:
                extra_tokens += image_tokens
            else:
                parts.append(json.dumps(part, ensure_ascii=False))
    elif content is not None:
        parts.append(json.dumps(content, ensure_ascii=False))

    for key in ("name", "tool_call_id"):
        value = message.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    if message.get("tool_calls"):
        parts.append(json.dumps(message["tool_calls"], ensure_ascii=False))
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        parts.append(reasoning)
    if message.get("thinking_blocks"):
        parts.append(json.dumps(message["thinking_blocks"], ensure_ascii=False))

    payload = "\n".join(parts)
    if not payload:
        return max(1, extra_tokens)
    try:
        enc = tiktoken.get_encoding("cl100k_base")
        text_tokens = len(enc.encode(payload))
    except Exception:
        text_tokens = len(payload) // 4
    return max(1, text_tokens + extra_tokens)


def estimate_prompt_tokens_chain(
    provider: Any,
    model: str | None,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> tuple[int, str]:
    """Estimate prompt tokens via provider counter first, then tiktoken fallback."""
    provider_counter = getattr(provider, "estimate_prompt_tokens", None)
    if callable(provider_counter):
        try:
            tokens, source = provider_counter(messages, tools, model)
            if isinstance(tokens, (int, float)) and tokens > 0:
                return int(tokens), str(source or "provider_counter")
        except Exception:
            pass

    estimated = estimate_prompt_tokens(messages, tools)
    if estimated > 0:
        return int(estimated), "tiktoken"
    return 0, "none"


__all__ = ["estimate_message_tokens", "estimate_prompt_tokens", "estimate_prompt_tokens_chain"]
