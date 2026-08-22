"""Raven's own tool vocabulary, and the read-boundary mapping into it.

A delegated run's record carries the transport's own tool name: presentation is
recoverable from provenance and provenance is not recoverable from
presentation, and the record is what an extractor reads. The mapping into
raven's names therefore happens here, on the way to a client, so the wire keeps
the one vocabulary every renderer's verb table is keyed by.

These tables lived in ``acp_dialects`` and were applied when the record was
written. They are not ACP's -- the main session log stores real raven calls
under the same names -- which is why they sit outside that package now.

Must not be applied inside ``session.py``'s ``_map_to_wire``: that mapper also
serves the main session transcript, whose calls are the host's own and already
raven-named.
"""

from __future__ import annotations

import json
from typing import Any

RAVEN_NAME = {
    # The ACP spec's `kind` enum, which is all codex-acp reports.
    "read": "read_file",
    "edit": "edit_file",
    "delete": "delete_file",
    "move": "move_file",
    "search": "grep",
    "execute": "exec",
    "think": "think",
    "fetch": "web_fetch",
    "switch_mode": "switch_mode",
    # claude-agent-acp's own tool names, which are finer than `kind`: it cannot
    # tell `Glob` from `Grep`, and both are `kind: "search"`.
    "Bash": "exec",
    "BashOutput": "exec",
    "Read": "read_file",
    "Write": "write_file",
    "Edit": "edit_file",
    "NotebookEdit": "edit_file",
    "Glob": "find",
    "Grep": "grep",
    "LS": "list_dir",
    "WebFetch": "web_fetch",
    "WebSearch": "web_search",
    "Task": "spawn",
}

ARGUMENT_KEY = {
    "exec": "command",
    "read_file": "path",
    "write_file": "path",
    "edit_file": "path",
    "list_dir": "path",
    "delete_file": "path",
    "move_file": "path",
    "grep": "pattern",
    "find": "pattern",
    "web_fetch": "url",
    "web_search": "query",
}

SUBJECT_KEYS = ("command", "path", "file_path", "abs_path", "filePath", "pattern", "query", "url", "prompt", "argument")
"""``argument`` is the write path's own fallback key, not an adapter field name."""


def _promote(arguments: dict[str, Any], key: str | None) -> dict[str, Any]:
    """Move the subject onto ``key``, dropping the adapter's spelling of it.

    Insertion order is load-bearing: a reader that does not know this tool takes
    the first string value it finds, so the subject has to be it.

    The tool's own key is tried before the generic order, and the field the
    subject came from is dropped by key rather than by value. Scanning the
    generic order first picks the wrong field whenever a call carries two
    candidates -- ``grep`` with a ``path`` scope reports the directory it
    searched and loses the pattern -- and dropping by value deletes an unrelated
    field that happens to hold the same string.

    An empty value is dropped rather than carried: the write path this replaced
    never sent one, and a field naming nothing is worse than an absent field.

    Any string in the payload is the last resort rather than the first, because a
    call that names its subject under a key we know is better evidence than
    whichever field happens to come first. It cannot misfire on an ``openai``
    row: ``web_search`` is the only step type that is also an ``ARGUMENT_KEY``
    entry, and both values in its payload are lists.
    """
    source = None
    if key is not None:
        source = next(
            (k for k in (key, *SUBJECT_KEYS) if isinstance(v := arguments.get(k), str) and v.strip()),
            None,
        ) or next(
            (k for k, v in arguments.items() if isinstance(v, str) and v.strip()),
            None,
        )
    merged: dict[str, Any] = {}
    if source is not None:
        merged[key] = arguments[source].strip()
    dropped = {key, source} if source is not None else frozenset()
    for name, value in arguments.items():
        if name in dropped or value in (None, "", {}, []):
            continue
        merged[name] = value
    return merged


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    """One stored transcript row, named in raven's vocabulary for the wire.

    A copy, never in place: the input is the stored transcript, which a live
    read hands over by reference.
    """
    calls = row.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        return row
    out: list[dict[str, Any]] = []
    for call in calls:
        if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
            out.append(call)
            continue
        fn = call["function"]
        stored = fn.get("name")
        name = RAVEN_NAME.get(stored, stored) if isinstance(stored, str) else stored
        arguments = fn.get("arguments")
        try:
            parsed = json.loads(arguments) if isinstance(arguments, str) else None
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            promoted = _promote(parsed, ARGUMENT_KEY.get(name) if isinstance(name, str) else None)
            arguments = json.dumps(promoted, ensure_ascii=False, default=str)
        out.append({**call, "function": {**fn, "name": name, "arguments": arguments}})
    return {**row, "tool_calls": out}


__all__ = ["ARGUMENT_KEY", "RAVEN_NAME", "SUBJECT_KEYS", "normalize_row"]
