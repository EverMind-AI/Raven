"""Turn a stored transcript back into the stream that produced it.

``session/load`` is not a getter: the agent answers it by *replaying* the
conversation as ``session/update`` notifications -- the same notifications a
live turn produces -- and only then returns, so a resumed session draws itself
through the code path a fresh one draws itself through.

Ported from the main repo's ``raven/acp/replay.py`` and re-keyed to Raven-X's
stored message shape (``Session.messages`` as ``_save_turn`` writes them,
verified against a real session file):

* text lives under ``content``, not ``text``;
* an assistant entry's ``tool_calls`` are OpenAI-shaped --
  ``{"id", "function": {"name", "arguments": "<json string>"}}`` -- not flat;
* there are no ``diff`` / ``notice`` fields in this build, so those branches
  are not carried over.

The mapping is order-sensitive in one place that matters: a tool call is
announced on the assistant entry that made it and answered by a later
``role="tool"`` entry, so the ``tool_call`` and its ``tool_call_update`` come
from two different messages and must stay in that order.

Pure and synchronous on purpose: list-in, list-out, one unit test per case.
What is lost, stated rather than papered over: only what ``_save_turn`` kept
is replayable -- host-injected scaffolding was stripped at save time, and a
media attachment replays as the words around it.
"""

from __future__ import annotations

import json
from typing import Any

from raven.acp.redact import redact
from raven.acp.tool_kinds import locations, title_for, tool_kind

# A replayed transcript is bounded by what a client can draw, not by what is
# stored. Newest-last, so the truncation drops the oldest -- what a scrollback
# would have dropped too.
MAX_REPLAYED_MESSAGES = 500

# One message's text, capped: a replayed transcript is for reading.
MAX_REPLAYED_TEXT = 16 * 1024


def replay(messages: Any, *, cwd: str | None = None) -> list[dict[str, Any]]:
    """Every ``SessionUpdate`` for a stored transcript, oldest first.

    Returns the updates rather than sending them, so the caller owns the
    framing and the ordering against its own response.
    """
    if not isinstance(messages, list):
        return []
    entries = [m for m in messages if isinstance(m, dict) and m.get("role")]
    dropped = max(0, len(entries) - MAX_REPLAYED_MESSAGES)
    if dropped:
        entries = entries[-MAX_REPLAYED_MESSAGES:]
    updates: list[dict[str, Any]] = []
    if dropped:
        # Said in the transcript itself: a client that silently starts
        # mid-conversation shows a history that begins mid-thought.
        updates.append(
            _chunk(
                "agent_message_chunk",
                f"[{dropped} earlier message(s) are not shown; the session continues below]",
            )
        )
    for entry in entries:
        updates.extend(_replay_entry(entry, cwd=cwd))
    return updates


def _replay_entry(entry: dict[str, Any], *, cwd: str | None) -> list[dict[str, Any]]:
    role = entry.get("role")
    if role == "user":
        return _user(entry)
    if role == "assistant":
        return _assistant(entry, cwd=cwd)
    if role == "tool":
        return _tool_result(entry)
    # ``system`` and anything a future writer adds: a system prompt is not part
    # of the conversation a person had.
    return []


def _user(entry: dict[str, Any]) -> list[dict[str, Any]]:
    text = _text_of(entry)
    if not text:
        return []
    return [_chunk("user_message_chunk", text)]


def _assistant(entry: dict[str, Any], *, cwd: str | None) -> list[dict[str, Any]]:
    """The thought, then the words, then the calls -- the order they happened in."""
    out: list[dict[str, Any]] = []
    reasoning = entry.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning.strip():
        out.append(_chunk("agent_thought_chunk", redact(_clip(reasoning))))
    text = _text_of(entry)
    if text:
        out.append(_chunk("agent_message_chunk", text))
    for call in entry.get("tool_calls") or ():
        announced = _tool_call(call, cwd=cwd)
        if announced is not None:
            out.append(announced)
    return out


def _tool_call(call: Any, *, cwd: str | None) -> dict[str, Any] | None:
    """A stored call as the ``tool_call`` that announced it.

    ``status: "pending"`` here and nowhere else: on a replay the work is over
    and its own ``tool_call_update`` follows with the outcome. ``in_progress``
    would show a spinner for a call that finished last week; ``completed``
    would claim an outcome before the entry that carries it.
    """
    if not isinstance(call, dict):
        return None
    call_id = call.get("id")
    if not isinstance(call_id, str) or not call_id:
        return None
    name, arguments = _call_parts(call)
    update: dict[str, Any] = {
        "sessionUpdate": "tool_call",
        "toolCallId": call_id,
        "title": redact(title_for(name, arguments, None)),
        "kind": tool_kind(name),
        "status": "pending",
    }
    found = locations(arguments, cwd)
    if found:
        update["locations"] = found
    return update


def _tool_result(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """A stored ``role="tool"`` entry as the update that answered its call.

    Without a ``tool_call_id`` there is nothing to attach it to; an invented id
    would create a second row for a call that already has one, so such an
    entry is dropped rather than rendered loose.
    """
    call_id = entry.get("tool_call_id")
    if not isinstance(call_id, str) or not call_id:
        return []
    update: dict[str, Any] = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": call_id,
        "status": "completed",
    }
    text = _text_of(entry)
    if text:
        update["content"] = [{"type": "content", "content": {"type": "text", "text": text}}]
    return [update]


def _call_parts(call: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    """Name and parsed arguments of a stored call, whichever shape it was kept in.

    Raven-X persists the provider's OpenAI shape (``function.name`` plus
    ``function.arguments`` as the JSON *string* the provider sent); the flat
    shape is accepted too so a hand-written fixture or a future writer does not
    silently lose its title. Arguments that will not parse yield ``None`` --
    the title falls back gracefully, and half-parsed JSON on a tool row helps
    nobody.
    """
    function = call.get("function")
    source = function if isinstance(function, dict) else call
    name = source.get("name")
    raw = source.get("arguments")
    arguments: dict[str, Any] | None = None
    if isinstance(raw, dict):
        arguments = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = None
        arguments = parsed if isinstance(parsed, dict) else None
    return (name if isinstance(name, str) else None), arguments


def _chunk(kind: str, text: str) -> dict[str, Any]:
    return {"sessionUpdate": kind, "content": {"type": "text", "text": text}}


def _text_of(entry: dict[str, Any]) -> str:
    """The entry's text, redacted and clipped, or empty.

    Redacted on the way out for the same reason a live frame is: a command
    line recorded three turns ago carries whatever was on it.
    """
    text = entry.get("content")
    if not isinstance(text, str) or not text.strip():
        return ""
    return redact(_clip(text))


def _clip(text: str) -> str:
    if len(text) <= MAX_REPLAYED_TEXT:
        return text
    return text[:MAX_REPLAYED_TEXT] + "\n[truncated]"


__all__ = ["MAX_REPLAYED_MESSAGES", "MAX_REPLAYED_TEXT", "replay"]
