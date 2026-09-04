"""Agent Tool for mutating the resident Task State."""

from __future__ import annotations

from typing import Any

from raven.agent import workdir
from raven.contracts.tool import Tool
from raven_design.task_state.manager import TaskStateError, TaskStateManager


class TaskStateTool(Tool):
    """Apply atomic Task State operations addressed by visible item numbers."""

    def __init__(self, manager: TaskStateManager):
        self._manager = manager

    @property
    def name(self) -> str:
        return "update_task_state"

    @property
    def description(self) -> str:
        return (
            "Create or update the resident Task State. Use initialize with a full state "
            "to replace it, or submit atomic add/update/remove/complete operations. "
            "Address an existing item with the visible 1-based item_number from the "
            "Task State context; do not invent an item id."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        status = {
            "type": "string",
            "enum": ["pending", "in_progress", "waiting", "blocked", "completed"],
        }
        requirements = {
            "type": "array",
            "items": {"type": "string"},
        }
        item = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "title": {"type": "string"},
                "status": status,
                "requirements": requirements,
            },
            "required": ["title"],
        }
        state = {
            "type": "object",
            "description": "Complete replacement state for initialize.",
            "additionalProperties": False,
            "properties": {
                "goal": {"type": "string"},
                "requirements": requirements,
                "items": {
                    "type": "array",
                    "items": item,
                },
            },
            "required": ["goal", "items"],
        }
        item_number = {
            "type": "integer",
            "minimum": 1,
            "description": "Visible 1-based item number from the current Task State.",
        }

        def nullable(schema: dict[str, Any]) -> dict[str, Any]:
            return {"anyOf": [schema, {"type": "null"}]}

        def operation_schema(
            operation: str,
            properties: dict[str, Any],
            required: list[str],
        ) -> dict[str, Any]:
            return {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "operation": {"type": "string", "enum": [operation]},
                    **properties,
                },
                "required": ["operation", *required],
            }

        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "operations": {
                    "type": "array",
                    "minItems": 1,
                    "description": "Operations applied atomically in order.",
                    "items": {
                        "oneOf": [
                            operation_schema("initialize", {"state": state}, ["state"]),
                            operation_schema(
                                "add",
                                {"item": {**item, "description": "New item for add."}},
                                ["item"],
                            ),
                            operation_schema(
                                "update",
                                {
                                    "target": {"type": "string", "enum": ["task_state"]},
                                    "changes": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "properties": {
                                            "goal": nullable({"type": "string"}),
                                            "requirements": nullable(requirements),
                                        },
                                        "required": ["goal", "requirements"],
                                    },
                                },
                                ["target", "changes"],
                            ),
                            operation_schema(
                                "update",
                                {
                                    "target": {"type": "string", "enum": ["item"]},
                                    "item_number": item_number,
                                    "changes": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "properties": {
                                            "title": nullable({"type": "string"}),
                                            "status": nullable(status),
                                            "requirements": nullable(requirements),
                                        },
                                        "required": ["title", "status", "requirements"],
                                    },
                                },
                                ["target", "item_number", "changes"],
                            ),
                            operation_schema(
                                "remove",
                                {
                                    "item_number": item_number,
                                    "reason": {
                                        "type": "string",
                                        "description": "Required explanation for removing the item.",
                                    },
                                },
                                ["item_number", "reason"],
                            ),
                            operation_schema(
                                "complete",
                                {"item_number": item_number},
                                ["item_number"],
                            ),
                        ]
                    },
                }
            },
            "required": ["operations"],
        }

    def display_call(self, args: dict[str, Any]) -> str:
        return "update_task_state"

    async def execute(self, operations: list[dict[str, Any]], **extra: Any) -> str:
        bound = workdir.current()
        if bound is None:
            return "Error: Task State has no active session context."
        if extra:
            return f"Error: Unknown Task State fields: {sorted(extra, key=str)}"
        try:
            result = self._manager.apply(
                str(bound),
                operations,
                mutation_group=None,
            )
        except TaskStateError as exc:
            return f"Error: {exc}"

        completed = sum(item["status"] == "completed" for item in result.state["items"])
        total = len(result.state["items"])
        return (
            f"Task State updated (revision {result.revision}). "
            f"Progress: {completed}/{total} completed. "
            "Use the visible item_number for the next update."
        )


__all__ = ["TaskStateTool"]
