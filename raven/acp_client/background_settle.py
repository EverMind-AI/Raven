"""Decide when a Claude Code run's work is over, from its raw SDK messages.

A prompt answer is not the end of a Claude Code run: background commands and
subagents keep working, and each wakes the model again when it ends. The adapter
forwards the SDK messages that say so as ``_claude/sdkMessage`` notifications
when the session opens with ``emitRawSDKMessages``. :class:`BackgroundSettle`
reads them and answers one question, ``done``: the prompt has answered, the
session is idle, no background task this run started is still live, and the
wake turn that follows a task's end has had its idle. It does no I/O and keeps
no clock; callers pass ``now`` in.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

RAW_MESSAGE_METHOD = "_claude/sdkMessage"

RAW_MESSAGE_FILTER: list[dict[str, str]] = [
    {"type": "system", "subtype": "background_tasks_changed"},
    {"type": "system", "subtype": "session_state_changed"},
    {"type": "system", "subtype": "task_started"},
    {"type": "result"},
]

SETTLE_CEILING_S = 3600.0
UNWOKEN_END_S = 15.0
ALIVE_EVERY_S = 60.0

_USAGE_FIELDS = (
    ("input_tokens", "inputTokens"),
    ("output_tokens", "outputTokens"),
    ("cache_read_input_tokens", "cachedReadTokens"),
    ("cache_creation_input_tokens", "cachedWriteTokens"),
)


class BackgroundSettle:
    """Track one run's background work; ``is_monitor_call`` tells a Monitor tool call id."""

    def __init__(self, is_monitor_call: Callable[[str], bool]) -> None:
        self._is_monitor_call = is_monitor_call
        self._answered = False
        self._state: str | None = None
        self._reporting = False
        self._own: dict[str, dict[str, str]] = {}
        self._live: dict[str, dict[str, Any]] = {}
        self._ended: list[str] = []
        self._last_end: float | None = None
        self._last_idle = float("-inf")
        self._last_running = float("-inf")
        self._usage: dict[str, int] | None = None

    @property
    def reporting(self) -> bool:
        return self._reporting

    @property
    def state(self) -> str | None:
        return self._state

    @property
    def answered(self) -> bool:
        return self._answered

    def prompt_answered(self) -> None:
        self._answered = True

    def apply(self, message: Any, now: float) -> list[str] | None:
        if not isinstance(message, dict):
            return None
        kind = message.get("type")
        subtype = message.get("subtype")
        if kind == "result":
            self._add_usage(message.get("usage"))
        elif kind != "system":
            return None
        elif subtype == "task_started":
            task_id = message.get("task_id")
            if isinstance(task_id, str):
                self._own[task_id] = {
                    "tool_use_id": str(message.get("tool_use_id") or ""),
                    "description": str(message.get("description") or task_id),
                }
        elif subtype == "background_tasks_changed":
            tasks = message.get("tasks")
            self._set_live(tasks if isinstance(tasks, list) else [], now)
        elif subtype == "session_state_changed":
            return self._set_state(message.get("state"), now)
        return None

    def done(self, now: float) -> bool:
        if not self._answered or self._state != "idle" or self.running_tasks():
            return False
        if self._last_end is None or self._last_idle > self._last_end:
            return True
        return now - self._last_end > UNWOKEN_END_S and self._last_running <= self._last_end

    def running_tasks(self) -> list[str]:
        return [self._own[task_id]["description"] for task_id in self._live if self._holds(task_id)]

    def usage_counts(self) -> dict[str, int] | None:
        if self._usage is None:
            return None
        counts = dict(self._usage)
        counts["totalTokens"] = sum(counts.values())
        return counts

    def _holds(self, task_id: str) -> bool:
        own = self._own.get(task_id)
        if own is None or self._live[task_id].get("ambient"):
            return False
        return not self._is_monitor_call(own["tool_use_id"])

    def _set_live(self, tasks: list[dict[str, Any]], now: float) -> None:
        previous = self._live
        self._live = {t["task_id"]: t for t in tasks if isinstance(t, dict) and isinstance(t.get("task_id"), str)}
        for task_id, task in previous.items():
            if task_id in self._live or task_id not in self._own or task.get("ambient"):
                continue
            self._ended.append(self._own[task_id]["description"])
            if not self._is_monitor_call(self._own[task_id]["tool_use_id"]):
                self._last_end = now

    def _set_state(self, state: Any, now: float) -> list[str] | None:
        if not isinstance(state, str):
            return None
        self._reporting = True
        previous, self._state = self._state, state
        if state == "idle":
            self._last_idle = now
            self._ended = []
        elif state == "running":
            self._last_running = now
            if self._answered and previous == "idle":
                ended, self._ended = self._ended, []
                return ended
        return None

    def _add_usage(self, usage: Any) -> None:
        if not isinstance(usage, dict):
            return
        if self._usage is None:
            self._usage = {ours: 0 for _, ours in _USAGE_FIELDS}
        for theirs, ours in _USAGE_FIELDS:
            value = usage.get(theirs)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                self._usage[ours] += int(value)


def wake_marker(agent: str, ended: list[str]) -> str:
    what = f"{', '.join(ended)} ended" if ended else "a background event"
    return f"[{agent} resumed on its own: {what}]"
