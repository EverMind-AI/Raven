"""todo tool -- the agent's own multi-step task checklist, saved per session.

The checklist uses the product plugin seams. Contract choices worth
restating, because they look like oversights:

*One tool, two actions.* ``{"action": "read"}`` returns the latest saved plan
without touching it; ``{"action": "write", "todos": [...]}`` replaces it, and
``[]`` clears it. A separate read tool would be a second definition to keep
in sync and a second name for the model to forget. A call in the retired
``todowrite`` shape (a bare ``todos`` list, no action) is a write: the
registry runs ``cast_params`` before validation, so the old habit lands on
the new tool. The old *name* is not registered: the trunk registry has no
hidden aliases, and a second visible definition is what the one-tool rule
exists to avoid.

*Whole-list replacement, not per-item patching.* Every write carries the full
list. A patch API needs the model to track item identity across turns, and a
model that loses an id then edits the wrong row silently corrupts the list.
Replacement makes each call self-describing, so the worst a confused model can
do is restate the list badly -- visible immediately, fixed by the next call.

*The plan is the model's.* Statuses are recorded as sent; nothing here audits
whether ``completed`` is true or reopens items. A second ``in_progress``
warns, it does not fail: rejecting the call would burn a turn and lose the
status update that came with it.

*Durable before acknowledged.* A write is saved under Agent home
(``<home>/todos/<channel>/<chat_id>.json``) before the success receipt is
returned, so it survives whatever happens to the rest of the tool batch or
the process. The saved record, not the transcript, is the source of truth:
the transcript is never mined for a plan, so text a user pasted from another
conversation cannot become one.

*Restored only when hidden.* The fork once re-rendered the plan onto every
request; that moved the request prefix every call and kept a stale copy to
sync. Now a write's receipt in the transcript IS the plan the model works to,
and only when no message still shows the current revision (the receipt was
elided to fit the window, the head was summarized, the process restarted
without the transcript) does the code-flow hook append one restore snapshot
to the last message -- a durable part of the transcript, not a per-request
reminder. See :meth:`TodoStore.snapshot_if_hidden`.

*Bound by the hook, not by the tool.* A tool does not see the session it runs
for; the code-flow hook does, and binds the store at the turn's inbound
phase and checks the actual session before the first model call, including
turns that skip inbound. The state rides a ``ContextVar``, so concurrent
sessions bind their own. Without a binding the tool refuses the call.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from raven.contracts.tool import Tool, ToolResult
from raven.utils.atomic_io import atomic_replace
from raven.utils.paths import safe_path_segment

TOOL_NAME = "todo"
#: The retired name. Not registered; kept so prompts and tests can say what
#: shape ``cast_params`` still accepts.
LEGACY_TOOL_NAME = "todowrite"

_STATUSES = ("pending", "in_progress", "completed", "cancelled")
_PRIORITIES = ("high", "medium", "low")
_STATUS_MARK = {
    "pending": "[ ]",
    "in_progress": "[~]",
    "completed": "[x]",
    "cancelled": "[-]",
}

_SCHEMA_VERSION = 1
STATE_SUBDIR = "todos"


# --------------------------------------------------------------------------- #
# record location                                                               #
# --------------------------------------------------------------------------- #


def plan_path(home: Path | str, session_key: str) -> Path:
    """Where a session's record lives: ``<home>/todos/<channel>/<chat_id>.json``.

    Under Agent home, never in the working directory: the plan is the agent's
    own state, and a repository must not grow a file the task did not ask for.
    Bucketed like the session transcript (``channel:chat_id``), and by the
    session alone, so the deletion observer -- which knows the key and nothing
    else -- can find the file to remove.
    """
    channel, _, chat_id = session_key.partition(":")
    if not chat_id:
        channel, chat_id = "session", channel
    return Path(home) / STATE_SUBDIR / safe_path_segment(channel) / f"{safe_path_segment(chat_id)}.json"


def discard_saved_plan(home: Path | str, session_key: str) -> bool:
    """Remove a session's record. True when a file was removed."""
    try:
        plan_path(home, session_key).unlink()
    except FileNotFoundError:
        return False
    return True


