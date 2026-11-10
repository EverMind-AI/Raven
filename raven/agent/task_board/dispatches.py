"""Which spawn calls and DAG runs a turn dispatched, read off its own tool rows, and
which one a delegated result brings back when it opens a turn.

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

# The meta-tool that forwards to any registered tool by name. Under tool-search
# compaction it is how run_subagent_dag (never resident) is reached, so a call
# through it is read as the call it forwards.
_FORWARDER = "tool_call"


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


def _object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError, RecursionError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _arguments(call: Mapping[str, Any]) -> dict[str, Any]:
    return _object((call.get("function") or {}).get("arguments"))


def call_target(call: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """The tool a call reaches -- the tool it forwards to when it goes through
    ``tool_call`` -- and that call's arguments, parsed from either wire shape."""
    name = str((call.get("function") or {}).get("name") or "")
    arguments = _arguments(call)
    if name != _FORWARDER:
        return name, arguments
    return str(arguments.get("name") or ""), _object(arguments.get("arguments"))


def delegated_handle(mark: Mapping[str, Any] | None) -> str | None:
    """The handle of the run whose result opened a turn, read off its request's mark
    (``TurnRequest.delegated``): the one the turn that dispatched it placed. A graph is
    placed whole, so a node's report names its run. A result with no run to name -- a
    spawn whose origin carried no node id, an instance's unprompted turn -- names none."""
    if not mark:
        return None
    if mark.get("kind") == "spawn" and mark.get("node_id"):
        return f"spawn:{mark['node_id']}"
    if mark.get("kind") == "dag" and mark.get("run_id"):
        return f"dag:{mark['run_id']}"
    return None


def turn_dispatches(messages: Sequence[Mapping[str, Any]]) -> list[Dispatch]:
    """This turn's dispatches in the order they were first made, each once."""
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    found: dict[str, Dispatch] = {}
    for message in messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                name, args = call_target(call)
                if name == "spawn" or name in _DAG_TOOLS:
                    calls[str(call.get("id"))] = (name, args)
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
