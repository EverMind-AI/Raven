"""Structured parsing of third-party CLI agent transcripts.

``codex_jsonl``, ``claude_stream_json`` and ``opencode_json`` are newline-delimited
JSON emitted by an agent CLI in headless mode; ``openclaw_json`` is a single JSON
document instead. Parsing is not cosmetic: a Claude Code run wraps its answer in
tens of kilobytes of hook and init events, so without extraction the reply is lost
to ``max_output_chars`` truncation.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any


def _iter_json_objects(stdout: str) -> Iterator[dict[str, Any]]:
    """Yield each line that parses as a JSON object, skipping everything else."""
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            yield obj


def parse_codex_jsonl(stdout: str) -> tuple[str | None, str | None]:
    """Parse a Codex ``exec --json`` transcript.

    The session id is the ``thread_id`` of the first ``thread.started`` event;
    the reply is the ``item.text`` of the last completed ``agent_message``.
    """
    thread_id: str | None = None
    reply: str | None = None
    for obj in _iter_json_objects(stdout):
        event_type = obj.get("type")
        if event_type == "thread.started" and thread_id is None:
            candidate = obj.get("thread_id")
            if isinstance(candidate, str):
                thread_id = candidate
        elif event_type == "item.completed":
            item = obj.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                reply = item["text"]
    return thread_id, reply


def parse_claude_stream_json(stdout: str) -> tuple[str | None, str | None, bool]:
    """Parse a Claude Code ``--output-format stream-json --verbose`` transcript.

    Every event carries ``session_id``; the answer is ``result`` on the terminal
    ``type == "result"`` event, which also carries ``is_error``. Claude does not
    reliably exit non-zero on failure under ``-p``, so ``is_error`` is the
    authoritative failure signal.
    """
    session_id: str | None = None
    reply: str | None = None
    is_error = False
    for obj in _iter_json_objects(stdout):
        candidate = obj.get("session_id")
        if session_id is None and isinstance(candidate, str):
            session_id = candidate
        if obj.get("type") == "result":
            is_error = bool(obj.get("is_error"))
            result = obj.get("result")
            if isinstance(result, str):
                reply = result
    return session_id, reply, is_error


def parse_openclaw_json(stdout: str) -> tuple[str | None, str | None]:
    """Parse an ``openclaw agent --json`` result.

    Unlike the other two formats this is one JSON document, not JSONL: the reply
    is the first ``payloads[]`` entry carrying text, falling back to
    ``meta.finalAssistantVisibleText`` for a run that produced no payload, and the
    session id is ``meta.agentMeta.sessionId``.
    """
    try:
        obj = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return None, None
    if not isinstance(obj, dict):
        return None, None

    meta = obj.get("meta")
    meta = meta if isinstance(meta, dict) else {}
    agent_meta = meta.get("agentMeta")
    agent_meta = agent_meta if isinstance(agent_meta, dict) else {}
    session_id = agent_meta.get("sessionId")
    if not isinstance(session_id, str):
        session_id = None

    reply: str | None = None
    payloads = obj.get("payloads")
    if isinstance(payloads, list):
        for payload in payloads:
            if isinstance(payload, dict) and isinstance(payload.get("text"), str):
                reply = payload["text"]
                break
    if reply is None and isinstance(meta.get("finalAssistantVisibleText"), str):
        reply = meta["finalAssistantVisibleText"]
    return session_id, reply


def parse_opencode_json(stdout: str) -> tuple[str | None, str | None]:
    """Parse an ``opencode run --format json`` transcript.

    Every event carries a top-level ``sessionID``; the reply is the ``part.text``
    of the ``type == "text"`` events. Text is collected per ``part.messageID``
    and only the last message's text is returned, because opencode opens a new
    message per step: a run that calls a tool emits ``tool_use`` under one
    messageID and the answer under the next, so returning every text part would
    prepend the model's intermediate narration to the answer.
    """
    session_id: str | None = None
    texts: dict[str, list[str]] = {}
    last_message: str | None = None
    for obj in _iter_json_objects(stdout):
        part = obj.get("part")
        part = part if isinstance(part, dict) else {}
        if session_id is None:
            candidate = obj.get("sessionID") or part.get("sessionID")
            if isinstance(candidate, str):
                session_id = candidate
        if obj.get("type") != "text" or not isinstance(part.get("text"), str):
            continue
        # A text part with no messageID still has to land somewhere, or a
        # transcript that omits the field would parse to an empty reply.
        message_id = part["messageID"] if isinstance(part.get("messageID"), str) else ""
        texts.setdefault(message_id, []).append(part["text"])
        last_message = message_id
    reply = "\n".join(texts[last_message]) if last_message is not None else None
    return session_id, reply


def parse_claude_stream_json_delta(line: str) -> str:
    """The reply text one ``--include-partial-messages`` line carries, or "".

    Line-at-a-time and stateless, unlike the parsers above, because this one
    runs while the process is still writing: it is handed each transcript line
    as it arrives rather than the finished document.

    Three filters, each measured against a live ``claude -p`` run rather than
    assumed:

    - only ``stream_event`` / ``content_block_delta``. The same stream repeats
      the finished text on an ``assistant`` event and again on ``result``;
      reading those too would render the reply three times.
    - only ``text_delta``. Its siblings ``thinking_delta`` and
      ``input_json_delta`` (a tool call's arguments assembling) are not reply
      text, and the second is not even prose.
    - only a null ``parent_tool_use_id``. A delta with one is a nested agent's
      output, which the run's own ``result`` does not contain either.

    An unparsable line is worth nothing here and returns "": this transport
    interleaves plain diagnostics with its transcript, and a keep-alive line
    must not fail a turn that is answering fine.
    """
    try:
        obj = json.loads(line.strip())
    except (json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(obj, dict) or obj.get("type") != "stream_event":
        return ""
    if obj.get("parent_tool_use_id") is not None:
        return ""
    event = obj.get("event")
    if not isinstance(event, dict) or event.get("type") != "content_block_delta":
        return ""
    delta = event.get("delta")
    if not isinstance(delta, dict) or delta.get("type") != "text_delta":
        return ""
    text = delta.get("text")
    return text if isinstance(text, str) else ""


_DELTA_READERS: dict[str, Callable[[str], str]] = {
    "claude_stream_json": parse_claude_stream_json_delta,
    # No entry for codex_jsonl on purpose: `codex exec --json` emits no partial
    # event at all. Measured -- a whole reply arrives as the `item.text` of one
    # `item.completed`, so an incremental reader would hand over the same single
    # frame the buffered path already returns.
}


def delta_reader(transcript_format: str | None) -> "Callable[[str], str] | None":
    """The per-line reply reader for a transcript format, or ``None``.

    ``None`` means the format carries no partial text, which is a fact about
    what the CLI emits rather than a gap here -- see ``_DELTA_READERS``.
    """
    return _DELTA_READERS.get(transcript_format or "")


__all__ = [
    "delta_reader",
    "parse_codex_jsonl",
    "parse_claude_stream_json",
    "parse_claude_stream_json_delta",
    "parse_openclaw_json",
    "parse_opencode_json",
]
