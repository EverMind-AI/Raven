"""Task State storage, validation, mutation, and prompt rendering."""

from __future__ import annotations

import copy
import hashlib
import html
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from raven.utils.atomic_io import atomic_replace
from raven.utils.helpers import safe_filename

TASK_STATE_STATUSES = frozenset({"pending", "in_progress", "waiting", "blocked", "completed"})
_ITEM_FIELDS = frozenset({"title", "status", "requirements"})
_STATE_FIELDS = frozenset({"goal", "requirements", "items"})
_OPERATION_FIELDS = {
    "initialize": frozenset({"operation", "state"}),
    "add": frozenset({"operation", "item"}),
    "update": frozenset({"operation", "target", "item_number", "changes"}),
    "remove": frozenset({"operation", "item_number", "reason"}),
    "complete": frozenset({"operation", "item_number"}),
}
_KNOWN_OPERATION_FIELDS = frozenset().union(*_OPERATION_FIELDS.values())
_MAX_GOAL_CHARS = 4000
_MAX_TITLE_CHARS = 1000
_MAX_REQUIREMENT_CHARS = 2000
_MAX_REQUIREMENTS = 50
_MAX_ITEMS = 100


class TaskStateError(ValueError):
    """A user-correctable Task State validation or mutation error."""


@dataclass(frozen=True)
class TaskStateUpdate:
    """Result of one atomic state mutation."""

    state: dict[str, Any]
    revision: int


class TaskStateStore:
    """Crash-safe sidecar store keyed by the conversation session."""

    def __init__(self, workspace: Path):
        self.root = workspace / "task_states"

    def _path(self, session_key: str) -> Path:
        if not session_key:
            raise TaskStateError("A session key is required for Task State.")
        safe = "".join(
            character if character.isascii() and (character.isalnum() or character in "._-") else "_"
            for character in safe_filename(session_key)
        )
        safe = safe.strip("._")[:64] or "session"
        digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:16]
        return self.root / f"{safe}-{digest}.json"

    def load(self, session_key: str) -> tuple[dict[str, Any] | None, int]:
        state, revision, _, _ = self._load_record(session_key)
        return state, revision

    def _load_record(
        self,
        session_key: str,
    ) -> tuple[dict[str, Any] | None, int, list[dict[str, Any] | None], str | None]:
        path = self._path(session_key)
        if not path.exists():
            return None, 0, [], None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("the root record is not an object")
            state = raw.get("state")
            if state is not None:
                state = TaskStateManager._normalize_state(state)
            revision = raw.get("revision", 0)
            if not isinstance(revision, int) or revision < 0:
                raise ValueError("the revision is invalid")
            history = raw.get("history", [])
            if not isinstance(history, list):
                raise ValueError("the history record is invalid")
            history = [item if item is None else TaskStateManager._normalize_state(item) for item in history]
            mutation_group = raw.get("last_mutation_group")
            if mutation_group is not None and not isinstance(mutation_group, str):
                raise ValueError("the mutation group is invalid")
            return state, revision, history, mutation_group
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            # A broken sidecar must never take the session down: quarantine the
            # file for forensics and degrade to "not initialized" so the next
            # initialize self-heals. Every read path (loop projection, RPC
            # resume/undo, the tool) inherits this recovery.
            logger.warning(
                "Task State load failed for {}; quarantining the file: {}",
                session_key,
                exc,
            )
            self._quarantine(path)
            return None, 0, [], None

    @staticmethod
    def _quarantine(path: Path) -> None:
        try:
            path.replace(path.with_name(path.name + ".corrupt"))
        except OSError as exc:
            logger.warning("Task State quarantine failed for {}: {}", path, exc)

    def save(
        self,
        session_key: str,
        state: dict[str, Any],
        *,
        mutation_group: str | None = None,
    ) -> int:
        previous_state, previous_revision, history, previous_group = self._load_record(session_key)
        if mutation_group is None:
            history.append(previous_state)
        elif mutation_group != previous_group:
            history.append(previous_state)
        revision = previous_revision + 1
        payload = {
            "version": 1,
            "revision": revision,
            "updated_at": datetime.now().isoformat(),
            "state": state,
            "history": history[-50:],
            "last_mutation_group": mutation_group,
        }
        atomic_replace(
            self._path(session_key),
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )
        return revision

    def checkpoint(self, session_key: str, mutation_group: str | None) -> int:
        """Record the state at the beginning of one conversation turn."""
        if not mutation_group:
            _, revision = self.load(session_key)
            return revision

        state, revision, history, previous_group = self._load_record(session_key)
        if state is None:
            return revision
        if mutation_group == previous_group:
            return revision

        history.append(state)
        payload = {
            "version": 1,
            "revision": revision,
            "updated_at": datetime.now().isoformat(),
            "state": state,
            "history": history[-50:],
            "last_mutation_group": mutation_group,
        }
        atomic_replace(
            self._path(session_key),
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )
        return revision

    def undo(self, session_key: str) -> int:
        state, revision, history, _ = self._load_record(session_key)
        if not history:
            return revision
        previous_state = history.pop()
        next_revision = revision + 1
        payload = {
            "version": 1,
            "revision": next_revision,
            "updated_at": datetime.now().isoformat(),
            "state": previous_state,
            "history": history,
            "last_mutation_group": None,
        }
        atomic_replace(
            self._path(session_key),
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )
        return next_revision

    def clear(self, session_key: str) -> None:
        path = self._path(session_key)
        if path.exists():
            path.unlink()

    def copy(self, source_session_key: str, target_session_key: str) -> int:
        state, _, history, _ = self._load_record(source_session_key)
        if state is None:
            self.clear(target_session_key)
            return 0
        target_path = self._path(target_session_key)
        payload = {
            "version": 1,
            "revision": 1,
            "updated_at": datetime.now().isoformat(),
            "state": state,
            "history": history,
            "last_mutation_group": None,
        }
        atomic_replace(
            target_path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )
        return 1