# --------------------------------------------------------------------------- #
# items, revision, rendering                                                    #
# --------------------------------------------------------------------------- #


def revision(items: list[dict[str, Any]]) -> str:
    encoded = json.dumps(items, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def normalize_todos(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("'todos' must be an array of checklist items.")
    items = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("every checklist item must be an object with 'content' and 'status'.")
        content = str(entry.get("content", "")).strip()
        if not content:
            raise ValueError("every checklist item needs a non-empty 'content'.")
        status = str(entry.get("status", "pending"))
        if status not in _STATUSES:
            raise ValueError(f"invalid status {status!r}; use one of {', '.join(_STATUSES)}.")
        priority = str(entry.get("priority", "medium"))
        if priority not in _PRIORITIES:
            priority = "medium"
        items.append({"content": content, "status": status, "priority": priority})
    return items


def render(items: list[dict[str, Any]]) -> str:
    if not items:
        return "(empty)"
    return "\n".join(f"{_STATUS_MARK.get(str(item.get('status')), '[?]')} {item.get('content', '')}" for item in items)


# --------------------------------------------------------------------------- #
# receipt codec                                                                 #
# --------------------------------------------------------------------------- #
#
# The visibility check reads these shapes back out of the working window, so a
# wording change must land in the formatter and the marker regex together. The
# revision in every receipt is what makes "the model can still see the CURRENT
# plan" a string search rather than a guess.

_READ_NONE = "No checklist is recorded for this session"
_SNAPSHOT_NONE = "Restored checklist: none is recorded."
SNAPSHOT_HEAD = "<system-reminder>\nRestored checklist"

_MARKER_RE = re.compile(
    r"Checklist updated \((?P<w_count>\d+) items, revision (?P<w_rev>[0-9a-f]{16})\)"
    r"|Checklist \((?P<r_count>\d+) items, revision (?P<r_rev>[0-9a-f]{16})\)"
    r"|" + re.escape(_READ_NONE) + r"|Restored checklist \(revision (?P<s_rev>[0-9a-f]{16}), (?P<s_count>\d+) items\)"
    r"|" + re.escape(_SNAPSHOT_NONE)
)


def write_receipt(items: list[dict[str, Any]]) -> str:
    return f"Checklist updated ({len(items)} items, revision {revision(items)}):\n{render(items)}"


def read_receipt(items: list[dict[str, Any]] | None) -> str:
    if items is None:
        return f"{_READ_NONE}. Use action 'write' to create one."
    return f"Checklist ({len(items)} items, revision {revision(items)}):\n{render(items)}"


def snapshot_text(items: list[dict[str, Any]] | None, note: str | None) -> str:
    if items is None:
        head, body = _SNAPSHOT_NONE, ""
    else:
        head = f"Restored checklist (revision {revision(items)}, {len(items)} items) saved by the todo tool:"
        body = json.dumps(items, ensure_ascii=False) + "\n"
    note_line = f"Note: {note}\n" if note else ""
    return (
        "<system-reminder>\n"
        f"{head}\n{body}{note_line}"
        "This was inserted by the system because the saved plan was no longer visible in context; "
        'it is not a new user instruction. Read the latest with the todo tool (action "read"); '
        'update it with action "write" and the full list.\n'
        "</system-reminder>"
    )


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(block.get("text", "")) for block in content if isinstance(block, dict))
    return ""


def latest_shown(messages: list[dict[str, Any]]) -> tuple[str, str | None] | None:
    """``(kind, revision)`` of the last plan-bearing marker the model can still see.

    Write and read receipts count only on tool messages (a user can paste the
    words); a restore snapshot counts wherever it landed, since the hook
    appends it to whatever message was last. An elided or summarized message
    carries no marker, which is exactly how a hidden plan is detected.
    """
    latest: tuple[str, str | None] | None = None
    for message in messages:
        text = _message_text(message)
        if not text:
            continue
        is_tool = message.get("role") == "tool"
        for match in _MARKER_RE.finditer(text):
            if match.group("w_rev") is not None:
                if is_tool:
                    latest = ("write", match.group("w_rev"))
            elif match.group("r_rev") is not None:
                if is_tool:
                    latest = ("read", match.group("r_rev"))
            elif match.group(0) == _READ_NONE:
                if is_tool:
                    latest = ("read", None)
            elif match.group("s_rev") is not None:
                latest = ("snapshot", match.group("s_rev"))
            else:
                latest = ("snapshot", None)
    return latest


# --------------------------------------------------------------------------- #
# on-disk record                                                                #
# --------------------------------------------------------------------------- #
#
# Two record shapes share one file:
#   plan        -- the latest saved checklist (``items`` is a list);
#   quarantine  -- the latest record was unreadable and has been set aside as
#                  evidence (``quarantined`` is true, ``items`` is null).
# The quarantine record exists so a restart never mistakes "record set aside"
# for "session never had a record".


def _plan_doc(
    session_key: str, workdir: str | None, items: list[dict[str, Any]], *, sequence: int, note: str | None
) -> dict[str, Any]:
    return {
        "schema": _SCHEMA_VERSION,
        "session_key": session_key,
        "workdir": workdir,
        "revision": revision(items),
        "sequence": sequence,
        "updated_at": datetime.now().isoformat(),
        "recovery_note": note,
        "items": items,
    }


def _quarantine_doc(session_key: str, workdir: str | None, *, evidence: str, note: str) -> dict[str, Any]:
    return {
        "schema": _SCHEMA_VERSION,
        "session_key": session_key,
        "workdir": workdir,
        "quarantined": True,
        "evidence": evidence,
        "updated_at": datetime.now().isoformat(),
        "recovery_note": note,
        "items": None,
    }


def _optional_str(doc: dict[str, Any], key: str) -> str | None:
    value = doc.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{key} must be a string or null")
    return value


def validate_record(raw: bytes, *, session_key: str, workdir: str | None) -> dict[str, Any]:
    """Decode and check a record; raises ValueError describing the first defect.

    The working directory is checked only when both sides know it: a record
    written for another checkout is another session's plan wearing this key.
    """
    try:
        doc = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ValueError(f"not UTF-8 ({exc.reason})") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"not JSON ({exc.msg})") from exc
    if not isinstance(doc, dict):
        raise ValueError("not a JSON object")
    if doc.get("schema") != _SCHEMA_VERSION:
        raise ValueError(f"unsupported schema {doc.get('schema')!r}")
    if doc.get("session_key") != session_key:
        raise ValueError(f"belongs to session {doc.get('session_key')!r}")
    recorded = _optional_str(doc, "workdir")
    if recorded is not None and workdir is not None and recorded != workdir:
        raise ValueError(f"belongs to working directory {recorded!r}")
    _optional_str(doc, "recovery_note")
    if doc.get("quarantined") is True:
        if doc.get("items") is not None:
            raise ValueError("quarantine record carries items")
        if not isinstance(doc.get("evidence"), str) or not doc.get("recovery_note"):
            raise ValueError("quarantine record lacks evidence or note")
        return doc
    if doc.get("quarantined") not in (None, False):
        raise ValueError("quarantined must be a boolean")
    items = normalize_todos(doc.get("items"))
    if doc.get("items") != items:
        raise ValueError("items are not in normalized form")
    if doc.get("revision") != revision(items):
        raise ValueError("revision does not match items")
    sequence = doc.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise ValueError(f"sequence must be a non-negative integer, got {sequence!r}")
    return doc


