"""todowrite tool — the agent's own multi-step task checklist.

Distinct from :mod:`raven.agent.tools.spawn`. A *checklist* is the agent
narrating its own plan to itself and to the user: pure state, no side effects,
no other process involved. *Spawning* delegates work to a subagent. Both
reference "tasks", which is why they get conflated; opencode ships them as
separate tools (``todowrite`` vs ``task``) and codex does the same
(``update_plan`` vs its spawn family). This is the checklist half.

Two contract choices worth stating, because both look like oversights:

*Whole-list replacement, not per-item patching.* Every call carries the full
list. A patch API (``add_item`` / ``set_status(id, ...)``) needs the model to
track item identity across turns, and a model that loses an id then edits the
wrong row silently corrupts the list. Replacement makes each call
self-describing, so the worst a confused model can do is restate the list
badly -- visible immediately, and fixed by the next call. Both opencode and
codex landed on the same shape.

*A second ``in_progress`` warns, it does not fail.* The one-at-a-time rule is
guidance the model should follow, but rejecting the call would burn a turn and
lose the status update that came with it -- and weaker models trip this rule
most often, so the failure mode would be worst exactly where the tool is
supposed to help. The list is accepted and the warning rides back in the
model-facing text, which is enough for the model to self-correct on its next
call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from raven.agent.tools.base import Tool, ToolResult

_STATUSES = ("pending", "in_progress", "completed", "cancelled")
_PRIORITIES = ("high", "medium", "low")

_STATUS_MARK = {
    "pending": "[ ]",
    "in_progress": "[~]",
    "completed": "[x]",
    "cancelled": "[-]",
}


@dataclass
class TodoStore:
    """Session-scoped checklist state.

    Injected rather than held on the tool so the agent loop can read the
    current list for transcript rendering and session capture without going
    through a tool call. One store per agent: a subagent builds its own
    registry and therefore its own store, which is the isolation we want -- a
    subagent's private steps have no business appearing in its parent's list.
    """

    items: list[dict[str, Any]] = field(default_factory=list)

    def replace(self, items: list[dict[str, Any]]) -> None:
        self.items = items

    def render(self) -> str:
        if not self.items:
            return "(empty)"
        return "\n".join(
            f"{_STATUS_MARK.get(str(item.get('status')), '[?]')} {item.get('content', '')}"
            for item in self.items
        )


class TodoWriteTool(Tool):
    """Create and maintain the checklist for the current session."""

    def __init__(self, store: TodoStore) -> None:
        self._store = store

    @property
    def name(self) -> str:
        return "todowrite"

    @property
    def description(self) -> str:
        return (
            "Create and maintain a structured checklist for the current session. Use it to "
            "plan multi-step work and to keep the user informed of progress.\n"
            "Use it when the task needs 3+ distinct steps, when the user gives several "
            "tasks at once, or when new instructions arrive mid-task (capture them as items). "
            "Skip it for a single straightforward step or a purely informational question -- "
            "tracking that adds no value.\n"
            "Pass the ENTIRE list every call; it replaces the previous one. Keep exactly one "
            "item 'in_progress' while work remains, and update status as you go rather than "
            "batching the updates at the end. Mark an item 'completed' only once the work is "
            "actually done and verified -- never on intent. If you are blocked, leave the item "
            "'in_progress' and add a new item describing the blocker."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "The complete checklist, replacing the previous one.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "The step, phrased as a specific action.",
                            },
                            "status": {
                                "type": "string",
                                "enum": list(_STATUSES),
                                "description": "Step status.",
                            },
                            "priority": {
                                "type": "string",
                                "enum": list(_PRIORITIES),
                                "description": "Step priority. Defaults to 'medium'.",
                            },
                        },
                        "required": ["content", "status"],
                    },
                }
            },
            "required": ["todos"],
        }

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        raw = kwargs.get("todos")
        if not isinstance(raw, list):
            return "Error: 'todos' must be an array of checklist items."

        items: list[dict[str, Any]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                return "Error: every checklist item must be an object with 'content' and 'status'."
            content = str(entry.get("content", "")).strip()
            if not content:
                return "Error: every checklist item needs a non-empty 'content'."
            status = str(entry.get("status", "pending"))
            if status not in _STATUSES:
                return f"Error: invalid status {status!r}; use one of {', '.join(_STATUSES)}."
            priority = str(entry.get("priority", "medium"))
            if priority not in _PRIORITIES:
                priority = "medium"
            items.append({"content": content, "status": status, "priority": priority})

        self._store.replace(items)
        rendered = self._store.render()

        active = sum(1 for item in items if item["status"] == "in_progress")
        remaining = any(item["status"] in ("pending", "in_progress") for item in items)
        notes = []
        if active > 1:
            notes.append(f"{active} items are 'in_progress'; keep exactly one and re-send the list.")
        elif active == 0 and remaining:
            notes.append("No item is 'in_progress'; mark the one you are starting.")

        body = f"Checklist updated ({len(items)} items):\n{rendered}"
        if notes:
            body = f"{body}\n\nNote: {' '.join(notes)}"
        return ToolResult(model_text=body, display_text=rendered)

    def display_call(self, args: dict[str, Any]) -> str | None:
        todos = args.get("todos")
        if not isinstance(todos, list):
            return None
        active = next(
            (t.get("content") for t in todos if isinstance(t, dict) and t.get("status") == "in_progress"),
            None,
        )
        done = sum(1 for t in todos if isinstance(t, dict) and t.get("status") == "completed")
        head = f"{done}/{len(todos)} done"
        return f"{head} - {active}" if active else head
