"""Reading an external agent run back from its ACP frame journal.

An ACP run used to leave one outer span and a pointer at the wire journal, so a
reader could see that the worker ran but not what it said or did. Spans are
recorded for new runs (see ``observability.record_steps``); this module serves
the runs that predate them, and is the one place the journal is parsed.

The aggregation mirrors ``acp_client.acp_agent._TurnCollector``: message chunks
merge into one message per burst, thoughts merge the same way, and a tool call
is paired with the ``tool_call_update`` that settled it. The result is the
provider shape ``turn_rows`` already renders, so the page shows a replayed run
with the same renderer as the live one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from raven.acp_client.acp_dialects import AcpDialect, content_texts
from raven.agent.subagent.backends import turn_rows

_MAX_FRAMES = 400_000
_MAX_BYTES = 64 * 1024 * 1024


def _slice(path: Path, start: int | None, end: int | None) -> bytes:
    size = path.stat().st_size
    begin = max(0, int(start)) if isinstance(start, int) else 0
    stop = min(size, int(end)) if isinstance(end, int) else size
    if stop <= begin or stop - begin > _MAX_BYTES:
        return b""
    with path.open("rb") as handle:
        handle.seek(begin)
        return handle.read(stop - begin)


def events(
    path: str | Path,
    *,
    session_id: str | None = None,
    start: int | None = None,
    end: int | None = None,
) -> tuple[str | None, list[dict[str, Any]]]:
    """(prompt, ordered events) for one run, in the collector's own shape.

    The events are ``{"t": say|thought|call|result, "at": <journal time>}`` --
    the same vocabulary ``_TurnCollector`` builds live, so a backfill writes
    spans with the same code the recorder does.
    """
    try:
        raw = _slice(Path(path), start, end)
    except OSError:
        return None, []
    dialect = AcpDialect()
    prompt_text: str | None = None
    events: list[dict[str, Any]] = []
    said: list[str] = []
    said_at: str | None = None
    thought: list[str] = []
    thought_at: str | None = None

    def flush_say() -> None:
        nonlocal said_at
        text = "".join(said).strip()
        said.clear()
        if text:
            events.append({"t": "say", "text": text, "at": said_at})
        said_at = None

    def flush_thought() -> None:
        nonlocal thought_at
        text = "".join(thought).strip()
        thought.clear()
        if text:
            events.append({"t": "thought", "text": text, "at": thought_at})
        thought_at = None

    for index, line in enumerate(raw.splitlines()):
        if index > _MAX_FRAMES:
            break
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(record, dict) or record.get("_type") != "acp_frame":
            continue
        frame = record.get("frame") if isinstance(record.get("frame"), dict) else {}
        params = frame.get("params") if isinstance(frame.get("params"), dict) else {}
        if session_id is not None and str(params.get("sessionId") or "") != session_id:
            continue
        at = record.get("timestamp") if isinstance(record.get("timestamp"), str) else None
        method = frame.get("method")
        if method == "session/prompt":
            flush_thought()
            prompt = params.get("prompt")
            texts = content_texts(prompt) if isinstance(prompt, list) else []
            if texts:
                prompt_text = "".join(texts)
            continue
        if method != "session/update":
            continue
        update = params.get("update") if isinstance(params.get("update"), dict) else {}
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            flush_thought()
            if said_at is None:
                said_at = at
            said.extend(content_texts(update.get("content")))
        elif kind == "agent_thought_chunk":
            flush_say()
            if thought_at is None:
                thought_at = at
            thought.extend(content_texts(update.get("content")))
        elif kind == "tool_call":
            flush_say()
            flush_thought()
            call = dialect.call(update)
            held = next((e for e in events if e.get("t") == "call" and e.get("id") == call.id), None)
            if held is not None:
                held["call"] = call
            else:
                events.append({"t": "call", "id": call.id or f"call-{len(events)}", "call": call, "at": at})
        elif kind == "tool_call_update" and update.get("status") in ("completed", "failed"):
            outcome = dialect.result(update)
            events.append(
                {
                    "t": "result",
                    "id": str(update.get("toolCallId") or ""),
                    "ok": outcome.ok,
                    "text": outcome.text,
                    "at": at,
                }
            )
    flush_say()
    flush_thought()
    return prompt_text, events


def replay(
    path: str | Path,
    *,
    session_id: str | None = None,
    start: int | None = None,
    end: int | None = None,
) -> list[dict[str, Any]]:
    """The run's conversation as provider messages: what it was asked, what it
    said, what it thought and what it ran, in the order the wire saw them."""
    prompt, steps = events(path, session_id=session_id, start=start, end=end)
    if prompt:
        steps = [{"t": "user", "text": prompt, "at": steps[0].get("at") if steps else None}, *steps]
    finished: list[dict[str, Any]] = []
    for event in steps:
        kind = event["t"]
        if kind == "say":
            finished.append(turn_rows.say(event["text"]))
        elif kind == "thought":
            finished.append(turn_rows.thought(event["text"], at=event.get("at")))
        elif kind == "user":
            finished.append(turn_rows.user(event["text"], at=event.get("at")))
        elif kind == "call":
            call = event["call"]
            finished.append(
                turn_rows.call(id=event["id"], name=call.name, arguments_json=call.arguments_json(), at=event.get("at"))
            )
        elif kind == "result":
            finished.append(turn_rows.result(id=event["id"], text=event["text"], ok=event["ok"], at=event.get("at")))
    return turn_rows.rows(finished)


_RENDER_TOTAL = 200_000
_RENDER_PART = 4_000


def _clip(text: object, limit: int = _RENDER_PART) -> str:
    value = str(text or "")
    return value if len(value) <= limit else value[:limit] + "\n… (truncated)"


_ITEM_LIMIT = 400


def items(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The replayed conversation as typed rows, the way the trajectory reads a
    run Raven recorded itself: an input row, an output row, a call and its
    result -- not one block of prose.

    The keys are the trajectory's own slot names (``llm.input``, ``llm.output``,
    ``tool.input``, ``tool.output``), so a reader maps a replayed run onto a
    recorded one without translating anything.
    """
    rows: list[dict[str, Any]] = []
    for message in messages:
        if len(rows) >= _ITEM_LIMIT:
            break
        role = message.get("role")
        if role == "assistant":
            reasoning = _clip(message.get("reasoning_content"), 20_000).strip()
            if reasoning:
                rows.append({"key": "llm.thinking", "value": reasoning, "source": "replay"})
            text = _clip(message.get("content"), 20_000).strip()
            if text:
                rows.append({"key": "llm.output", "value": text, "source": "replay"})
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") or {}
                name = str(function.get("name") or "tool")
                arguments = _clip(function.get("arguments"), 4_000)
                rows.append({"key": "tool.input", "value": f"{name} {arguments}".strip(), "source": "replay"})
        elif role == "tool":
            rows.append({"key": "tool.output", "value": _clip(message.get("content"), 20_000), "source": "replay"})
        else:
            rows.append({"key": "llm.input", "value": _clip(message.get("content"), 20_000), "source": "replay"})
    return rows


def render(messages: list[dict[str, Any]]) -> str:
    """The replayed conversation as readable text, for the details pane.

    One journal can hold tens of thousands of frames, so each part and the
    whole are clipped: the pane is a reading surface, not a pager.
    """
    lines: list[str] = []
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            text = _clip(message.get("content")).strip()
            if text:
                lines.append(text)
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                name = str((call.get("function") or {}).get("name") or "tool")
                arguments = _clip((call.get("function") or {}).get("arguments"))
                lines.append(f"→ {name} {arguments}".rstrip())
        elif role == "tool":
            lines.append(f"← {_clip(message.get('content'))}")
        else:
            lines.append(f"{role or 'message'}: {_clip(message.get('content'))}")
        lines.append("")
    glued = "\n".join(lines).strip()
    if len(glued) <= _RENDER_TOTAL:
        return glued
    return glued[:_RENDER_TOTAL] + "\n… (replay truncated)"


__all__ = ["events", "items", "replay", "render"]
