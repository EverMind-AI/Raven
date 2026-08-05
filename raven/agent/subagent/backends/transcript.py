"""Structured parsing of third-party CLI agent transcripts.

Both formats are newline-delimited JSON emitted by an agent CLI in headless
mode. Parsing is not cosmetic: a Claude Code run wraps its answer in tens of
kilobytes of hook and init events, so without extraction the reply is lost to
``max_output_chars`` truncation.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
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


__all__ = ["parse_codex_jsonl", "parse_claude_stream_json"]
