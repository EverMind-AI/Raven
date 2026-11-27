"""The Responses SSE state both adapters assemble a reply through.

The Codex and direct Responses adapters read the same event stream, so the
assembly lives here rather than in either transport: one copy for ``chat`` and
``chat_stream`` on both wires.
"""

from __future__ import annotations

import json
from typing import Any

import json_repair

from raven.providers.base import RunMeta, ToolCallRequest
from raven.providers.tool_arguments import object_arguments
from raven.providers.tool_names import normalized_tool_name
from raven.providers.usage import merge_usage

_FINISH_REASON_MAP = {"completed": "stop", "incomplete": "length", "failed": "error", "cancelled": "error"}

# Only the two reasons the loop can act on are named; any other is not guessed
# into one of them.
_INCOMPLETE_REASONS = {"max_output_tokens": "length", "content_filter": "content_filter"}


def _map_finish_reason(status: str | None) -> str:
    # A ``response.completed`` that names no status still says it completed; a
    # status nobody mapped says nothing.
    return _FINISH_REASON_MAP.get(status or "completed", "unknown")


class _ResponsesStreamState:
    """One Responses SSE body, assembled event by event.

    ``feed`` keeps what one event contributes and answers the text a streaming
    reader may show as it arrives -- the reply's deltas and a refusal's, which
    ``chat`` only ever sees folded together. ``result`` is what ``chat`` needs
    once the body ends. Both readers share this state so the streamed and
    non-streamed paths cannot disagree on tool calls, usage or the finish
    reason.
    """

    def __init__(self, usage_sink: dict[str, Any] | None = None) -> None:
        self._usage_sink = usage_sink
        self.content = ""
        self.refusal = ""
        self.tool_calls: list[ToolCallRequest] = []
        self._tool_call_buffers: dict[str, dict[str, Any]] = {}
        # What a stream that closes without a terminal event keeps: nothing here
        # said how the response ended, so it must not read as a finished one.
        self.finish_reason = "unknown"

    def feed(self, event: dict[str, Any]) -> tuple[str | None, str | None]:
        """Take one event; answer the (text, refusal) a reader may show now."""
        raw_usage = (event.get("response") or {}).get("usage")
        if self._usage_sink is not None and isinstance(raw_usage, dict):
            self._usage_sink.update(merge_usage(self._usage_sink, raw_usage))
        event_type = event.get("type")
        if event_type == "response.output_item.added":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                call_id = item.get("call_id")
                if call_id:
                    self._tool_call_buffers[call_id] = {
                        "id": item.get("id") or "fc_0",
                        "name": item.get("name"),
                        "arguments": item.get("arguments") or "",
                    }
        elif event_type == "response.output_text.delta":
            delta = event.get("delta") or ""
            self.content += delta
            return delta or None, None
        elif event_type == "response.refusal.delta":
            delta = event.get("delta") or ""
            self.refusal += delta
            return None, delta or None
        elif event_type == "response.refusal.done":
            # The whole text, which the deltas have already spelled out.
            self.refusal = event.get("refusal") or self.refusal
        elif event_type == "response.function_call_arguments.delta":
            call_id = event.get("call_id")
            if call_id and call_id in self._tool_call_buffers:
                self._tool_call_buffers[call_id]["arguments"] += event.get("delta") or ""
        elif event_type == "response.function_call_arguments.done":
            call_id = event.get("call_id")
            if call_id and call_id in self._tool_call_buffers:
                self._tool_call_buffers[call_id]["arguments"] = event.get("arguments") or ""
        elif event_type == "response.output_item.done":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                call_id = item.get("call_id")
                if call_id:
                    buf = self._tool_call_buffers.get(call_id) or {}
                    args_raw = buf.get("arguments") or item.get("arguments") or "{}"
                    # Repaired rather than wrapped in {"raw": ...}, and the repair
                    # recorded: a turn cut mid-blob ends the arguments text here,
                    # and that is the one local signal the call never finished
                    # arriving. Wrapped, it stayed dispatchable and the model read
                    # a schema complaint about a field it never sent.
                    repaired = False
                    try:
                        args = json.loads(args_raw)
                    except Exception:
                        args = json_repair.loads(args_raw)
                        repaired = True
                    args, parked = object_arguments(args, args_raw)
                    repaired = repaired or parked
                    self.tool_calls.append(
                        ToolCallRequest(
                            id=f"{call_id}|{buf.get('id') or item.get('id') or 'fc_0'}",
                            name=normalized_tool_name(buf.get("name") or item.get("name")),
                            arguments=args,
                            run_meta=RunMeta(arguments_repaired=True) if repaired else None,
                        )
                    )
        elif event_type in {"response.completed", "response.done"}:
            status = (event.get("response") or {}).get("status")
            self.finish_reason = _map_finish_reason(status)
        elif event_type == "response.incomplete":
            details = (event.get("response") or {}).get("incomplete_details")
            reason = details.get("reason") if isinstance(details, dict) else None
            self.finish_reason = _INCOMPLETE_REASONS.get(reason, "unknown")
        elif event_type in {"error", "response.failed"}:
            # The code is the retry signal: classify_error buckets by message
            # substring, and "server_is_overloaded" is what turns a dead-end
            # unknown into a retryable server error. An `error` event carries
            # it at the top level or under "error"; `response.failed` nests it
            # under the response.
            err = event.get("error") or (event.get("response") or {}).get("error") or {}
            if not isinstance(err, dict):
                err = {}
            code = err.get("code") or event.get("code") or ""
            message = err.get("message") or event.get("message") or ""
            detail = ": ".join(str(part) for part in (code, message) if part)
            raise RuntimeError(f"Codex response failed: {detail}" if detail else "Codex response failed")
        return None, None

    def result(self) -> tuple[str, list[ToolCallRequest], str]:
        content = self.content
        finish_reason = self.finish_reason
        # A refusal part makes the call a refusal even though the response closes
        # as ``completed``; a failed status keeps its own word.
        if self.refusal and finish_reason != "error":
            content = content or self.refusal
            finish_reason = "refusal"
        return content, self.tool_calls, finish_reason