# --------------------------------------------------------------------------- #
# store                                                                         #
# --------------------------------------------------------------------------- #


@dataclass
class _Bound:
    session_key: str | None = None
    workdir: str | None = None
    path: Path | None = None
    items: list[dict[str, Any]] = field(default_factory=list)
    initialized: bool = False
    sequence: int = 0
    recovery_note: str | None = None
    writes: int = 0


class TodoStore:
    """Session-scoped checklist state with a durable per-session record.

    One store per Agent home, shared by the tool and the hook: the hook binds
    the turn's session (:meth:`bind`), the tool reads and writes through the
    binding, the hook restores the plan into context when it has gone out of
    view (:meth:`snapshot_if_hidden`), and the deletion observer discards the
    record (:meth:`discard`). ``home=None`` keeps a bound session in memory;
    an unbound store cannot read or write a checklist.
    """

    def __init__(self, home: Path | str | None = None) -> None:
        self._home = Path(home) if home is not None else None
        self._state: ContextVar[_Bound | None] = ContextVar("code_flow_todo_state", default=None)

    @property
    def is_bound(self) -> bool:
        return self._state.get() is not None

    def _bound_state(self) -> _Bound:
        state = self._state.get()
        if state is None:
            raise RuntimeError("the checklist is not bound to a session")
        return state

    @property
    def home(self) -> Path | None:
        return self._home

    @property
    def items(self) -> list[dict[str, Any]]:
        return self._bound_state().items

    @property
    def initialized(self) -> bool:
        return self._bound_state().initialized

    @property
    def revision(self) -> str:
        return revision(self.items)

    @property
    def recovery_note(self) -> str | None:
        return self._bound_state().recovery_note

    @property
    def writes(self) -> int:
        return self._bound_state().writes

    @property
    def path(self) -> Path | None:
        state = self._state.get()
        return state.path if state is not None else None

    @property
    def session_key(self) -> str | None:
        state = self._state.get()
        return state.session_key if state is not None else None

    def bind(self, session_key: str, workdir: Path | str | None) -> None:
        """Attach the store to a session for the current turn.

        The saved record wins when present (a quarantine record included) --
        also over an empty transcript, because a process killed mid-turn
        leaves exactly that behind and the plan the model wrote is the one
        thing that survived. An unreadable record is set aside as evidence
        and replaced by a quarantine record, so the loss is reported now and
        on every later restart until the model writes again -- never
        silently replaced by an older plan. A new session key selects a separate
        record; returning to the old key resumes its checklist.
        """
        self._state.set(None)
        if not session_key:
            raise ValueError("a checklist requires a non-empty session key")
        state = _Bound(session_key=session_key, workdir=str(workdir) if workdir is not None else None)
        if self._home is not None:
            state.path = plan_path(self._home, session_key)
            self._load(state, state.path)
        self._state.set(state)

    def replace(self, items: list[dict[str, Any]]) -> None:
        """Save a normalized list; raises before touching memory if the write fails."""
        state = self._bound_state()
        items = deepcopy(items)
        sequence = state.sequence + 1
        if state.path is not None:
            doc = _plan_doc(state.session_key or "", state.workdir, items, sequence=sequence, note=None)
            atomic_replace(state.path, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
        state.items = items
        state.initialized = True
        state.sequence = sequence
        state.recovery_note = None
        state.writes += 1

    def snapshot_if_hidden(self, messages: list[dict[str, Any]]) -> str | None:
        """The restore snapshot to append when no message still shows the current plan.

        A plan is visible when the last plan-bearing marker in the window (a
        write receipt, a read receipt, or an earlier snapshot) carries the
        current revision. Anything else -- the receipt elided, the head
        summarized, a later write whose receipt is gone -- means the model may
        be working to a stale plan, so the saved one is restated once; the
        snapshot itself then counts as visible until it, too, is elided.
        Nothing recorded and nothing to report means no snapshot at all.
        """
        state = self._bound_state()
        if not state.initialized and not state.recovery_note:
            return None
        current = revision(state.items) if state.initialized else None
        latest = latest_shown(messages)
        if latest is not None and latest[1] == current:
            return None
        return snapshot_text(deepcopy(state.items) if state.initialized else None, state.recovery_note)

    def render(self) -> str:
        return render(self.items)

    def discard(self, session_key: str) -> bool:
        """Remove a session's record (the session was deleted or cleared)."""
        if self._home is None:
            return False
        return discard_saved_plan(self._home, session_key)

    def _load(self, state: _Bound, path: Path) -> None:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning("todo: saved plan {} unreadable: {}", path, exc)
            state.recovery_note = f"the saved checklist could not be read ({exc}); no checklist is currently recorded"
            return
        try:
            doc = validate_record(raw, session_key=state.session_key or "", workdir=state.workdir)
        except ValueError as exc:
            self._quarantine(state, path, raw, str(exc))
            return
        state.recovery_note = doc.get("recovery_note") or None
        if doc.get("quarantined") is True:
            return
        state.items = doc["items"]
        state.initialized = True
        state.sequence = doc["sequence"]

    def _quarantine(self, state: _Bound, path: Path, raw: bytes, reason: str) -> None:
        """Set an unreadable record aside and leave a blocking state behind.

        The evidence copy is written first, then the record is replaced in
        place by a quarantine marker. Until that replacement lands the
        unreadable file itself stays at the record path, so a restart hits
        the same wall instead of finding "no record". Evidence is named by
        content hash, so a retry after a failed marker write does not pile
        up copies.
        """
        digest = hashlib.sha256(raw).hexdigest()[:12]
        evidence = path.with_name(f"{path.stem}.corrupt-{digest}.json")
        note = (
            f"the saved checklist was unreadable ({reason}) and was set aside as {evidence.name}; "
            "no checklist is currently recorded"
        )
        state.recovery_note = note
        try:
            evidence.write_bytes(raw)
        except OSError as exc:
            logger.warning("todo: saved plan {} unreadable ({}); evidence copy failed: {}", path, reason, exc)
            state.recovery_note = (
                f"the saved checklist is unreadable ({reason}) and could not be set aside ({exc}); "
                "no checklist is currently recorded"
            )
            return
        marker = _quarantine_doc(state.session_key or "", state.workdir, evidence=evidence.name, note=note)
        try:
            atomic_replace(path, json.dumps(marker, ensure_ascii=False, indent=2) + "\n")
        except OSError as exc:
            logger.warning("todo: quarantine record for {} not saved ({}); unreadable file left in place", path, exc)
            return
        logger.warning("todo: saved plan {} unreadable ({}); kept as {}", path, reason, evidence.name)


class TodoStores:
    """One store per Agent home, process-wide, so the tool factory and the
    hook factory -- called separately by the registry -- meet on one object."""

    def __init__(self) -> None:
        self._stores: dict[str, TodoStore] = {}
        self._lock = threading.Lock()

    def for_home(self, home: Path | str) -> TodoStore:
        key = str(Path(home).expanduser().resolve())
        with self._lock:
            store = self._stores.get(key)
            if store is None:
                store = self._stores[key] = TodoStore(home)
            return store

    def clear(self) -> None:
        with self._lock:
            self._stores.clear()


STORES = TodoStores()


# --------------------------------------------------------------------------- #
# tool                                                                          #
# --------------------------------------------------------------------------- #


class TodoTool(Tool):
    """Read or replace the checklist for the current session."""

    def __init__(self, store: TodoStore | None = None) -> None:
        self._store = store if store is not None else TodoStore()

    @property
    def name(self) -> str:
        return TOOL_NAME

    @property
    def description(self) -> str:
        return (
            "Read or maintain Raven-Code's structured checklist for the current session. "
            "Checklists are isolated by conversation and saved before a write is acknowledged. Use it to "
            "plan multi-step work and to keep the user informed of progress.\n"
            "action 'read' returns the latest saved checklist without changing it. "
            "action 'write' replaces the checklist with 'todos'; pass the ENTIRE list every "
            "time, and an empty list to clear it.\n"
            "Use it when the task needs 3+ distinct steps, when the user gives several "
            "tasks at once, or when new instructions arrive mid-task (capture them as items). "
            "Skip it for a single straightforward step or a purely informational question -- "
            "tracking that adds no value.\n"
            "Keep exactly one item 'in_progress' while work remains, and update status as you "
            "go rather than batching the updates at the end. Mark an item 'completed' only once "
            "the work is actually done and verified -- never on intent. If you are blocked, "
            "leave the item 'in_progress' and add a new item describing the blocker."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["read", "write"],
                    "description": "'read' returns the saved checklist; 'write' replaces it with 'todos'.",
                },
                "todos": {
                    "type": "array",
                    "description": "The complete checklist, replacing the previous one. Required for 'write'; "
                    "an empty array clears the checklist. Ignored for 'read'.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "The step, phrased as a specific action."},
                            "status": {"type": "string", "enum": list(_STATUSES), "description": "Step status."},
                            "priority": {
                                "type": "string",
                                "enum": list(_PRIORITIES),
                                "description": "Step priority. Defaults to 'medium'.",
                            },
                        },
                        "required": ["content", "status"],
                    },
                },
            },
            "required": ["action"],
        }

    def cast_params(self, params: dict[str, Any]) -> dict[str, Any]:
        # The retired ``todowrite`` shape has no action; a bare list is a write.
        # Runs before the registry validates, so the call is not refused first.
        if isinstance(params, dict) and "action" not in params and isinstance(params.get("todos"), list):
            return {**params, "action": "write"}
        return params

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        if not self._store.is_bound:
            return ToolResult(
                model_text="Error: the checklist is not bound to a session. Nothing was read or saved.",
                ok=False,
            )
        action = kwargs.get("action")
        if action == "read":
            return self._read()
        if action != "write":
            return f"Error: 'action' must be 'read' or 'write', got {action!r}."
        if "todos" not in kwargs:
            return (
                "Error: action 'write' needs 'todos' (the complete list; [] clears it). "
                "Use action 'read' to view the current checklist."
            )
        try:
            items = normalize_todos(kwargs.get("todos"))
        except ValueError as exc:
            return f"Error: {exc}"
        try:
            self._store.replace(items)
        except OSError as exc:
            logger.warning("todo: checklist not saved: {}", exc)
            return f"Error: checklist not saved ({exc}). The previous checklist is unchanged."

        active = sum(1 for item in items if item["status"] == "in_progress")
        remaining = any(item["status"] in ("pending", "in_progress") for item in items)
        notes = []
        if active > 1:
            notes.append(f"{active} items are 'in_progress'; keep exactly one and re-send the list.")
        elif active == 0 and remaining:
            notes.append("No item is 'in_progress'; mark the one you are starting.")
        body = write_receipt(items)
        if notes:
            body = f"{body}\n\nNote: {' '.join(notes)}"
        return ToolResult(model_text=body, display_text=self._store.render())

    def _read(self) -> ToolResult:
        note = self._store.recovery_note
        if not self._store.initialized:
            body, display = read_receipt(None), "(no checklist)"
        else:
            body, display = read_receipt(self._store.items), self._store.render()
        if note:
            body = f"{body}\n\nNote: {note}"
        return ToolResult(model_text=body, display_text=display)

    def display_call(self, args: dict[str, Any]) -> str | None:
        if args.get("action") == "read":
            return "read checklist"
        todos = args.get("todos")
        if not isinstance(todos, list):
            return None
        if not todos:
            return "checklist cleared"
        active = next(
            (t.get("content") for t in todos if isinstance(t, dict) and t.get("status") == "in_progress"),
            None,
        )
        done = sum(1 for t in todos if isinstance(t, dict) and t.get("status") == "completed")
        head = f"{done}/{len(todos)} done"
        return f"{head} - {active}" if active else head


__all__ = [
    "LEGACY_TOOL_NAME",
    "SNAPSHOT_HEAD",
    "STATE_SUBDIR",
    "STORES",
    "TOOL_NAME",
    "TodoStore",
    "TodoStores",
    "TodoTool",
    "discard_saved_plan",
    "latest_shown",
    "normalize_todos",
    "plan_path",
    "read_receipt",
    "render",
    "revision",
    "snapshot_text",
    "validate_record",
    "write_receipt",
]
