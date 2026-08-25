"""One sub-agent instance's own conversation, kept for the whole conversation.

A call record answers "what was this one dispatch", and an instance outlives it:
a stateful agent resumed under the same handle spans many calls, and those calls
arrive through three different lanes -- ``spawn``, a DAG node, and a direct chat
-- each of which writes its own record directory. So an instance's conversation
was only ever readable by walking three directory shapes and stitching them
together in the right order, which nothing did.

This module gives an instance one home per conversation, beside the rest of that
conversation's sub-agent history:

``<session_dir>/subagents/instances/<agent>/<handle>.jsonl``
    The instance's conversation, in the *same format* as the session log at
    ``sessions/<group>/<chat_id>.jsonl`` -- a ``_type: "metadata"`` header, then
    untagged message rows. Anything that can read a raven session can read a
    sub-agent instance's session.

The wire frames behind those turns are deliberately *not* copied here. They are
already written, in full and in order, by :mod:`raven.agent.acp.journal`, and a
per-instance copy was measured to hold no record the journal did not: 47 records
against 47, for 78x the transcript's bytes. What such a copy would have added is
reach -- the journal lives in the audit store, outside any workspace a sub-agent
can read -- and nothing asks for that today. A call's record still names the
journal and the byte range it occupied, so the frames remain findable.

Local file I/O rather than the DAG core's file backend, even on the DAG lane:
this is a session-level artifact outside any run directory, written the way
``SpawnRecord`` and ``DirectChatRecord`` beside it already write, and the only
backend that exists is the local one.

Nothing here may fail a run. Every write is best-effort and every failure is a
log line.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from raven.utils.helpers import safe_path_segment

_INSTANCES_DIRNAME = "instances"


def instance_root(session_dir: Path, agent: str) -> Path:
    """Where one agent's instance logs live in a conversation. Not created here."""
    return Path(session_dir) / "subagents" / _INSTANCES_DIRNAME / safe_path_segment(agent)


def transcript_path(session_dir: Path, agent: str, handle: str) -> Path:
    """The instance's conversation, in the session log's own format."""
    return instance_root(session_dir, agent) / f"{safe_path_segment(handle)}.jsonl"


def _header(*, session_key: str, agent: str, handle: str, kind: str, title: str = "") -> dict[str, Any]:
    """The opening record, in the shape a session log opens with.

    ``key`` names the instance rather than the conversation, so two instances of
    one agent in one conversation are not two files claiming the same key.
    ``opened_by`` is the lane that created the file and not a claim about the
    rest of it: an instance is reached through several, which is the reason this
    file exists.

    ``title`` is what the dispatch that opened this instance said it was for --
    a node's ``node_summary``, a spawn's ``task_summary``. It belongs on the
    header rather than on the turn because it describes the instance, and it
    lives here rather than in the instance registry because this file is already
    addressed by ``(agent, handle)``: a reader that has a registry row has the
    key to this file, while the registry would have to carry a copy that every
    status write could drop. Omitted when the lane that opened the file has no
    dispatch behind it (a direct chat, a hand-made instance) -- the reader falls
    back to the handle rather than showing an empty line.
    """
    now = datetime.now().isoformat()
    meta: dict[str, Any] = {
        "session_key": session_key,
        "agent": agent,
        "handle": handle,
        "opened_by": kind,
    }
    if title:
        meta["title"] = title
    return {
        "_type": "metadata",
        "key": f"{session_key}#{agent}/{handle}",
        "created_at": now,
        "updated_at": now,
        "metadata": meta,
    }


def _append(path: Path, records: list[dict[str, Any]], *, header: dict[str, Any]) -> None:
    """Append ``records``, writing ``header`` first if the file is new."""
    if not records:
        return
    lines = [json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in records]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        exists = path.exists()
        with path.open("a", encoding="utf-8") as handle:
            if not exists:
                handle.write(json.dumps(header, ensure_ascii=False) + "\n")
            handle.writelines(lines)
    except OSError as exc:
        logger.warning("Subagent instance log {} could not be appended: {}", path, exc)


