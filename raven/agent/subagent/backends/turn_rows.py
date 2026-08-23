"""The rows one delegated turn contributes to an instance's conversation.

Owns the row shape and nothing else: an ordered event list in, provider-shaped
message rows out. Two transports produce that list -- the ACP collector reading
`session/update` notifications, and the OpenAI Step Dialect reading a response
field -- and one implementation of the shape is what makes an `openai`
instance's conversation read identically to an `acp` one.

Deliberately ignorant of both. A `call` event carries a tool name and a JSON
string, never an `acp_dialects.ToolCall`: a shared builder that imported one
transport's vocabulary would not be shared.

The final answer is not a row here. The record keeps it and the reader appends
it as the Closing Message; `in_flight_answer` is for a live read, which has no
record to append it from.
"""

from __future__ import annotations

from typing import Any


def say(text: str) -> dict[str, Any]:
    """Something the agent said between steps."""
    return {"t": "say", "text": text}


def thought(text: str, at: str | None = None) -> dict[str, Any]:
    return {"t": "thought", "text": text, "at": at}


def call(*, id: str, name: str, arguments_json: str, at: str | None = None) -> dict[str, Any]:
    return {"t": "call", "id": id, "name": name, "arguments_json": arguments_json, "at": at}


def result(*, id: str, text: str, ok: bool, at: str | None = None) -> dict[str, Any]:
    return {"t": "result", "id": id, "text": text, "ok": ok, "at": at}


def rows(
    events: list[dict[str, Any]],
    *,
    in_flight_answer: str | None = None,
    in_flight_answer_at: str | None = None,
) -> list[dict[str, Any]]:
    """The ordered events as provider-shaped messages.

    One assistant message per tool call, wearing whatever thought preceded it,
    followed by a ``role="tool"`` result matched through the call id -- the exact
    shape ``session.resume`` stores, so a client renders a delegated run with the
    renderer it already has.
    """
    msgs: list[dict[str, Any]] = []
    pending: list[str] = []
    pending_at: str | None = None
    narration: list[str] = []
    for ev in events:
        kind = ev.get("t")
        if kind == "say":
            narration.append(ev.get("text") or "")
        elif kind == "thought":
            if not pending:
                pending_at = ev.get("at")
            pending.append(ev.get("text") or "")
        elif kind == "call":
            entry: dict[str, Any] = {
                "role": "assistant",
                "content": "".join(narration).strip(),
                "tool_calls": [
                    {
                        "id": ev.get("id") or "",
                        "type": "function",
                        "function": {"name": ev.get("name") or "", "arguments": ev.get("arguments_json") or "{}"},
                    }
                ],
            }
            if at := (pending_at or ev.get("at")):
                entry["timestamp"] = at
            if pending:
                entry["reasoning_content"] = "".join(pending)
                pending = []
                pending_at = None
            narration = []
            msgs.append(entry)
        elif kind == "result":
            text = ev.get("text") or ""
            row: dict[str, Any] = {
                "role": "tool",
                "tool_call_id": ev.get("id") or "",
                "content": text if ev.get("ok") else f"[failed] {text}".strip(),
            }
            if at := ev.get("at"):
                row["timestamp"] = at
            msgs.append(row)
    if pending:
        trailing: dict[str, Any] = {"role": "assistant", "content": "", "reasoning_content": "".join(pending)}
        if pending_at:
            trailing["timestamp"] = pending_at
        msgs.append(trailing)
    if in_flight_answer:
        streaming: dict[str, Any] = {"role": "assistant", "content": in_flight_answer}
        if in_flight_answer_at:
            streaming["timestamp"] = in_flight_answer_at
        msgs.append(streaming)
    return msgs


__all__ = ["call", "result", "rows", "say", "thought"]
