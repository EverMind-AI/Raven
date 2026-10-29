"""Which spawn calls and DAG runs a turn dispatched, read off its own tool rows.

A dispatch is known by the receipt its tool returned the moment the work was
accepted. The managers write those sentences, so they are parsed here, in one
place, for both readers: the board's post-turn call and ``session.history``. A
call refused before it started returns no such receipt and is not a dispatch.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from raven.agent.task_board.board import Dispatch

# "DAG run <id> started in the background ..." / "DAG run <id> finished: ..." from
# run_subagent_dag, and "DAG <id>: started '<name>' ..." from the playbook loader.
DAG_RUN_ID_RE = re.compile(r"\bDAG (?:run )?(\d{8}T\d+Z-[0-9a-f]+)")

# "Subagent [<summary>] started (id: <task_id>)." The summary is model-authored and
# may quote another receipt, so the manager's own suffix is the last match.
SPAWN_TASK_ID_RE = re.compile(r"\bstarted \(id: ([0-9a-f]{8})\)")

_DAG_TOOLS = ("run_subagent_dag", "load_playbook")


def dag_run_id_of(content: str) -> str | None:
    match = DAG_RUN_ID_RE.search(content)
    return match.group(1) if match else None


def spawn_task_id_of(content: str) -> str | None:
    matches = SPAWN_TASK_ID_RE.findall(content)
    return matches[-1] if matches else None


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text") or "") for part in content if isinstance(part, Mapping))
    return ""


def _arguments(call: Mapping[str, Any]) -> dict[str, Any]:
    raw = (call.get("function") or {}).get("arguments")
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def turn_dispatches(messages: Sequence[Mapping[str, Any]]) -> list[Dispatch]:
    """This turn's dispatches in the order they were first made, each once."""
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    found: dict[str, Dispatch] = {}
    for message in messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                name = (call.get("function") or {}).get("name") or ""
                if name == "spawn" or name in _DAG_TOOLS:
                    calls[str(call.get("id"))] = (name, _arguments(call))
            continue
        if message.get("role") != "tool":
            continue
        name, args = calls.get(str(message.get("tool_call_id")), ("", {}))
        content = _text(message.get("content"))
        if name == "spawn" and spawn_task_id_of(content) and args.get("node_id"):
            handle = f"spawn:{args['node_id']}"
            summary = str(args.get("task_summary") or "")
        elif name in _DAG_TOOLS and (run_id := dag_run_id_of(content)):
            handle = f"dag:{run_id}"
            summary = str(args.get("task_summary") or args.get("name") or "")
        else:
            continue
        found.setdefault(handle, Dispatch(handle, summary))
    return list(found.values())