class TaskStateManager:
    """Owns the session-scoped state and the visible-number addressing model."""

    def __init__(self, workspace: Path, store: TaskStateStore | None = None):
        self.store = store or TaskStateStore(workspace)

    def get(self, session_key: str) -> dict[str, Any] | None:
        state, _ = self.store.load(session_key)
        return state

    def revision(self, session_key: str) -> int:
        _, revision = self.store.load(session_key)
        return revision

    def apply(
        self,
        session_key: str,
        operations: list[dict[str, Any]],
        *,
        mutation_group: str | None = None,
    ) -> TaskStateUpdate:
        if not isinstance(operations, list) or not operations:
            raise TaskStateError("operations must be a non-empty list.")
        if any(not isinstance(operation, dict) for operation in operations):
            raise TaskStateError("Every operation must be an object.")

        for operation in operations:
            self._validate_operation_fields(operation)
        names = [operation.get("operation") for operation in operations]
        if "initialize" in names:
            if len(operations) != 1:
                raise TaskStateError("initialize replaces the whole state and must be called alone.")
            state = self._normalize_state(operations[0].get("state"))
            revision = self.store.save(session_key, state, mutation_group=mutation_group)
            return TaskStateUpdate(state=state, revision=revision)

        current, _ = self.store.load(session_key)
        if current is None:
            raise TaskStateError("Task State is not initialized. Call initialize first.")

        working = copy.deepcopy(current)
        original_items = {number: item for number, item in enumerate(working["items"], start=1)}
        for operation in operations:
            self._apply_operation(working, original_items, operation)
        state = self._normalize_state(working)
        revision = self.store.save(session_key, state, mutation_group=mutation_group)
        return TaskStateUpdate(state=state, revision=revision)

    def clear(self, session_key: str) -> None:
        self.store.clear(session_key)

    def copy(self, source_session_key: str, target_session_key: str) -> int:
        return self.store.copy(source_session_key, target_session_key)

    def undo(self, session_key: str) -> int:
        return self.store.undo(session_key)

    def begin_turn(self, session_key: str, mutation_group: str | None) -> int:
        return self.store.checkpoint(session_key, mutation_group)

    def render(self, session_key: str) -> str:
        state, revision = self.store.load(session_key)
        if state is None:
            return (
                "<task_state>\n"
                f"Revision: {revision}\n"
                "Not initialized. Initialize Task State for multi-step work or "
                "persistent requirements.\n"
                "</task_state>"
            )

        items = state["items"]
        completed = sum(item["status"] == "completed" for item in items)
        lines = [
            "<task_state>",
            f"Revision: {revision}",
            f"Goal: {self._escape(state['goal'])}",
            f"Progress: {completed}/{len(items)} completed",
            (
                "Completion policy: unfinished items persist across turns; report "
                "their status honestly; claim overall success only when every item "
                "is completed."
            ),
        ]
        if state["requirements"]:
            lines.append("Requirements:")
            lines.extend(f"- {self._escape(req)}" for req in state["requirements"])
        lines.append("Items (use the visible item_number when updating):")
        for number, item in enumerate(items, start=1):
            lines.append(f"{number}. [{item['status']}] {self._escape(item['title'])}")
            for requirement in item["requirements"]:
                lines.append(f"   - {self._escape(requirement)}")
        lines.append("</task_state>")
        return "\n".join(lines)

    def unfinished_numbers(self, session_key: str) -> list[int]:
        state = self.get(session_key)
        if state is None:
            return []
        return [
            number
            for number, item in enumerate(state["items"], start=1)
            if item["status"] in {"pending", "in_progress"}
        ]

    def waiting_or_blocked(self, session_key: str) -> bool:
        state = self.get(session_key)
        if state is None:
            return False
        return any(item["status"] in {"waiting", "blocked"} for item in state["items"])

    def snapshot(self, session_key: str) -> dict[str, Any] | None:
        state, revision = self.store.load(session_key)
        if state is None:
            return None
        return {
            "goal": state["goal"],
            "requirements": list(state["requirements"]),
            "items": [
                {
                    "item_number": number,
                    "title": item["title"],
                    "status": item["status"],
                    "requirements": list(item["requirements"]),
                }
                for number, item in enumerate(state["items"], start=1)
            ],
            "revision": revision,
        }

    def _apply_operation(
        self,
        state: dict[str, Any],
        original_items: dict[int, dict[str, Any]],
        operation: dict[str, Any],
    ) -> None:
        name = operation.get("operation")
        if name == "add":
            item = self._normalize_item(operation.get("item"))
            if len(state["items"]) >= _MAX_ITEMS:
                raise TaskStateError(f"Task State supports at most {_MAX_ITEMS} items.")
            state["items"].append(item)
            return

        if name == "update":
            target = operation.get("target", "item")
            changes = operation.get("changes")
            if not isinstance(changes, dict):
                raise TaskStateError("update requires a non-empty changes object.")
            changes = {field: value for field, value in changes.items() if value is not None}
            if not changes:
                raise TaskStateError("update requires a non-empty changes object.")
            if target == "task_state":
                unknown = set(changes) - {"goal", "requirements"}
                if unknown:
                    raise TaskStateError(f"Unknown Task State fields: {sorted(unknown)}")
                if "goal" in changes:
                    state["goal"] = changes["goal"]
                if "requirements" in changes:
                    state["requirements"] = changes["requirements"]
                return
            if target != "item":
                raise TaskStateError("update target must be task_state or item.")
            item = self._find_item(state, original_items, operation)
            unknown = set(changes) - _ITEM_FIELDS
            if unknown:
                raise TaskStateError(f"Unknown item fields: {sorted(unknown)}")
            item.update(changes)
            return

        if name == "remove":
            reason = operation.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise TaskStateError("remove requires a non-empty reason.")
            item = self._find_item(state, original_items, operation)
            index = next(index for index, current in enumerate(state["items"]) if current is item)
            state["items"].pop(index)
            return

        if name == "complete":
            item = self._find_item(state, original_items, operation)
            item["status"] = "completed"
            return

        if name == "initialize":
            raise TaskStateError("initialize must be the only operation in a call.")
        raise TaskStateError("operation must be one of initialize, add, update, remove, complete.")

    @staticmethod
    def _find_item(
        state: dict[str, Any],
        original_items: dict[int, dict[str, Any]],
        operation: dict[str, Any],
    ) -> dict[str, Any]:
        number = operation.get("item_number")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise TaskStateError("item_number must be a positive integer.")
        item = original_items.get(number)
        if item is None or not any(current is item for current in state["items"]):
            raise TaskStateError(f"item_number {number} is not available in the current Task State.")
        return item

    @classmethod
    def _normalize_state(cls, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise TaskStateError("state must be an object.")
        unknown = set(raw) - _STATE_FIELDS
        if unknown:
            raise TaskStateError(f"Unknown Task State fields: {sorted(unknown)}")
        goal = cls._string(raw.get("goal"), "goal", _MAX_GOAL_CHARS)
        requirements = cls._requirements(raw.get("requirements", []), "requirements")
        items = raw.get("items")
        if not isinstance(items, list):
            raise TaskStateError("state.items must be a list.")
        if len(items) > _MAX_ITEMS:
            raise TaskStateError(f"Task State supports at most {_MAX_ITEMS} items.")
        return {
            "goal": goal,
            "requirements": requirements,
            "items": [cls._normalize_item(item) for item in items],
        }

    @classmethod
    def _normalize_item(cls, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise TaskStateError("Every item must be an object.")
        unknown = set(raw) - _ITEM_FIELDS
        if unknown:
            raise TaskStateError(f"Unknown item fields: {sorted(unknown)}")
        title = cls._string(raw.get("title"), "item.title", _MAX_TITLE_CHARS)
        status = raw.get("status", "pending")
        if not isinstance(status, str) or status not in TASK_STATE_STATUSES:
            raise TaskStateError(f"Invalid item status: {status!r}")
        return {
            "title": title,
            "status": status,
            "requirements": cls._requirements(
                raw.get("requirements", []),
                "item.requirements",
            ),
        }

    @classmethod
    def _requirements(cls, raw: Any, field: str) -> list[str]:
        if not isinstance(raw, list) or len(raw) > _MAX_REQUIREMENTS:
            raise TaskStateError(f"{field} must be a list of at most {_MAX_REQUIREMENTS} strings.")
        return [cls._string(value, f"{field}[{index}]", _MAX_REQUIREMENT_CHARS) for index, value in enumerate(raw)]

    @staticmethod
    def _string(value: Any, field: str, limit: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise TaskStateError(f"{field} must be a non-empty string.")
        value = value.strip()
        if len(value) > limit:
            raise TaskStateError(f"{field} exceeds the {limit}-character limit.")
        return value

    @staticmethod
    def _escape(value: str) -> str:
        return html.escape(value, quote=False)

    @staticmethod
    def _validate_operation_fields(operation: dict[str, Any]) -> None:
        name = operation.get("operation")
        if not isinstance(name, str) or name not in _OPERATION_FIELDS:
            raise TaskStateError("operation must be one of initialize, add, update, remove, complete.")
        unknown = set(operation) - _KNOWN_OPERATION_FIELDS
        if unknown:
            raise TaskStateError(f"Unknown operation fields: {sorted(unknown, key=str)}")


__all__ = [
    "TASK_STATE_STATUSES",
    "TaskStateError",
    "TaskStateManager",
    "TaskStateStore",
    "TaskStateUpdate",
]
