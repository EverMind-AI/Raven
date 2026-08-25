"""Turn a stored transcript back into the stream that produced it.

``session/load`` is not a getter. The client sends it and the agent answers by
*replaying* the conversation as ``session/update`` notifications -- the same
notifications a live turn produces -- and only then returns. So a resumed session
draws itself through the code path a fresh one draws itself through, and a client
needs no second renderer.

What makes this worth its own module is that the stored shape and the live shape
are different vocabularies. A transcript is a list of provider messages with
extras hung off them (``reasoning_content``, ``tool_calls``, ``diff``); the wire
is a sequence of typed chunks. The mapping is order-sensitive in one place that
matters: a tool call is announced on the assistant entry that made it and
answered by a later ``role="tool"`` entry, so the ``tool_call`` and its
``tool_call_update`` come from two different messages and must stay in that order.

Pure and synchronous on purpose. Everything here is list-in, list-out, so each
case is one unit test whose frames are checked against the official schema --
which is also what makes a replay bug cheap to find, since the alternative is
reading it off a client's screen.

**What is lost, stated rather than papered over.** ``_map_to_wire`` flattens a
multimodal user message to the text of its text blocks, so an image a person
attached three turns ago replays as the words around it. Recovering it means
reading ``raw.messages`` instead, where the blocks are still intact -- and then
re-reading files that may no longer exist. Not attempted here.
"""

from __future__ import annotations

from typing import Any

from raven.acp import redact
from raven.acp.tool_kinds import locations, title_for, tool_kind

# A replayed transcript is bounded by what a client can draw, not by what is
# stored: a session with two thousand messages would otherwise emit two thousand
# notifications before the load returns, and the request would look hung.
# Newest-last, so the truncation drops the oldest -- which is what a scrollback
# would have dropped too.
MAX_REPLAYED_MESSAGES = 500

# One message's text, capped. A replayed transcript is for reading; a tool result
# that was already truncated to a preview when it was live does not need its full
# stored form now.
MAX_REPLAYED_TEXT = 16 * 1024


def replay(messages: Any, *, session_id: str, cwd: str | None = None) -> list[dict[str, Any]]:
    """Every ``SessionUpdate`` for a stored transcript, oldest first.

    Returns the updates rather than sending them, so the caller owns the framing
    and the ordering against its own response.
    """
    if not isinstance(messages, list):
        return []
    entries = [m for m in messages if isinstance(m, dict) and m.get("role")]
    dropped = max(0, len(entries) - MAX_REPLAYED_MESSAGES)
    if dropped:
        entries = entries[-MAX_REPLAYED_MESSAGES:]
    updates: list[dict[str, Any]] = []
    if dropped:
        # Said out loud in the transcript itself. A client that silently starts
        # mid-conversation shows a person a history that appears to begin in the
        # middle of a thought.
        updates.append(
            _chunk(
                "agent_message_chunk",
                f"[{dropped} earlier message(s) are not shown; the session continues below]",
            )
        )
    # Tool calls announced but never answered: kept so a call whose result was
    # lost still renders as a row rather than vanishing.
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
    # ``system`` and anything a future writer adds. A system prompt is not part
    # of the conversation a person had, and replaying it would put the agent's
    # instructions on their screen.
    return []


def _user(entry: dict[str, Any]) -> list[dict[str, Any]]:
    text = _text_of(entry)
    if not text:
        return []
    return [_chunk("user_message_chunk", text)]


def _assistant(entry: dict[str, Any], *, cwd: str | None) -> list[dict[str, Any]]:
    """The thought, then the words, then the calls -- the order they happened in.

    A notice recorded on the entry replaces the answer rather than accompanying
    it (that is what ``action_blocked`` means), so it is rendered as the message
    when there is no text of its own.
    """
    out: list[dict[str, Any]] = []
    reasoning = entry.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning.strip():
        out.append(_chunk("agent_thought_chunk", _clip(reasoning)))
    text = _text_of(entry)
    if text:
        out.append(_chunk("agent_message_chunk", text))
    notice = entry.get("notice")
    if isinstance(notice, str) and notice.strip() and not text:
        out.append(_chunk("agent_message_chunk", _clip(notice)))
    for call in entry.get("tool_calls") or ():
        announced = _tool_call(call, cwd=cwd)
        if announced is not None:
            out.append(announced)
    return out


