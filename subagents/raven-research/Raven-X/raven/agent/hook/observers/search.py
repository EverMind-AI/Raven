"""Search-behavior observers: duplicate tool calls, empty-result streaks.

Re-issuing a byte-identical search this turn is degeneration, not
exploration — the result is already in context. A streak of empty
search results is the fingerprint of a collapsed retrieval chain.
The first gets an intervention (rollback), the second gets a tag:
an empty result is real evidence the model must be allowed to see,
so erasing it and re-sampling would destroy information.
"""

from __future__ import annotations

import json
import logging

from raven.agent.hook.base import AgentHook, AgentHookContext, HookDecision

logger = logging.getLogger(__name__)

_DEFAULT_WATCHED_TOOLS = ("web_search", "web_fetch")


def _tool_calls(response: object) -> list:
    calls = getattr(response, "tool_calls", None)
    if calls is None and isinstance(response, dict):
        calls = response.get("tool_calls")
    return list(calls or [])


def _call_key(call: object) -> tuple[str, str]:
    name = getattr(call, "name", "") or ""
    arguments = getattr(call, "arguments", None) or {}
    try:
        canonical = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        canonical = str(arguments)
    return (name, canonical)


class DuplicateQueryObserver(AgentHook):
    """Rollback when every tool call in the response is a byte-identical
    repeat of a watched call already made this turn.

    The all-of condition keeps it conservative: a response mixing a
    repeated search with any fresh call still makes progress and passes.
    Bounded per turn; past the budget duplicates pass through — the
    tool-failure loop break and the iteration budget own termination.
    """

    def __init__(
        self,
        watched_tools: tuple[str, ...] = _DEFAULT_WATCHED_TOOLS,
        max_rollbacks: int = 2,
        retry_temperature: float = 1.0,
    ) -> None:
        self._watched_tools = frozenset(watched_tools)
        self._max_rollbacks = max_rollbacks
        self._retry_temperature = retry_temperature

    @property
    def name(self) -> str:
        return "DuplicateQueryObserver"

    async def before_execute_tools(self, ctx: AgentHookContext) -> HookDecision:
        calls = _tool_calls(ctx.response)
        if not calls:
            return HookDecision()
        state = ctx.metadata.setdefault("dup_query", {"seen": set(), "rollbacks": 0})
        seen: set = state["seen"]
        keys = [_call_key(c) for c in calls]

        all_watched_repeats = all(k[0] in self._watched_tools and k in seen for k in keys)
        if all_watched_repeats and state["rollbacks"] < self._max_rollbacks:
            state["rollbacks"] += 1
            logger.warning(
                "dup-query: response repeats %d already-made call(s) (%s); rollback %d/%d",
                len(keys),
                ", ".join(sorted({k[0] for k in keys})),
                state["rollbacks"],
                self._max_rollbacks,
            )
            return HookDecision(
                rollback=True,
                rollback_overrides={"temperature": self._retry_temperature},
                notes=[f"dup_query rollback={state['rollbacks']}"],
            )

        for k in keys:
            if k[0] in self._watched_tools:
                seen.add(k)
        return HookDecision()


class EmptySearchObserver(AgentHook):
    """Tag streaks of empty search results — observability only.

    ``streak`` counts consecutive tool-call iterations whose watched
    results were all empty; at ``streak_threshold`` the turn is tagged
    once in ``ctx.metadata["empty_search"]`` and a note is emitted.
    """

    def __init__(
        self,
        watched_tools: tuple[str, ...] = _DEFAULT_WATCHED_TOOLS,
        streak_threshold: int = 3,
        empty_markers: tuple[str, ...] = ("No results for:",),
    ) -> None:
        self._watched_tools = frozenset(watched_tools)
        self._streak_threshold = streak_threshold
        self._empty_markers = empty_markers

    @property
    def name(self) -> str:
        return "EmptySearchObserver"

    async def after_iteration(self, ctx: AgentHookContext) -> HookDecision:
        state = ctx.metadata.setdefault("empty_search", {"streak": 0, "watermark": 0})
        messages = ctx.messages or []
        fresh = messages[state["watermark"] :]
        state["watermark"] = len(messages)

        results = [
            m for m in fresh if isinstance(m, dict) and m.get("role") == "tool" and m.get("name") in self._watched_tools
        ]
        if not results:
            return HookDecision()

        if all(self._is_empty(str(m.get("content") or "")) for m in results):
            state["streak"] += 1
        else:
            state["streak"] = 0
            return HookDecision()

        if state["streak"] == self._streak_threshold:
            state["tagged"] = True
            logger.warning(
                "empty-search: %d consecutive iteration(s) of empty watched results",
                state["streak"],
            )
            return HookDecision(notes=[f"empty_search_streak {state['streak']}"])
        return HookDecision()

    def _is_empty(self, content: str) -> bool:
        return any(marker in content for marker in self._empty_markers) or not content.strip()


__all__ = ["DuplicateQueryObserver", "EmptySearchObserver"]
