"""Event parsing for the API-key OpenAI Responses transport."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import json_repair

from raven.providers.base import LLMResponse, RunMeta, ToolCallRequest
from raven.providers.openai_codex_provider import _map_finish_reason as map_finish_reason


def convert_tool_choice(tool_choice: str | dict[str, Any] | None) -> str | dict[str, Any] | None:
    if not isinstance(tool_choice, dict):
        return tool_choice
    fn = tool_choice.get("function")
    if tool_choice.get("type") == "function" and isinstance(fn, dict) and fn.get("name"):
        return {"type": "function", "name": fn["name"]}
    return tool_choice


def _event_dict(event: Any) -> dict[str, Any]:
    if isinstance(event, dict):
        return event
    dump = getattr(event, "model_dump", None)
    if callable(dump):
        value = dump(mode="python")
        if isinstance(value, dict):
            return value
    raise TypeError(f"unsupported Responses stream event: {type(event).__name__}")


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _text_parts(parts: Any) -> list[str]:
    return [str(text) for part in parts or [] if (text := _get(part, "text") or _get(part, "refusal"))]


def _parse_arguments(raw: Any) -> tuple[dict[str, Any], bool]:
    if isinstance(raw, dict):
        return raw, False
    try:
        parsed = json.loads(raw)
        repaired = False
    except Exception:
        try:
            parsed = json_repair.loads(raw)
        except Exception:
            return {"raw": raw}, True
        repaired = True
    if not isinstance(parsed, dict):
        return {"raw": raw}, True
    return parsed, repaired


def _tool_call(item: Any, fallback: dict[str, Any] | None = None) -> ToolCallRequest:
    fallback = fallback or {}
    raw = _get(item, "arguments") or fallback.get("arguments") or "{}"
    arguments, repaired = _parse_arguments(raw)
    call_id = _get(item, "call_id") or fallback.get("call_id") or "call_0"
    item_id = _get(item, "id") or fallback.get("id") or "fc_0"
    return ToolCallRequest(
        id=f"{call_id}|{item_id}",
        name=_get(item, "name") or fallback.get("name") or "",
        arguments=arguments,
        run_meta=RunMeta(arguments_repaired=True) if repaired else None,
    )


def parse_usage(response: Any) -> dict[str, int]:
    raw = _get(response, "usage")
    if not raw:
        return {}
    prompt = int(_get(raw, "input_tokens", 0) or 0)
    completion = int(_get(raw, "output_tokens", 0) or 0)
    usage = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": int(_get(raw, "total_tokens", 0) or (prompt + completion)),
    }
    details = _get(raw, "input_tokens_details") or {}
    cache_read = int(_get(details, "cached_tokens", 0) or _get(raw, "cache_read_input_tokens", 0) or 0)
    cache_write = int(
        _get(details, "cache_write_tokens", 0)
        or _get(details, "cache_creation_tokens", 0)
        or _get(raw, "cache_creation_input_tokens", 0)
        or 0
    )
    if cache_read:
        usage["cache_read_input_tokens"] = cache_read
    if cache_write:
        usage["cache_creation_input_tokens"] = cache_write
    return usage


def _response_error(response: Any) -> str:
    error = _get(response, "error")
    if isinstance(error, dict):
        code = error.get("code") or error.get("type")
        message = error.get("message")
    else:
        code = _get(error, "code") or _get(error, "type")
        message = _get(error, "message") or (str(error) if error else "")
    detail = ": ".join(str(part) for part in (code, message) if part)
    return detail or f"responses request {_get(response, 'status') or 'failed'}"


def _event_error(event: Any) -> str:
    response = _get(event, "response") or {}
    error = _get(event, "error") or _get(response, "error") or {}
    if isinstance(error, dict):
        code = error.get("code") or error.get("type") or _get(event, "code")
        message = error.get("message") or _get(event, "message")
    else:
        code = _get(event, "code")
        message = str(error) if error else _get(event, "message")
    detail = ": ".join(str(part) for part in (code, message) if part)
    return detail or "responses request failed"


def parse_response(response: Any) -> LLMResponse:
    status = _get(response, "status")
    if status in {"failed", "cancelled"}:
        raise RuntimeError(_response_error(response))

    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[ToolCallRequest] = []
    for item in _get(response, "output") or []:
        kind = _get(item, "type", "")
        if kind == "message":
            content_parts.extend(_text_parts(_get(item, "content")))
        elif kind == "reasoning":
            reasoning_parts.extend(_text_parts(_get(item, "summary") or _get(item, "content")))
        elif kind == "function_call" and _get(item, "call_id"):
            tool_calls.append(_tool_call(item))

    return LLMResponse(
        content="".join(content_parts) or None,
        tool_calls=tool_calls,
        finish_reason="tool_calls" if tool_calls else map_finish_reason(status),
        usage=parse_usage(response),
        reasoning_content="".join(reasoning_parts) or None,
        response_id=_get(response, "id"),
    )


def _pending_call(pending: dict[str, dict[str, Any]], event: Any) -> dict[str, Any] | None:
    if call_id := _get(event, "call_id"):
        return pending.get(call_id)
    item_id = _get(event, "item_id")
    return next((buf for buf in pending.values() if buf.get("id") == item_id), None)


async def consume_response_events(events: AsyncIterator[Any]) -> LLMResponse:
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    pending: dict[str, dict[str, Any]] = {}
    tool_calls: list[ToolCallRequest] = []
    terminal: Any = None
    status: str | None = None

    async for raw_event in events:
        event = _event_dict(raw_event)
        kind = _get(event, "type", "")
        if kind in {"response.output_text.delta", "response.refusal.delta"}:
            if delta := _get(event, "delta"):
                content_parts.append(str(delta))
        elif kind in {"response.reasoning_summary_text.delta", "response.reasoning_text.delta"}:
            if delta := _get(event, "delta"):
                reasoning_parts.append(str(delta))
        elif kind == "response.output_item.added":
            item = _get(event, "item") or {}
            call_id = _get(item, "call_id")
            if _get(item, "type") == "function_call" and call_id:
                pending[call_id] = {
                    "call_id": call_id,
                    "id": _get(item, "id") or "fc_0",
                    "name": _get(item, "name"),
                    "arguments": _get(item, "arguments") or "",
                }
        elif kind == "response.function_call_arguments.delta":
            if (buf := _pending_call(pending, event)) is not None:
                buf["arguments"] += _get(event, "delta") or ""
        elif kind == "response.function_call_arguments.done":
            if (buf := _pending_call(pending, event)) is not None:
                buf["arguments"] = _get(event, "arguments") or buf["arguments"]
        elif kind == "response.output_item.done":
            item = _get(event, "item") or {}
            call_id = _get(item, "call_id")
            if _get(item, "type") == "function_call" and call_id:
                tool_calls.append(_tool_call(item, pending.get(call_id)))
        elif kind in {"response.completed", "response.incomplete"}:
            terminal = _get(event, "response") or {}
            status = _get(terminal, "status") or ("incomplete" if kind == "response.incomplete" else "completed")
        elif kind in {"error", "response.failed"}:
            raise RuntimeError(_event_error(event))

    if status is None:
        raise RuntimeError("responses body ended before completion")
    if status in {"failed", "cancelled"}:
        raise RuntimeError(_response_error(terminal))

    terminal_response = parse_response(terminal)
    if not content_parts and terminal_response.content:
        content_parts.append(terminal_response.content)
    if not reasoning_parts and terminal_response.reasoning_content:
        reasoning_parts.append(terminal_response.reasoning_content)
    if not tool_calls:
        tool_calls = terminal_response.tool_calls

    return LLMResponse(
        content="".join(content_parts) or None,
        tool_calls=tool_calls,
        finish_reason="tool_calls" if tool_calls else map_finish_reason(status),
        usage=parse_usage(terminal),
        reasoning_content="".join(reasoning_parts) or None,
        response_id=_get(terminal, "id"),
    )


async def consume_response_stream(stream: Any) -> LLMResponse:
    try:
        return await consume_response_events(stream)
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            result = close()
            if hasattr(result, "__await__"):
                await result