def _tool_call(call: Any, *, cwd: str | None) -> dict[str, Any] | None:
    """A stored call as the ``tool_call`` that announced it.

    ``status: "pending"`` here and nowhere else in this codebase: on a live call
    the status is ``in_progress`` because the work is running, but on a replay the
    work is over and its own ``tool_call_update`` follows with the outcome. A row
    left at ``in_progress`` would show a spinner for a call that finished last
    week; ``completed`` would claim an outcome before the entry that carries it.
    """
    if not isinstance(call, dict):
        return None
    call_id = call.get("id")
    name = call.get("name")
    if not isinstance(call_id, str) or not call_id:
        return None
    arguments = _arguments_of(call)
    update: dict[str, Any] = {
        "sessionUpdate": "tool_call",
        "toolCallId": call_id,
        "title": redact.redact(title_for(name if isinstance(name, str) else None, arguments, None)),
        "kind": tool_kind(name if isinstance(name, str) else None),
        "status": "pending",
    }
    found = locations(arguments, cwd)
    if found:
        update["locations"] = found
    return update


def _tool_result(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """A stored ``role="tool"`` entry as the update that answered its call.

    Without a ``tool_call_id`` there is nothing to attach it to, and an update
    with an invented id would create a second row for a call that already has
    one. Such an entry is dropped rather than rendered loose.
    """
    call_id = entry.get("tool_call_id")
    if not isinstance(call_id, str) or not call_id:
        return []
    update: dict[str, Any] = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": call_id,
        "status": "completed",
    }
    content: list[dict[str, Any]] = []
    text = _text_of(entry)
    if text:
        content.append({"type": "content", "content": {"type": "text", "text": text}})
    # The unified diff, not a structured one: the stored record is the rendering,
    # and the file's contents at the time are gone. Sent as text so the change is
    # visible at all, which beats a `diff` block whose newText would have to be
    # invented.
    diff = entry.get("diff")
    if isinstance(diff, str) and diff.strip():
        content.append({"type": "content", "content": {"type": "text", "text": _clip(diff)}})
    if content:
        update["content"] = content
    return [update]


def _chunk(kind: str, text: str) -> dict[str, Any]:
    return {"sessionUpdate": kind, "content": {"type": "text", "text": text}}


def _text_of(entry: dict[str, Any]) -> str:
    """The entry's text, redacted and clipped, or empty.

    Redacted on the way out for the same reason a live frame is: a replayed
    transcript is rendered in an editor and kept in its history, and a command
    line recorded three turns ago carries whatever was on it.
    """
    text = entry.get("text")
    if not isinstance(text, str) or not text.strip():
        return ""
    return redact.redact(_clip(text))


def _clip(text: str) -> str:
    if len(text) <= MAX_REPLAYED_TEXT:
        return text
    return text[:MAX_REPLAYED_TEXT] + "\n[truncated]"


def _arguments_of(call: dict[str, Any]) -> dict[str, Any] | None:
    """A stored call's arguments as a mapping, whatever shape they were kept in.

    They are stored as the JSON *string* the provider sent, so a title or a
    location needs them parsed. A string that will not parse yields ``None``,
    which the title falls back on gracefully -- guessing at half-parsed arguments
    would put a fragment of JSON on a tool row.
    """
    raw = call.get("arguments")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        import json

        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


__all__ = ["MAX_REPLAYED_MESSAGES", "MAX_REPLAYED_TEXT", "replay"]
