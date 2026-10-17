"""A Mirasim session's snapshot, rebuilt from the host's patches, and the ACP updates its changes make.

The host never streams events: it keeps one *snapshot* per session -- the turn's
phase, its text and reasoning so far, its tool calls, plan, sub-agents, files
delivered, steers taken and usage -- and sends a subscriber that snapshot once
(``snapshot``) and then patches against it (``session``). A patch is

- ``full``: the whole snapshot, replacing what is held;
- ``appendText`` / ``appendReasoning``: text added to ``text`` / ``reasoning``;
- ``set``: keys replaced whole, a list key included;
- ``lists``: list keys updated by index, ``{key: {len, at: {index: item}}}``,
  sent only to a client that asked for them.

applied in that order, the order of the host's own applier. With a stream
cadence asked for, several patches arrive merged as one frame that names the
sequence number it starts from (``fromSeq``); one that does not start where the
last ended means frames were lost, and the session is subscribed to again for a
fresh snapshot.

:class:`SessionView` holds the snapshot, and :class:`TurnStream` decides what of
it the ACP client has not seen yet. Both are pure -- no I/O, no clock -- so every
rule is tested against recorded shapes. Measured on Mirasim 0.0.465 and 0.0.537:

- a turn is one ``taskId``; a new one resets ``text`` and ``reasoning`` with a
  ``set`` and leaves the lists to whatever the new turn sets;
- the phase runs ``queued`` -> ``streaming`` -> ``done``, and only the first two
  are a turn still running (``Kl`` in the host);
- a tool call is ``{id, name, summary, input, status, result}``, its status
  ``running``, ``done`` or ``failed``;
- the plan is ``{explanation, steps: [{text, status}]}``, a step's status
  ``pending``, ``inProgress`` or ``completed``;
- a steer the turn took is a ``steers`` entry with no ``pending`` flag.

Anything else in a snapshot is ignored rather than guessed at.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

RUNNING_PHASES = frozenset({"queued", "streaming"})
"""The phases of a turn that has not ended; every other phase is a turn that has."""

_TOOL_STATUS = {"running": "in_progress", "pending": "pending", "done": "completed", "failed": "failed"}
_PLAN_STATUS = {"pending": "pending", "inProgress": "in_progress", "completed": "completed"}

_TOOL_KINDS: dict[str, str] = {
    **dict.fromkeys(
        ("bash", "shell", "exec", "exec_command", "local_shell", "run_command", "terminal", "powershell"), "execute"
    ),
    **dict.fromkeys(("read", "read_file", "view", "cat", "ls", "list_dir", "notebookread"), "read"),
    **dict.fromkeys(("write", "write_file", "edit", "edit_file", "multiedit", "apply_patch", "notebookedit"), "edit"),
    **dict.fromkeys(("grep", "glob", "search", "find", "websearch", "web_search", "codebase_search"), "search"),
    **dict.fromkeys(("webfetch", "web_fetch", "fetch"), "fetch"),
    **dict.fromkeys(("todowrite", "todo_write", "update_plan", "think"), "think"),
}
"""ACP's ``ToolKind`` for the tool names the agents Mirasim runs call most, matched without case.

Only an icon rides on it, so anything unnamed here is ``other`` rather than a guess."""


def is_running(snapshot: dict[str, Any]) -> bool:
    return snapshot.get("phase") in RUNNING_PHASES


def tool_kind(name: Any) -> str:
    return _TOOL_KINDS.get(name.strip().lower(), "other") if isinstance(name, str) else "other"


@dataclass
class SessionView:
    """One session's snapshot as the host's frames build it, and the sequence number it stands at."""

    snapshot: dict[str, Any] = field(default_factory=dict)
    seq: int | None = None
    """``None`` until a snapshot arrives, and again after a gap: patches are then ignored until one does."""

    def reset(self, snapshot: Any, seq: Any) -> None:
        """Take a ``snapshot`` frame's state as the whole of what is known."""
        self.snapshot = dict(snapshot) if isinstance(snapshot, dict) else {}
        self.seq = seq if _is_int(seq) else None

    def apply(self, frame: dict[str, Any]) -> bool:
        """Apply one ``session`` frame; ``False`` when it does not follow the last, and a fresh snapshot is due.

        A ``full`` patch stands on its own; any other needs the state it was
        diffed against. Without ``fromSeq`` (no cadence asked for) every frame is
        one patch and follows the last by one.
        """
        patch = frame.get("patch")
        seq = frame.get("seq")
        if not isinstance(patch, dict) or not _is_int(seq):
            return True
        full = patch.get("full")
        if isinstance(full, dict):
            self.snapshot = dict(full)
        else:
            start = frame.get("fromSeq")
            if self.seq is None or (start if _is_int(start) else seq - 1) != self.seq:
                self.seq = None
                return False
            snapshot = dict(self.snapshot)
            for key, added in (("text", patch.get("appendText")), ("reasoning", patch.get("appendReasoning"))):
                if isinstance(added, str) and added:
                    held = snapshot.get(key)
                    snapshot[key] = (held if isinstance(held, str) else "") + added
            changed = patch.get("set")
            if isinstance(changed, dict):
                snapshot.update(changed)
            lists = patch.get("lists")
            for key, spec in lists.items() if isinstance(lists, dict) else ():
                if not isinstance(spec, dict) or (isinstance(changed, dict) and key in changed):
                    continue
                held = self.snapshot.get(key)
                length = spec.get("len")
                items = list(held if isinstance(held, list) else [])[: length if _is_int(length) else None]
                at = spec.get("at")
                for index, item in at.items() if isinstance(at, dict) else ():
                    position = int(index) if isinstance(index, str) and index.isdigit() else None
                    if position is not None:
                        items.extend([None] * (position + 1 - len(items)))
                        items[position] = item
                snapshot[key] = items
            self.snapshot = snapshot
        self.seq = seq
        return True


