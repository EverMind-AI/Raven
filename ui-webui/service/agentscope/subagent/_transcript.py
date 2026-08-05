# -*- coding: utf-8 -*-
"""Structured parsing of CLI sub-agent transcripts (Codex JSONL)."""

import json


def parse_codex_jsonl(stdout: str) -> tuple[str | None, str | None]:
    """Parse a Codex ``exec --json`` transcript.

    The transcript is one JSON object per line. The session id is the
    ``thread_id`` of the first ``thread.started`` event; the reply is the
    ``item.text`` of the last completed ``agent_message`` item. Lines that
    are not JSON objects, and objects missing the expected fields, are
    tolerated (they contribute nothing).

    Args:
        stdout (`str`):
            The raw stdout of a Codex ``exec --json`` run.

    Returns:
        `tuple[str | None, str | None]`:
            ``(thread_id, reply_text)``. Either element is ``None`` when the
            corresponding event is absent or malformed.
    """
    thread_id: str | None = None
    reply: str | None = None
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        event_type = obj.get("type")
        if event_type == "thread.started" and thread_id is None:
            candidate = obj.get("thread_id")
            if isinstance(candidate, str):
                thread_id = candidate
        elif event_type == "item.completed":
            item = obj.get("item")
            if (
                isinstance(item, dict)
                and item.get("type") == "agent_message"
                and isinstance(item.get("text"), str)
            ):
                reply = item["text"]
    return thread_id, reply
