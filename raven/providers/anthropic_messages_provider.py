"""API-key transport for Anthropic Messages-compatible endpoints.

The transport deliberately speaks the native ``/v1/messages`` wire instead of
asking LiteLLM to translate an OpenAI Chat request.  That keeps Claude tool
use, prompt-cache markers, extended thinking blocks, and OpenRouter's native
Anthropic route intact.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any

import httpx
import json_repair

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
from raven.providers.prompt_cache import accepts_cache_control
from raven.providers.rates import CLAUDE_MAX_OUTPUT_TOKENS
from raven.providers.tool_names import normalized_tool_name

_DEFAULT_BASE = "https://api.anthropic.com"
_ANTHROPIC_VERSION = "2023-06-01"
# The Messages API requires ``max_tokens``. The number a request carries is
# decided by the one owner every path shares, ``send_max_tokens`` (the model's
# catalogue ceiling, ``rates.CLAUDE_MAX_OUTPUT_TOKENS`` when the catalogue has
# no row for it); the previous 4096 here cut a page's HTML on every write. A
# model whose real ceiling is below what the catalogue said names it in a 400;
# `clamp_to_model_limit` retries at that number and the provider remembers it.
# The budget is part of ``max_tokens``, so a budget sized to the ceiling left a
# high-effort turn returning nothing but thinking -- which the loop reads as an
# empty turn. Thinking now gets at most half the ceiling; the rest is visible.
_THINKING_BUDGETS = {"minimal": 1024, "low": 1024, "medium": 4096, "high": 8192, "xhigh": 16384, "max": 32768}
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


def _thinking_budget(max_tokens: int, effort: str | None) -> int | None:
    if not effort or effort.lower() in {"none", "off", "disabled"}:
        return None
    budget = min(_THINKING_BUDGETS.get(effort.lower(), 4096), max_tokens - _visible_reserve(max_tokens))
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
    """
    text = error_text or ""
    if clamp_to_model_limit(body, text):
        return "ceiling"
    thinking = body.get("thinking")
    if isinstance(thinking, dict) and "not supported" in text:
        if thinking.get("type") == "enabled" and "enabled" in text:
            effort = str((body.get("output_config") or {}).get("effort") or "high")
            body["thinking"] = {"type": "adaptive"}
            body["output_config"] = {"effort": effort}
            return "thinking"
        if thinking.get("type") == "adaptive" and "adaptive" in text:
            effort = str((body.pop("output_config", None) or {}).get("effort") or "high")
            budget = _thinking_budget(int(body.get("max_tokens") or CLAUDE_MAX_OUTPUT_TOKENS), effort)
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
    output_limit = max(1, max_tokens or CLAUDE_MAX_OUTPUT_TOKENS)
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
    thinking_on = bool(reasoning_effort) and reasoning_effort.lower() not in {"none", "off", "disabled"}
    if thinking_on and adaptive_thinking(model):
        body["thinking"] = {"type": "adaptive"}
        body["output_config"] = {"effort": _ADAPTIVE_EFFORTS.get(reasoning_effort.lower(), "high")}
    elif (budget := _thinking_budget(output_limit, reasoning_effort)) is not None:
        body["thinking"] = {"type": "enabled", "budget_tokens": budget}
    elif temperature is not None and not adaptive_thinking(model):
        # Claude 4.7 and later think adaptively by default, and a thinking
        # request accepts no temperature but the default -- so the repository's
        # 0.1 would be refused on the ordinary request.
        body["temperature"] = temperature
    return body


def _strip_cache_control(value: Any) -> Any:
    """Remove cache markers when the selected model family cannot read them."""
    if isinstance(value, list):
        return [_strip_cache_control(item) for item in value]
    if isinstance(value, dict):
        return {key: _strip_cache_control(item) for key, item in value.items() if key != "cache_control"}
    return value


def _usage(raw: Any) -> dict[str, int]:
    if not raw:
        return {}
    prompt = int(_get(raw, "input_tokens", 0) or 0)
    completion = int(_get(raw, "output_tokens", 0) or 0)
    cache_read = int(_get(raw, "cache_read_input_tokens", 0) or 0)
    cache_write = int(_get(raw, "cache_creation_input_tokens", 0) or 0)
    creation = _get(raw, "cache_creation", {}) or {}
    if not cache_write and isinstance(creation, dict):
        cache_write += sum(int(v or 0) for v in creation.values() if isinstance(v, (int, float)))
    total = int(_get(raw, "total_tokens", 0) or (prompt + completion))
    result = {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}
    if cache_read:
        result["cache_read_input_tokens"] = cache_read
    if cache_write:
        result["cache_creation_input_tokens"] = cache_write
    return result


def _merge_usage(target: dict[str, int], raw: Any) -> None:
    """Merge message-start and message-delta usage without erasing counts."""
    incoming = _usage(raw)
    for key, value in incoming.items():
        if value or key not in target:
            target[key] = value
    target["total_tokens"] = int(target.get("prompt_tokens", 0)) + int(target.get("completion_tokens", 0))


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


async def _iter_sse(response: httpx.Response, timeout: float) -> AsyncIterator[dict[str, Any]]:
    event_name = ""
    data_lines: list[str] = []
    lines = response.aiter_lines()
    while True:
        try:
            line = await asyncio.wait_for(lines.__anext__(), timeout)
        except StopAsyncIteration:
            break
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


async def consume_message_stream(response: httpx.Response, timeout: float) -> AsyncIterator[ChatDelta]:
    """Normalize Anthropic SSE events into Raven stream deltas."""
    tool_indices: dict[int, int] = {}
    tool_ids: dict[int, str] = {}
    tool_names: dict[int, str] = {}
    thinking: dict[int, dict[str, Any]] = {}
    next_tool = 0
    usage: dict[str, int] = {}
    terminal_sent = False

    async for event in _iter_sse(response, timeout):
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
            async with httpx.AsyncClient(timeout=self.generation.timeout) as client:
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
        try:
            async with httpx.AsyncClient(timeout=self.generation.timeout) as client:
                for _attempt in range(3):
                    async with client.stream(
                        "POST",
                        url,
                        headers=build_headers(self.api_key or "", url, self.extra_headers, stream=True),
                        json=body,
                    ) as response:
                        if response.status_code != 200:
                            detail = await _error_text_async(response)
                            repair = rewrite_on_400(body, detail) if response.status_code == 400 else None
                            if repair:
                                if repair == "ceiling":
                                    self._remember_ceiling(body)
                                continue
                            raise ProviderHTTPError(response.status_code, detail)
                        async for delta in consume_message_stream(response, self.generation.timeout):
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
