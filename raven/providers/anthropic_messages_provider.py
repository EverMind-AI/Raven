"""API-key transport for Anthropic Messages-compatible endpoints.

The transport deliberately speaks the native ``/v1/messages`` wire instead of
asking LiteLLM to translate an OpenAI Chat request.  That keeps Claude tool
use, prompt-cache markers, extended thinking blocks, and OpenRouter's native
Anthropic route intact.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any

import httpx
import json_repair
from loguru import logger

from raven.providers.base import (
    ChatDelta,
    LLMProvider,
    LLMResponse,
    ProviderHTTPError,
    RunMeta,
    ToolCallRequest,
    format_llm_error,
    send_max_tokens,
)
from raven.providers.first_byte import (
    BOUND_NAME,
    FirstByteTimeoutError,
    httpx_timeout,
    stream_first_byte_budget,
)
from raven.providers.prompt_cache import accepts_cache_control
from raven.providers.rates import DEFAULT_MAX_OUTPUT_TOKENS
from raven.providers.tool_names import normalized_tool_name
from raven.providers.usage import reported_cost, token_count

_DEFAULT_BASE = "https://api.anthropic.com"
_ANTHROPIC_VERSION = "2023-06-01"
# The Messages API requires ``max_tokens``. The number a request carries is
# decided by the one owner every path shares, ``send_max_tokens``
# (``rates.DEFAULT_MAX_OUTPUT_TOKENS``, lowered to what the model's catalogue
# row declares when it declares less); the previous 4096 here cut a page's
# HTML on every write. A
# model whose real ceiling is below what the catalogue said names it in a 400;
# `clamp_to_model_limit` retries at that number and the provider remembers it.
# How much of that ceiling thinking may take is nobody's number here: the effort
# goes out as the label the caller named and whoever serves the model sizes it
# (OpenRouter's `reasoning.effort` is documented as a share of ``max_tokens``;
# Anthropic's own adaptive models read `output_config.effort`). The table that
# used to sit here was written against a 16384 ceiling -- ``high: 8192`` was half
# of it -- and outlived that ceiling by an order of magnitude. A number is
# reached for only when a vendor refuses the label (`rewrite_on_400`), and it
# comes from litellm rather than from us.
_MODEL_LIMIT_RE = re.compile(r"max_tokens: (\d+) > (\d+)")
# Claude 4.7 and later think adaptively: ``thinking.type: "enabled"`` with a
# budget is a 400 there, and depth is ``output_config.effort``. Earlier models
# are the reverse. Decided from the model id, and corrected from the 400 when
# the id says nothing (`rewrite_on_400`).
_CLAUDE_FAMILY_RE = re.compile(r"claude-(opus|sonnet|haiku|fable|mythos)-(\d+)(?:[.-](\d+))?")
_ADAPTIVE_EFFORTS = {"minimal": "low", "low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "max"}
_TOOL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DATA_URL_RE = re.compile(r"^data:([^;,]+);base64,(.+)$", re.DOTALL)

_FINISH_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "pause_turn": "stop",
}


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def messages_url(api_base: str | None) -> str:
    """Resolve a configured base to the native Messages endpoint."""
    base = (api_base or _DEFAULT_BASE).rstrip("/")
    if base.endswith("/messages"):
        return base
    if base.endswith("/v1"):
        return f"{base}/messages"
    if base.endswith("/api"):
        return f"{base}/v1/messages"
    return f"{base}/v1/messages"


def _is_openrouter(url: str) -> bool:
    return "openrouter.ai" in url.lower()


def _merge_headers(defaults: dict[str, str], overrides: dict[str, str] | None) -> dict[str, str]:
    """Merge headers case-insensitively so auth cannot be duplicated."""
    result = dict(defaults)
    for key, value in (overrides or {}).items():
        for existing in list(result):
            if existing.lower() == key.lower():
                result.pop(existing)
        result[key] = value
    return result


def build_headers(
    api_key: str,
    url: str,
    extra_headers: dict[str, str] | None = None,
    *,
    stream: bool = False,
) -> dict[str, str]:
    """Build native Anthropic headers for direct and OpenRouter routes."""
    use_bearer = _is_openrouter(url) or api_key.startswith("sk-or-")
    auth = {"Authorization": f"Bearer {api_key}"} if use_bearer else {"x-api-key": api_key}
    defaults = {
        "anthropic-version": _ANTHROPIC_VERSION,
        "content-type": "application/json",
        "accept": "text/event-stream" if stream else "application/json",
        **auth,
    }
    return _merge_headers(defaults, extra_headers)


def _safe_tool_id(value: Any, mapping: dict[str, str]) -> str:
    raw = str(value or "toolu_unknown")
    if raw in mapping:
        return mapping[raw]
    if _TOOL_ID_RE.fullmatch(raw):
        mapping[raw] = raw
        return raw
    mapping[raw] = f"toolu_{hashlib.sha256(raw.encode()).hexdigest()[:24]}"
    return mapping[raw]


def _parse_json_object(raw: Any) -> tuple[dict[str, Any], bool]:
    if isinstance(raw, dict):
        return raw, False
    if raw in (None, ""):
        return {}, False
    try:
        parsed = json.loads(raw)
        repaired = False
    except Exception:
        try:
            parsed = json_repair.loads(raw)
        except Exception:
            return {"_raw_arguments": str(raw)}, True
        repaired = True
    return (parsed, repaired) if isinstance(parsed, dict) else ({"_raw_arguments": raw}, True)


def _image_block(value: dict[str, Any]) -> dict[str, Any] | None:
    image = value.get("image_url")
    if isinstance(image, dict):
        url = image.get("url")
    else:
        url = image
    if not isinstance(url, str) or not url:
        return None
    match = _DATA_URL_RE.match(url)
    if match:
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": match.group(1), "data": match.group(2)},
        }
    return {"type": "image", "source": {"type": "url", "url": url}}


def _text_block(text: Any, source: dict[str, Any] | None = None) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "text", "text": str(text or "")}
    if source and source.get("cache_control"):
        block["cache_control"] = deepcopy(source["cache_control"])
    return block


def _content_blocks(content: Any, *, placeholder: bool = True) -> list[dict[str, Any]]:
    """Convert Raven's OpenAI-shaped content parts to Anthropic blocks.

    ``placeholder=False`` returns an empty list for content that renders to
    nothing instead of a "(empty)" text block: an assistant turn that only
    called a tool must render the same whether or not a cache breakpoint was
    attached to its (empty) text, or the prefix changes when the mark moves on.
    """
    if isinstance(content, str):
        if not content and not placeholder:
            return []
        return [_text_block(content or "(empty)")]
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return [_text_block(json.dumps(content, ensure_ascii=False))]

    blocks: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            blocks.append(_text_block(item))
            continue
        kind = item.get("type")
        if kind in {"text", "input_text", "output_text"}:
            text = item.get("text")
            if text:
                blocks.append(_text_block(text, item))
            continue
        if kind == "image_url":
            image = _image_block(item)
            if image:
                blocks.append(image)
            continue
        if kind in {"image", "thinking", "redacted_thinking", "tool_use", "tool_result"}:
            # These can already be native blocks when a session is resumed.
            blocks.append(deepcopy(item))
            continue
        blocks.append(_text_block(json.dumps(item, ensure_ascii=False)))
    if blocks or not placeholder:
        return blocks
    return [_text_block("(empty)")]


def _tool_definitions(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for tool in tools:
        fn = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        item: dict[str, Any] = {
            "name": fn["name"],
            "description": fn.get("description") or "",
            "input_schema": fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {"type": "object"},
        }
        if tool.get("cache_control"):
            item["cache_control"] = deepcopy(tool["cache_control"])
        converted.append(item)
    return converted


def _tool_choice(choice: str | dict[str, Any] | None) -> dict[str, Any] | None:
    if choice is None:
        return None
    if isinstance(choice, str):
        if choice == "required":
            return {"type": "any"}
        if choice == "auto":
            return {"type": choice}
        if choice == "none":
            return None
        return None
    fn = choice.get("function") if choice.get("type") == "function" else choice
    if isinstance(fn, dict) and fn.get("name"):
        return {"type": "tool", "name": fn["name"]}
    return None


def _assistant_message(message: dict[str, Any], ids: dict[str, str]) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []
    for block in message.get("thinking_blocks") or []:
        if not isinstance(block, dict):
            continue
        # Only what the API signed goes back. A stream cut at the output ceiling
        # ends inside the thinking and leaves the block unsigned; replayed, the
        # API refuses it as a modified thinking block and the turn dies.
        if block.get("type") == "thinking" and block.get("signature"):
            blocks.append(deepcopy(block))
        elif block.get("type") == "redacted_thinking" and block.get("data"):
            blocks.append(deepcopy(block))
    content = message.get("content")
    if content not in (None, "", []):
        blocks.extend(_content_blocks(content, placeholder=False))
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") or {}
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        arguments, _ = _parse_json_object(fn.get("arguments"))
        blocks.append(
            {
                "type": "tool_use",
                "id": _safe_tool_id(call.get("id"), ids),
                "name": fn["name"],
                "input": arguments,
            }
        )
    return {"role": "assistant", "content": blocks or [_text_block("(empty)")]}


def _tool_result_message(message: dict[str, Any], ids: dict[str, str]) -> dict[str, Any]:
    content = _content_blocks(message.get("content"))
    # Anthropic accepts a string or a block list for tool_result content. A list
    # preserves images and cache metadata without a lossy JSON round-trip.
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": _safe_tool_id(message.get("tool_call_id"), ids),
                "content": content,
            }
        ],
    }


def _source_cache_marker(message: dict[str, Any]) -> dict[str, Any] | None:
    """The breakpoint a Raven message carries, wherever the strategy put it.

    ``CacheOptimizer`` marks the last content block; older callers mark the
    message itself. Either means "a breakpoint ends here".
    """
    marker = message.get("cache_control")
    content = message.get("content")
    if not marker and isinstance(content, list):
        for block in reversed(content):
            if isinstance(block, dict) and block.get("cache_control"):
                marker = block["cache_control"]
                break
    return deepcopy(marker) if marker else None


def _strip_nested_markers(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for block in blocks:
        item = {key: value for key, value in block.items() if key != "cache_control"}
        if block.get("type") == "tool_result" and isinstance(block.get("content"), list):
            item["content"] = [
                {key: value for key, value in inner.items() if key != "cache_control"}
                if isinstance(inner, dict)
                else inner
                for inner in block["content"]
            ]
        cleaned.append(item)
    return cleaned


def _attach_message_cache_marker(item: dict[str, Any], message: dict[str, Any]) -> dict[str, Any]:
    """Put the message's breakpoint on the last block Anthropic will see.

    The marked source block does not always survive conversion: an assistant
    turn that only called a tool has an empty text block, which is dropped, and
    a tool result is wrapped in a ``tool_result`` block whose nested content is
    not a breakpoint position. Either way the breakpoint has to end up on the
    last top-level block, or the whole tail of the conversation goes uncached.
    """
    marker = _source_cache_marker(message)
    content = item.get("content")
    if not marker or not isinstance(content, list) or not content:
        return item
    updated = _strip_nested_markers(content)
    if isinstance(updated[-1], dict):
        updated[-1] = {**updated[-1], "cache_control": marker}
    return {**item, "content": updated}


def convert_messages(messages: list[dict[str, Any]]) -> tuple[str | list[dict[str, Any]] | None, list[dict[str, Any]]]:
    """Convert Raven history to native Anthropic Messages history."""
    system_blocks: list[dict[str, Any]] = []
    converted: list[dict[str, Any]] = []
    ids: dict[str, str] = {}

    for message in messages:
        role = message.get("role")
        if role == "system":
            blocks = _content_blocks(message.get("content"))
            if message.get("cache_control") and blocks:
                blocks[-1] = {**blocks[-1], "cache_control": deepcopy(message["cache_control"])}
            system_blocks.extend(blocks)
            continue
        if role == "assistant":
            item = _assistant_message(message, ids)
        elif role == "tool":
            item = _tool_result_message(message, ids)
        else:
            item = {"role": "user", "content": _content_blocks(message.get("content"))}
        item = _attach_message_cache_marker(item, message)

        # Anthropic requires alternating user/assistant turns. Merging adjacent
        # same-role entries also keeps resumed histories valid after compaction.
        if converted and converted[-1]["role"] == item["role"]:
            previous = converted[-1].setdefault("content", [])
            previous.extend(item["content"])
        else:
            converted.append(item)

    if not converted:
        converted = [{"role": "user", "content": [_text_block("(empty)")]}]
    elif converted[0]["role"] != "user":
        converted.insert(0, {"role": "user", "content": [_text_block("(empty)")]})
    if converted[-1]["role"] == "assistant":
        # A note a hook appended after the last exchange arrives as a trailing
        # assistant message. OpenAI-compatible endpoints read that as prefill;
        # Anthropic requires the conversation to end on the user side and the
        # current claude models refuse prefill outright, so the note travels as
        # user-side text instead. Only text can cross: a trailing tool_use has
        # no result yet and a thinking block cannot be re-sent unsigned.
        tail = converted.pop()
        text = [block for block in tail.get("content") or [] if block.get("type") == "text"] or [
            _text_block("(continue)")
        ]
        if converted and converted[-1]["role"] == "user":
            converted[-1].setdefault("content", []).extend(text)
        else:
            converted.append({"role": "user", "content": text})

    if not system_blocks:
        system: str | list[dict[str, Any]] | None = None
    elif all(set(block) <= {"type", "text"} for block in system_blocks):
        system = "\n".join(str(block["text"]) for block in system_blocks)
    else:
        system = system_blocks
    return system, converted


def _visible_reserve(max_tokens: int) -> int:
    return max(1024, max_tokens // 2)


def _litellm_budgets() -> dict[str, int]:
    """litellm's own effort-to-budget table, or empty when it cannot be imported.

    Read lazily: this is the refusal path only (a vendor that rejects an effort
    label and demands a token count), and importing litellm costs seconds.
    """
    try:
        from raven.providers.litellm_setup import import_litellm

        constants = import_litellm().constants
    except Exception:  # noqa: BLE001 - a repair must not become the failure
        return {}
    budgets = {}
    for effort in ("minimal", "low", "medium", "high", "xhigh", "max"):
        value = getattr(constants, f"DEFAULT_REASONING_EFFORT_{effort.upper()}_THINKING_BUDGET", None)
        if isinstance(value, int) and value > 0:
            budgets[effort] = value
    return budgets


def _thinking_budget(max_tokens: int, effort: str | None) -> int | None:
    """A token budget for an effort, for the one vendor shape that takes no label."""
    if not effort or effort.lower() in {"none", "off", "disabled"}:
        return None
    budgets = _litellm_budgets()
    if not budgets:
        return None
    asked = budgets.get(effort.lower(), budgets.get("medium", 0))
    # The budget is part of ``max_tokens``, so a budget sized to the ceiling
    # leaves a turn returning nothing but thinking -- which the loop reads as an
    # empty turn. Thinking gets at most half the ceiling; the rest is visible.
    budget = min(asked, max_tokens - _visible_reserve(max_tokens))
    # Anthropic's floor for a budget; below it the ceiling is too small to think in.
    return budget if budget >= 1024 else None


def adaptive_thinking(model: str) -> bool:
    match = _CLAUDE_FAMILY_RE.search((model or "").rsplit("/", 1)[-1].lower())
    if match is None:
        return False
    family, major, minor = match.group(1), int(match.group(2)), int(match.group(3) or 0)
    return family in {"fable", "mythos"} or major >= 5 or (major == 4 and minor >= 7)


def clamp_to_model_limit(body: dict[str, Any], error_text: str) -> bool:
    """Lower ``max_tokens`` to the ceiling a 400 named; True when the body changed."""
    match = _MODEL_LIMIT_RE.search(error_text or "")
    if match is None:
        return False
    limit = int(match.group(2))
    if limit <= 0 or limit >= int(body.get("max_tokens") or 0):
        return False
    body["max_tokens"] = limit
    thinking = body.get("thinking")
    if isinstance(thinking, dict) and "budget_tokens" in thinking:
        budget = min(int(thinking.get("budget_tokens") or 0), limit - _visible_reserve(limit))
        if budget >= 1024:
            thinking["budget_tokens"] = budget
        else:
            body.pop("thinking", None)
    return True


def rewrite_on_400(body: dict[str, Any], error_text: str) -> str | None:
    """Repair the one thing a 400 complained about; the repair's name when a retry is worth it.

    Five complaints are known: a ceiling above the model's (``"ceiling"``), a
    budgeted thinking request to a model that thinks adaptively or the reverse
    (``"thinking"``), an effort level the model does not offer (``"effort"``),
    and a temperature beside thinking (``"temperature"``). The name matters to
    the caller: only a ceiling complaint teaches the model's ceiling, the others
    leave ``max_tokens`` as the caller pinned it and must not be remembered as one.

    This is the only place a reasoning budget becomes a number, and the number is
    litellm's: a vendor that refuses an effort label has to be answered in tokens,
    and answering it with a table of our own is what this repair replaced.
    """
    text = error_text or ""
    if clamp_to_model_limit(body, text):
        return "ceiling"
    if isinstance(body.get("reasoning"), dict) and "reasoning" in text:
        effort = str((body.pop("reasoning") or {}).get("effort") or "high")
        budget = _thinking_budget(int(body.get("max_tokens") or DEFAULT_MAX_OUTPUT_TOKENS), effort)
        if budget is not None:
            body["thinking"] = {"type": "enabled", "budget_tokens": budget}
            body.pop("temperature", None)
        return "thinking"
    thinking = body.get("thinking")
    if isinstance(thinking, dict) and "not supported" in text:
        if thinking.get("type") == "enabled" and "enabled" in text:
            effort = str((body.get("output_config") or {}).get("effort") or "high")
            body["thinking"] = {"type": "adaptive"}
            body["output_config"] = {"effort": effort}
            return "thinking"
        if thinking.get("type") == "adaptive" and "adaptive" in text:
            effort = str((body.pop("output_config", None) or {}).get("effort") or "high")
            budget = _thinking_budget(int(body.get("max_tokens") or DEFAULT_MAX_OUTPUT_TOKENS), effort)
            if budget is None:
                body.pop("thinking", None)
            else:
                body["thinking"] = {"type": "enabled", "budget_tokens": budget}
            return "thinking"
    if "effort" in text and body.pop("output_config", None) is not None:
        return "effort"
    if "temperature" in text and body.pop("temperature", None) is not None:
        return "temperature"
    return None


def thinking_request(model: str, reasoning_effort: str | None) -> dict[str, Any]:
    """The reasoning keys a request carries for this model and effort.

    One owner for the shape, so the record of what was asked for cannot drift
    from what was sent. Two shapes, both of them a label:

    - a Claude that thinks adaptively reads ``output_config.effort``, beside
      ``thinking.type: "adaptive"`` (Anthropic's own newer form);
    - everything else on this wire gets OpenRouter's unified ``reasoning.effort``,
      which the gateway translates into whatever the upstream wants.

    A vendor that takes neither says so in a 400, and `rewrite_on_400` is where
    a token count is reached for -- never here.
    """
    if not reasoning_effort or reasoning_effort.lower() in {"none", "off", "disabled"}:
        return {}
    effort = _ADAPTIVE_EFFORTS.get(reasoning_effort.lower(), "high")
    if adaptive_thinking(model):
        return {"thinking": {"type": "adaptive"}, "output_config": {"effort": effort}}
    return {"reasoning": {"effort": effort}}


def generation_keys(model: str, temperature: float | None, reasoning_effort: str | None) -> dict[str, Any]:
    """Reasoning and temperature, as one request carries them.

    Called by the body builder and by ``request_generation``, which is what
    keeps the record of a call's parameters from drifting away from the request:
    whether a temperature survives depends on which reasoning shape went out,
    and that decision has to be made once.
    """
    keys = thinking_request(model, reasoning_effort)
    if temperature is not None and "thinking" not in keys and not adaptive_thinking(model):
        # Claude 4.7 and later think adaptively by default, and Anthropic's
        # thinking request accepts no temperature but the default -- so the
        # repository's own temperature would be refused beside one. An effort
        # sent as a label carries no ``thinking`` key, and keeps its temperature.
        keys["temperature"] = temperature
    return keys


def build_request_body(
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    max_tokens: int | None,
    temperature: float | None,
    reasoning_effort: str | None,
    tool_choice: str | dict[str, Any] | None,
    stream: bool,
) -> dict[str, Any]:
    system, converted_messages = convert_messages(messages)
    output_limit = max(1, max_tokens or DEFAULT_MAX_OUTPUT_TOKENS)
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": output_limit,
        "messages": converted_messages,
        "stream": stream,
    }
    if system is not None:
        body["system"] = system
    if tools and tool_choice != "none":
        body["tools"] = _tool_definitions(tools)
        body["tool_choice"] = _tool_choice(tool_choice) or {"type": "auto"}
    body.update(generation_keys(model, temperature, reasoning_effort))
    return body


def _strip_cache_control(value: Any) -> Any:
    """Remove cache markers when the selected model family cannot read them."""
    if isinstance(value, list):
        return [_strip_cache_control(item) for item in value]
    if isinstance(value, dict):
        return {key: _strip_cache_control(item) for key, item in value.items() if key != "cache_control"}
    return value


def _usage(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    prompt = int(_get(raw, "input_tokens", 0) or 0)
    completion = int(_get(raw, "output_tokens", 0) or 0)
    cache_read = int(_get(raw, "cache_read_input_tokens", 0) or 0)
    cache_write = int(_get(raw, "cache_creation_input_tokens", 0) or 0)
    creation = _get(raw, "cache_creation", {}) or {}
    if not cache_write and isinstance(creation, dict):
        cache_write += sum(int(v or 0) for v in creation.values() if isinstance(v, (int, float)))
    total = int(_get(raw, "total_tokens", 0) or (prompt + completion + cache_read + cache_write))
    result = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "prompt_tokens_include_cache": False,
    }
    # Absent is not zero: a turn whose reasoning text never arrived is only
    # distinguishable from a turn that did not think by this count.
    details = _get(raw, "output_tokens_details", {}) or {}
    for key in ("thinking_tokens", "reasoning_tokens"):
        thought = token_count(_get(details, key))
        if thought is not None:
            result["reasoning_tokens"] = thought
            break
    if _get(raw, "cache_read_input_tokens") is not None:
        result["cache_read_input_tokens"] = cache_read
    if _get(raw, "cache_creation_input_tokens") is not None or creation:
        result["cache_creation_input_tokens"] = cache_write
    cost = reported_cost(_get(raw, "cost"))
    if cost is not None:
        result["cost_usd"] = cost
    return result


def _merge_usage(target: dict[str, Any], raw: Any) -> None:
    """Merge message-start and message-delta usage without erasing counts."""
    incoming = _usage(raw)
    for key, value in incoming.items():
        if value or key not in target or key == "cost_usd":
            target[key] = value
    target["total_tokens"] = sum(
        int(target.get(key, 0))
        for key in ("prompt_tokens", "completion_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    )


def _finish_reason(reason: Any) -> str:
    return _FINISH_REASONS.get(str(reason or "end_turn"), str(reason or "stop"))


def parse_message(payload: dict[str, Any]) -> LLMResponse:
    """Parse a non-streaming Anthropic response."""
    text: list[str] = []
    reasoning: list[str] = []
    thinking_blocks: list[dict[str, Any]] = []
    calls: list[ToolCallRequest] = []
    ids: dict[str, str] = {}
    for block in payload.get("content") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            if block.get("text"):
                text.append(str(block["text"]))
        elif kind == "thinking":
            thinking_blocks.append(deepcopy(block))
            if block.get("thinking"):
                reasoning.append(str(block["thinking"]))
        elif kind == "redacted_thinking":
            thinking_blocks.append(deepcopy(block))
        elif kind == "tool_use":
            arguments, repaired = _parse_json_object(block.get("input"))
            calls.append(
                ToolCallRequest(
                    id=_safe_tool_id(block.get("id"), ids),
                    name=normalized_tool_name(block.get("name") or ""),
                    arguments=arguments,
                    run_meta=RunMeta(arguments_repaired=True) if repaired else None,
                )
            )
    return LLMResponse(
        content="".join(text) or None,
        tool_calls=calls,
        finish_reason="tool_calls" if calls else _finish_reason(payload.get("stop_reason")),
        usage=_usage(payload.get("usage")),
        reasoning_content="".join(reasoning) or None,
        thinking_blocks=thinking_blocks or None,
    )


async def _iter_sse(response: httpx.Response, timeout: float, first_byte: float = 0.0) -> AsyncIterator[dict[str, Any]]:
    """Anthropic SSE events, with the wait for the *first* line bounded apart.

    A stream that has not started is not a stream that stopped mid-answer, and
    the two are minutes apart: ``first_byte`` bounds the former and ``timeout``
    (the idle cap) every line after it. 0 means no separate bound and the idle
    cap covers both, which is what this did before ``llmFirstByteTimeout``.
    """
    event_name = ""
    data_lines: list[str] = []
    lines = response.aiter_lines()
    started = asyncio.get_running_loop().time()
    opening = first_byte > 0
    while True:
        try:
            line = await asyncio.wait_for(lines.__anext__(), first_byte if opening else timeout)
        except StopAsyncIteration:
            break
        except TimeoutError as exc:
            if not opening:
                raise
            waited = asyncio.get_running_loop().time() - started
            raise FirstByteTimeoutError(
                phase="waiting for the first stream event", budget=first_byte, waited=waited
            ) from exc
        opening = False
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
        elif line == "":
            if data_lines:
                raw = "\n".join(data_lines).strip()
                data_lines = []
                try:
                    payload = json.loads(raw)
                except Exception:
                    payload = None
                if isinstance(payload, dict):
                    if event_name and "type" not in payload:
                        payload["type"] = event_name
                    yield payload
            event_name = ""
    if data_lines:
        try:
            payload = json.loads("\n".join(data_lines).strip())
        except Exception:
            payload = None
        if isinstance(payload, dict):
            yield payload


async def consume_message_stream(
    response: httpx.Response, timeout: float, first_byte: float = 0.0
) -> AsyncIterator[ChatDelta]:
    """Normalize Anthropic SSE events into Raven stream deltas."""
    tool_indices: dict[int, int] = {}
    tool_ids: dict[int, str] = {}
    tool_names: dict[int, str] = {}
    thinking: dict[int, dict[str, Any]] = {}
    next_tool = 0
    usage: dict[str, Any] = {}
    terminal_sent = False

    async for event in _iter_sse(response, timeout, first_byte):
        kind = event.get("type") or ""
        if kind == "message_start":
            message = event.get("message") or {}
            _merge_usage(usage, _get(message, "usage"))
            continue
        if kind == "content_block_start":
            index = int(event.get("index", 0) or 0)
            block = event.get("content_block") or {}
            block_type = block.get("type")
            if block_type in {"thinking", "redacted_thinking"}:
                thinking[index] = deepcopy(block)
            elif block_type == "tool_use":
                slot = next_tool
                next_tool += 1
                tool_indices[index] = slot
                tool_ids[index] = str(block.get("id") or f"toolu_{slot}")
                tool_names[index] = str(block.get("name") or "")
                initial = block.get("input")
                args = json.dumps(initial, ensure_ascii=False) if isinstance(initial, dict) and initial else ""
                yield ChatDelta(
                    content=None,
                    tool_call_delta={
                        "tool_calls": [
                            {
                                "index": slot,
                                "id": tool_ids[index],
                                "function": {"name": tool_names[index], "arguments": args},
                            }
                        ]
                    },
                )
            continue
        if kind == "content_block_delta":
            index = int(event.get("index", 0) or 0)
            delta = event.get("delta") or {}
            delta_type = delta.get("type")
            if delta_type == "text_delta" and delta.get("text"):
                yield ChatDelta(content=str(delta["text"]))
            elif delta_type == "thinking_delta" and delta.get("thinking"):
                if index in thinking:
                    thinking[index]["thinking"] = thinking[index].get("thinking", "") + str(delta["thinking"])
                yield ChatDelta(content=None, reasoning_content=str(delta["thinking"]))
            elif delta_type == "signature_delta":
                if index in thinking:
                    thinking[index]["signature"] = thinking[index].get("signature", "") + str(
                        delta.get("signature") or ""
                    )
            elif delta_type == "input_json_delta" and index in tool_indices:
                yield ChatDelta(
                    content=None,
                    tool_call_delta={
                        "tool_calls": [
                            {
                                "index": tool_indices[index],
                                "function": {"arguments": str(delta.get("partial_json") or "")},
                            }
                        ]
                    },
                )
            continue
        if kind == "message_delta":
            delta = event.get("delta") or {}
            _merge_usage(usage, event.get("usage"))
            reason_value = delta.get("stop_reason")
            if reason_value is None:
                yield ChatDelta(content=None, usage=usage or None)
                continue
            reason = _finish_reason(reason_value)
            terminal_sent = True
            yield ChatDelta(
                content=None,
                usage=usage or None,
                finish_reason=reason,
                thinking_blocks=list(thinking.values()) or None,
            )
            continue
        if kind == "error":
            error = event.get("error") or {}
            detail = ": ".join(str(part) for part in (error.get("type"), error.get("message")) if part)
            raise RuntimeError(detail or "Anthropic Messages stream failed")
        if kind == "message_stop" and not terminal_sent:
            terminal_sent = True
            yield ChatDelta(
                content=None,
                usage=usage or None,
                finish_reason="stop",
                thinking_blocks=list(thinking.values()) or None,
            )

    if not terminal_sent:
        raise RuntimeError("Anthropic Messages body ended before completion")


class AnthropicMessagesProvider(LLMProvider):
    """Call a direct Anthropic or Anthropic-compatible Messages endpoint."""

    api_protocol = "anthropic"

    def __init__(
        self,
        api_key: str = "",
        api_base: str | None = None,
        default_model: str = "anthropic/claude-sonnet-5",
        extra_headers: dict[str, str] | None = None,
        provider_name: str | None = None,
        model_overrides: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.extra_headers = extra_headers or {}
        self.model_overrides = model_overrides or {}
        self._provider_name = provider_name or "anthropic"
        # Output ceilings learned from a model's own 400, so the next request
        # to it starts there instead of earning the same refusal again.
        self._ceilings: dict[str, int] = {}

    @property
    def provider_name(self) -> str:
        return self._provider_name

    def get_default_model(self) -> str:
        return self.default_model

    def wire_model_id(self, model: str) -> str:
        from raven.providers.wire import wire_model

        return wire_model(model, client_provider=self._provider_name)

    def can_serve(self, model: str) -> bool:
        from raven.providers.registry import find_by_model, find_by_name

        mine = find_by_name(self._provider_name)
        if mine is None or mine.is_gateway:
            return True
        theirs = find_by_model(model)
        return theirs is None or theirs.name == mine.name

    def supports_native_tool_result_images(self, model: str | None = None) -> bool:
        del model
        return True

    @staticmethod
    def _parse_response(payload: dict[str, Any]) -> LLMResponse:
        """Expose the normalized parser for provider-level tests and probes."""
        return parse_message(payload)

    def request_generation(self, **asked: Any) -> dict[str, Any]:
        """See ``LLMProvider.request_generation``; adds this wire's reasoning shape."""
        record = super().request_generation(**asked)
        # The Messages API requires the field, so this wire always names a ceiling
        # even where nobody pinned one.
        record["max_tokens"] = record["output_ceiling"]
        wire_id = self.wire_model_id(asked.get("model") or self.default_model)
        keys = generation_keys(wire_id, record.get("temperature"), record.get("reasoning_effort"))
        record["temperature"] = keys.pop("temperature", None)
        record.update(keys)
        return record

    def reasoning_wire_keys(self, model: str | None, reasoning_effort: str | None) -> dict[str, Any]:
        """See ``LLMProvider.reasoning_wire_keys``; this wire translates the label.

        Which is why it has to answer for itself: ``_ADAPTIVE_EFFORTS`` maps both
        ``minimal`` and ``low`` onto ``low``, so those two rungs serialize to one
        request here -- ``output_config.effort: low`` on a model that thinks
        adaptively, ``reasoning.effort: low`` otherwise.
        """
        return thinking_request(self.wire_model_id(model or self.default_model), reasoning_effort)

    def _body(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str | None,
        max_tokens: int | None,
        temperature: float,
        reasoning_effort: str | None,
        tool_choice: str | dict[str, Any] | None,
        stream: bool,
    ) -> dict[str, Any]:
        wire_id = self.wire_model_id(model or self.default_model)
        body = build_request_body(
            model=wire_id,
            messages=messages,
            tools=tools,
            max_tokens=send_max_tokens(self.generation, wire_id, pinned=max_tokens),
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            tool_choice=tool_choice,
            stream=stream,
        )
        for pattern, overrides in self.model_overrides.items():
            if isinstance(overrides, dict) and pattern.lower() in (model or self.default_model).lower():
                body.update(overrides)
        learned = self._ceilings.get(body["model"])
        if learned:
            clamp_to_model_limit(body, f"max_tokens: {body.get('max_tokens')} > {learned}")
        if not self.supports_prompt_caching(model or self.default_model):
            body = _strip_cache_control(body)
        return body

    def supports_prompt_caching(self, model: str) -> bool:
        """See ``LLMProvider.supports_prompt_caching``.

        This transport speaks Anthropic's own wire, so the field always has a
        place to go; the answer is whether the model's vendor reads it. Without
        this override the base default (False) told ``CacheOptimizer`` to place
        no breakpoints, and every request went out uncached.
        """
        url = messages_url(self.api_base)
        addressed_to = "anthropic" if _is_openrouter(url) or self._provider_name == "anthropic" else self._provider_name
        return accepts_cache_control(model or self.default_model, addressed_to=addressed_to)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        url = messages_url(self.api_base)
        body = self._body(
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            tool_choice=tool_choice,
            stream=False,
        )
        try:
            # A non-streaming completion's first response byte and its last are
            # the same byte, so the wait stays on the whole-call budget; what is
            # separable is everything before the model starts generating, and
            # ``httpx_timeout`` puts connect/write/pool on the first-byte bound
            # while leaving the read wide. A plain float reached httpx as the
            # same number on all four phases, so a dead route cost 1800s.
            async with httpx.AsyncClient(timeout=httpx_timeout(self.generation) or self.generation.timeout) as client:
                for _attempt in range(3):
                    response = await asyncio.wait_for(
                        client.post(url, headers=build_headers(self.api_key or "", url, self.extra_headers), json=body),
                        self.generation.timeout,
                    )
                    repair = rewrite_on_400(body, _error_text(response)) if response.status_code == 400 else None
                    if repair:
                        if repair == "ceiling":
                            self._remember_ceiling(body)
                        continue
                    break
            if response.status_code != 200:
                raise ProviderHTTPError(response.status_code, _error_text(response))
            return self._parse_response(response.json())
        except Exception as exc:
            return self._error_response(exc)

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: object = LLMProvider._SENTINEL,
        temperature: object = LLMProvider._SENTINEL,
        reasoning_effort: object = LLMProvider._SENTINEL,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> AsyncIterator[ChatDelta]:
        gen = getattr(self, "generation", None)
        if max_tokens is self._SENTINEL:
            max_tokens = getattr(gen, "max_tokens", None)
        if temperature is self._SENTINEL:
            temperature = getattr(gen, "temperature", 0.7)
        if reasoning_effort is self._SENTINEL:
            reasoning_effort = getattr(gen, "reasoning_effort", None)
        url = messages_url(self.api_base)
        body = self._body(
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens if isinstance(max_tokens, int) else None,
            temperature=temperature if isinstance(temperature, (int, float)) else 0.7,
            reasoning_effort=reasoning_effort if isinstance(reasoning_effort, str) else None,
            tool_choice=tool_choice,
            stream=True,
        )
        # The declared first-byte bound, on the two awaits that can hold a
        # stream silent before it starts: opening it, and its first event. The
        # client's own float bounded neither -- for a stream, waiting on the
        # response headers is a read, and httpx leaves read on the call budget.
        first_byte = stream_first_byte_budget(self.generation)
        try:
            async with httpx.AsyncClient(timeout=httpx_timeout(self.generation) or self.generation.timeout) as client:
                for _attempt in range(3):
                    async with contextlib.AsyncExitStack() as opened:
                        response = await self._open_stream(opened, client, url, body, first_byte)
                        if response.status_code != 200:
                            detail = await _error_text_async(response)
                            repair = rewrite_on_400(body, detail) if response.status_code == 400 else None
                            if repair:
                                if repair == "ceiling":
                                    self._remember_ceiling(body)
                                continue
                            raise ProviderHTTPError(response.status_code, detail)
                        # The per-line watchdog runs on the stream-idle budget,
                        # not the call budget (reviewed 2026-09-07: this adapter
                        # fed it self.generation.timeout, so streamIdleTimeout
                        # did nothing on the default Claude path). getattr: a
                        # GenerationSettings from before the field falls back to
                        # the call budget.
                        idle_timeout = getattr(self.generation, "stream_idle_timeout", None) or self.generation.timeout
                        async for delta in consume_message_stream(response, idle_timeout, first_byte=first_byte):
                            yield delta
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            classification = self.classify_error(exc)
            yield ChatDelta(
                content=format_llm_error(exc, classification, provider=self._provider_name),
                finish_reason="error",
                error_classification=classification,
            )

    async def _open_stream(
        self,
        opened: contextlib.AsyncExitStack,
        client: httpx.AsyncClient,
        url: str,
        body: dict[str, Any],
        first_byte: float,
    ) -> httpx.Response:
        """The streaming response, with the open bounded at the first-byte budget.

        Entered through the caller's stack rather than an ``async with`` here so
        the connection is closed by the caller's scope whichever way this
        returns, and so a bounded open can be cancelled without leaving the
        context half-entered.
        """
        request = client.stream(
            "POST",
            url,
            headers=build_headers(self.api_key or "", url, self.extra_headers, stream=True),
            json=body,
        )
        if first_byte <= 0:
            return await opened.enter_async_context(request)
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            return await asyncio.wait_for(opened.enter_async_context(request), first_byte)
        except TimeoutError as exc:
            waited = loop.time() - started
            logger.warning(
                "LLM first byte: nothing from {} after {:.1f}s while opening the stream "
                "(bound {}={:g}s); giving the call up as stalled so the retry ladder can ask again",
                self._provider_name or "anthropic",
                waited,
                BOUND_NAME,
                first_byte,
            )
            raise FirstByteTimeoutError(phase="opening the stream", budget=first_byte, waited=waited) from exc

    def _remember_ceiling(self, body: dict[str, Any]) -> None:
        """Called only for a 400 that named the model's ceiling: the other repairs
        leave ``max_tokens`` at whatever the caller pinned, and a short pin
        remembered as a ceiling would clamp every later request to it."""
        limit = int(body.get("max_tokens") or 0)
        if limit:
            self._ceilings[str(body.get("model"))] = min(limit, self._ceilings.get(str(body.get("model")), limit))

    def _error_response(self, exc: Exception) -> LLMResponse:
        classification = self.classify_error(exc)
        return LLMResponse(
            content=format_llm_error(exc, classification, provider=self._provider_name),
            finish_reason="error",
            error_classification=classification,
        )


def _error_text(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        return response.text or f"HTTP {response.status_code}"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("type") or payload)
        return str(payload.get("message") or payload)
    return str(payload)


async def _error_text_async(response: httpx.Response) -> str:
    try:
        raw = await response.aread()
        payload = json.loads(raw)
    except Exception:
        return f"HTTP {response.status_code}"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("type") or payload)
        return str(payload.get("message") or payload)
    return str(payload)


# Short alias for callers that refer to the selected protocol as "anthropic".
AnthropicProvider = AnthropicMessagesProvider
# Keep the naming used by the existing OpenAI transport available to callers
# that inspect conversion helpers while the public names remain descriptive.
_convert_messages = convert_messages
_convert_tools = _tool_definitions


__all__ = [
    "AnthropicMessagesProvider",
    "AnthropicProvider",
    "build_headers",
    "build_request_body",
    "consume_message_stream",
    "convert_messages",
    "_convert_messages",
    "_convert_tools",
    "messages_url",
    "parse_message",
]