@dataclass
class TurnStream:
    """What of one turn the ACP client has been shown, so a later snapshot sends only what is new.

    Keyed by the turn's ``taskId``: a snapshot of another turn starts over.
    """

    task_id: str | None = None
    text: str = ""
    reasoning: str = ""
    tools: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    """``id`` -> the ``(status, result)`` last sent for it."""
    plan: str | None = None
    sub_agents: dict[str, Any] = field(default_factory=dict)
    artifacts: set[str] = field(default_factory=set)
    steers: int = 0
    ended: bool = False
    """The turn's end was reported; frames for it that arrive later add nothing."""
    _rewrites_logged: bool = False

    def updates(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        """The ACP ``session/update`` payloads that bring the client level with ``snapshot``.

        In the order a turn usually produces them within one merged frame:
        steers taken, reasoning, the text that leads into a tool call, the tool
        calls themselves, then the plan, sub-agents and files delivered.
        """
        task_id = _text(snapshot.get("taskId")) or None
        if task_id != self.task_id:
            self.start(task_id)
        if self.ended:
            return []
        out: list[dict[str, Any]] = []
        out += self._steers(snapshot.get("steers"))
        if delta := self._grown("reasoning", snapshot.get("reasoning")):
            out.append(_chunk("agent_thought_chunk", delta))
        if delta := self._grown("text", snapshot.get("text")):
            out.append(_chunk("agent_message_chunk", delta))
        out += self._tools(snapshot.get("toolCalls"))
        out += self._plan(snapshot.get("plan"))
        out += self._sub_agents(snapshot.get("subAgents"))
        out += self._artifacts(snapshot.get("artifacts"))
        return out

    def start(self, task_id: str | None) -> None:
        """Forget what was shown, for the turn ``task_id`` that begins now."""
        self.task_id = task_id
        self.text = ""
        self.reasoning = ""
        self.tools = {}
        self.plan = None
        self.sub_agents = {}
        self.artifacts = set()
        self.steers = 0
        self.ended = False

    def _grown(self, key: str, value: Any) -> str:
        """What ``value`` adds to the text of ``key`` already sent, or nothing.

        Within a turn the host only appends. A value that does not extend what
        was sent was rewritten under the client -- which cannot take text back
        -- so it is adopted silently, logged once, and the stream goes on from it.
        """
        now = value if isinstance(value, str) else ""
        sent: str = getattr(self, key)
        if now == sent:
            return ""
        setattr(self, key, now)
        if now.startswith(sent):
            return now[len(sent) :]
        if not self._rewrites_logged:
            self._rewrites_logged = True
            logger.debug("Mirasim adapter: a turn's {} was rewritten, not extended; going on from the new text", key)
        return ""

    def _steers(self, items: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        landed = (
            [item for item in items if isinstance(item, dict) and not item.get("pending")]
            if isinstance(items, list)
            else []
        )
        for item in landed[self.steers :]:
            if said := _text(item.get("text")):
                out.append(_chunk("user_message_chunk", said))
        self.steers = max(self.steers, len(landed))
        return out

    def _tools(self, calls: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for call in calls if isinstance(calls, list) else ():
            call_id = _text(call.get("id")) if isinstance(call, dict) else ""
            if not call_id:
                continue
            state = (call.get("status"), call.get("result"))
            if call_id not in self.tools:
                out.append(_tool_call(call))
            elif self.tools[call_id] != state:
                out.append(_tool_update(call_id, *state))
            self.tools[call_id] = state
        return out

    def _plan(self, plan: Any) -> list[dict[str, Any]]:
        steps = plan.get("steps") if isinstance(plan, dict) else None
        entries = [
            {"content": said, "priority": "medium", "status": _PLAN_STATUS.get(step.get("status"), "pending")}
            for step in (steps if isinstance(steps, list) else ())
            if isinstance(step, dict) and (said := _text(step.get("text")))
        ]
        signature = json.dumps(entries, sort_keys=True)
        if signature == (self.plan or json.dumps([])):
            return []
        self.plan = signature
        return [{"sessionUpdate": "plan", "entries": entries}]

    def _sub_agents(self, items: Any) -> list[dict[str, Any]]:
        """Each sub-agent the turn started, as a tool call of its own that settles when the sub-agent does."""
        out: list[dict[str, Any]] = []
        for item in items if isinstance(items, list) else ():
            sub_id = _text(item.get("id")) if isinstance(item, dict) else ""
            if not sub_id:
                continue
            status = item.get("status")
            call_id = f"subagent-{sub_id}"
            if sub_id not in self.sub_agents:
                title = next(
                    (_text(item.get(k)) for k in ("description", "title", "name", "agent") if _text(item.get(k))), ""
                )
                out.append(
                    {
                        "sessionUpdate": "tool_call",
                        "toolCallId": call_id,
                        "title": title or "Sub-agent",
                        "kind": "other",
                        "status": _TOOL_STATUS.get(status, "in_progress"),
                    }
                )
            elif self.sub_agents[sub_id] != status:
                out.append(
                    {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": call_id,
                        "status": _TOOL_STATUS.get(status, "in_progress"),
                    }
                )
            self.sub_agents[sub_id] = status
        return out

    def _artifacts(self, items: Any) -> list[dict[str, Any]]:
        """Each file the turn delivered, as a link the client can open -- the way Mirasim's own ACP sends one."""
        out: list[dict[str, Any]] = []
        for item in items if isinstance(items, list) else ():
            path = _text(item.get("path")) if isinstance(item, dict) else ""
            if not path or path in self.artifacts or not Path(path).is_absolute():
                continue
            self.artifacts.add(path)
            link: dict[str, Any] = {
                "type": "resource_link",
                "uri": Path(path).as_uri(),
                "name": _text(item.get("fileName")) or Path(path).name,
            }
            if mime := _text(item.get("mime")):
                link["mimeType"] = mime
            if _is_int(item.get("fileSize")):
                link["size"] = item["fileSize"]
            out.append({"sessionUpdate": "agent_message_chunk", "content": link})
        return out


def replay(turn: dict[str, Any]) -> list[dict[str, Any]]:
    """One settled turn of a session's history, as the updates that would have drawn it live.

    The prompt goes first as the user's message, then the reasoning and the text
    in the order the turn's ``segments`` interleaved them, any tool calls the
    history kept before the last stretch of text, and the files it delivered.
    """
    out: list[dict[str, Any]] = []
    if prompt := _text(turn.get("prompt")):
        out.append(_chunk("user_message_chunk", prompt))
    text = turn.get("text") if isinstance(turn.get("text"), str) else ""
    reasoning = turn.get("reasoning") if isinstance(turn.get("reasoning"), str) else ""
    pieces: list[tuple[str, str]] = []
    segments = turn.get("segments")
    for segment in segments if isinstance(segments, list) else ():
        if not isinstance(segment, dict) or not _is_int(segment.get("from")) or not _is_int(segment.get("to")):
            continue
        source = {"thinking": ("agent_thought_chunk", reasoning), "text": ("agent_message_chunk", text)}.get(
            segment.get("kind")
        )
        if source is not None and (piece := source[1][segment["from"] : segment["to"]]):
            pieces.append((source[0], piece))
    if not pieces:
        pieces = [
            (kind, body) for kind, body in (("agent_thought_chunk", reasoning), ("agent_message_chunk", text)) if body
        ]
    calls = [
        _tool_call(call, settled=True)
        for call in turn.get("toolCalls") or ()
        if isinstance(call, dict) and _text(call.get("id"))
    ]
    last_text = max(
        (index for index, (kind, _) in enumerate(pieces) if kind == "agent_message_chunk"), default=len(pieces)
    )
    for index, (kind, piece) in enumerate(pieces):
        if index == last_text:
            out += calls
            calls = []
        out.append(_chunk(kind, piece))
    out += calls
    out += TurnStream()._artifacts(turn.get("artifacts"))
    return out


def _tool_call(call: dict[str, Any], *, settled: bool = False) -> dict[str, Any]:
    status = call.get("status")
    update: dict[str, Any] = {
        "sessionUpdate": "tool_call",
        "toolCallId": _text(call.get("id")),
        "title": _text(call.get("summary")) or _text(call.get("name")) or "Tool",
        "kind": tool_kind(call.get("name")),
        "status": _TOOL_STATUS.get(status, "completed" if settled else "in_progress"),
    }
    if call.get("input") is not None:
        update["rawInput"] = call["input"]
    if result := _result_text(call.get("result")):
        update["content"] = [_text_content(result)]
    return update


def _tool_update(call_id: str, status: Any, result: Any) -> dict[str, Any]:
    update: dict[str, Any] = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": call_id,
        "status": _TOOL_STATUS.get(status, "in_progress"),
    }
    if said := _result_text(result):
        update["content"] = [_text_content(said)]
    return update


def _result_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if result is None:
        return ""
    try:
        return json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(result)


def _text_content(text: str) -> dict[str, Any]:
    return {"type": "content", "content": {"type": "text", "text": text}}


def _chunk(kind: str, text: str) -> dict[str, Any]:
    return {"sessionUpdate": kind, "content": {"type": "text", "text": text}}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


__all__ = ["RUNNING_PHASES", "SessionView", "TurnStream", "is_running", "replay", "tool_kind"]