def build_turn(
    *,
    prompt: str | None = None,
    messages: list[dict[str, Any]] | None = None,
    answer: str | None = None,
    error: str | None = None,
) -> list[dict[str, Any]]:
    """One finished turn as message rows: what was asked, what the run did on
    the way where the transport could see it, and what came back.

    Separate from :func:`append_turn` because a second caller needs the same
    rows without writing them: a sub-agent the host extracts memories *for*
    hands this turn to everos. Sharing the recipe is the point -- if the two
    drifted, the extracted memory would describe a conversation that no log on
    disk agrees with, and nothing would notice.
    """
    turn: list[dict[str, Any]] = []
    if prompt is not None:
        turn.append({"role": "user", "content": prompt, "timestamp": datetime.now().isoformat()})
    turn.extend(m for m in (messages or []) if isinstance(m, dict))
    if answer is not None:
        turn.append({"role": "assistant", "content": answer, "timestamp": datetime.now().isoformat()})
    if error is not None:
        turn.append({"role": "assistant", "content": f"[failed] {error}", "timestamp": datetime.now().isoformat()})
    return turn


def append_turn(
    session_dir: Path | None,
    *,
    agent: str,
    handle: str,
    session_key: str,
    kind: str = "",
    title: str = "",
    prompt: str | None = None,
    messages: list[dict[str, Any]] | None = None,
    answer: str | None = None,
    error: str | None = None,
) -> list[dict[str, Any]]:
    """Add one finished turn to this instance's conversation, and return it.

    The transcript grows by the turn as a conversation does: what was asked, what
    the run did on the way where the transport could see it, and what came back.
    A failed turn is appended too, as the error it ended with -- the run whose
    record is worth having is the one that went wrong.

    Returns the turn it just logged (empty when nothing was written), so a
    caller that also has to hand this call's conversation to everos for
    extraction reads back exactly what landed on disk instead of building its
    own copy that could drift from it.
    """
    if session_dir is None or not agent or not handle:
        return []
    turn = build_turn(prompt=prompt, messages=messages, answer=answer, error=error)

    header = _header(session_key=session_key, agent=agent, handle=handle, kind=kind, title=title)
    _append(transcript_path(session_dir, agent, handle), turn, header=header)
    return turn


def instance_title(session_dir: Path, agent: str, handle: str) -> str:
    """What this instance was dispatched for, or ``""``.

    Only the header is read: it is the first line of the file, so this costs one
    small read however long the conversation got.
    """
    path = transcript_path(session_dir, agent, handle)
    try:
        with path.open("r", encoding="utf-8") as fh:
            first = fh.readline()
    except OSError:
        return ""
    try:
        row = json.loads(first)
    except json.JSONDecodeError:
        return ""
    if not isinstance(row, dict) or row.get("_type") != "metadata":
        return ""
    meta = row.get("metadata")
    title = meta.get("title") if isinstance(meta, dict) else None
    return title if isinstance(title, str) else ""


def message_rows(session_dir: Path, agent: str, handle: str) -> list[dict[str, Any]]:
    """This instance's message rows, header skipped; empty when it has none.

    Rows rather than the file's existence, because the two differ: a crash
    between the header write and the first row leaves a file that exists without
    holding a conversation. Anything deciding whether this instance has a
    conversation on record has to ask that question, and asking it here is what
    keeps one answer to it.

    The header is the one tagged record in the file; everything else is a
    message, which is what makes this the same grammar as a session log. A line
    that will not parse is skipped rather than fatal, for the reason
    ``_map_to_wire`` skips a malformed stored message: one bad line must not
    empty a conversation.
    """
    rows: list[dict[str, Any]] = []
    try:
        with transcript_path(session_dir, agent, handle).open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and "_type" not in row and row.get("role"):
                    rows.append(row)
    except OSError:
        return []
    return rows


__all__ = [
    "append_turn",
    "build_turn",
    "instance_root",
    "message_rows",
    "transcript_path",
]
