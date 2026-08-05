"""History trimming — the Curator's contribution to ``*history``.

Extracted from :class:`CuratorAssembler` so the Curator and the unified
context engine share one implementation of the operations that decide
which session messages reach the model:

- **adjacency closure** (:meth:`canonical_ids`) — if a tool call is
  selected its result messages come along, and vice versa, so the
  provider never sees a dangling tool call / orphan result;
- **clean extraction** (:meth:`history_from_ids`) — project the selected
  session messages down to the provider-safe key set;
- **structural validation** (:meth:`structural_errors`) — verify every
  tool result has a parent assistant ``tool_calls`` and every call has a
  result;
- **budget trimming** (:meth:`trim`) — build the prompt, estimate its
  token cost, and drop the lowest-priority non-protected messages until
  it fits.

This is the *only* code path that selects ``*history``. Segment 6
(``# Curator Working State``) is rendered by :class:`ContextBuilder`
from the plan's working-state text — it is not this module's concern.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from raven.providers.base import LLMProvider
from raven.utils.helpers import estimate_prompt_tokens_chain

# Provider-safe message keys. Anything else on a session message
# (timestamps, internal ids, manifest annotations) is dropped before
# the dict reaches the LLM. reasoning_content / thinking_blocks must survive
# so multi-turn reasoning contracts (e.g. DeepSeek thinking mode) hold; the
# provider gate strips thinking_blocks for non-Anthropic targets downstream.
_ALLOWED_KEYS = {
    "role",
    "content",
    "tool_calls",
    "tool_call_id",
    "name",
    "reasoning_content",
    "thinking_blocks",
}


@dataclass
class TrimOutcome:
    """Result of a :meth:`HistoryTrimmer.trim` call."""

    history: list[dict[str, Any]]
    included_ids: list[int]
    estimated_tokens: int
    max_prompt_tokens: int
    source: str
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.estimated_tokens <= self.max_prompt_tokens

    @property
    def over_by(self) -> int:
        return max(0, self.estimated_tokens - self.max_prompt_tokens)


class HistoryTrimmer:
    """Shapes and budget-trims the session history into ``*history``."""

    def __init__(
        self,
        provider: LLMProvider,
        model: str,
        get_tool_definitions: Callable[[], list[dict[str, Any]]],
        context_window_tokens: int,
    ) -> None:
        self.provider = provider
        self.model = model
        self.get_tool_definitions = get_tool_definitions
        self.context_window_tokens = context_window_tokens

    # ------------------------------------------------------------------
    # Pure history-shaping helpers (no token estimation / no I/O)
    # ------------------------------------------------------------------

    @staticmethod
    def canonical_ids(messages: list[dict[str, Any]], ids: list[int]) -> list[int]:
        """Close ``ids`` over tool-call / tool-result adjacency.

        Returns the selected indices in order, trimmed so the sequence
        begins at a ``user`` message (so history never starts mid
        tool-exchange). Returns ``[]`` if no user message survives.
        """
        selected = {mid for mid in ids if isinstance(mid, int) and 0 <= mid < len(messages)}
        tool_parent_by_call: dict[str, int] = {}
        tool_result_by_call: dict[str, list[int]] = {}
        for idx, message in enumerate(messages):
            if message.get("role") == "assistant" and message.get("tool_calls"):
                for tc in message.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        tool_parent_by_call[str(tc["id"])] = idx
            if message.get("role") == "tool" and message.get("tool_call_id"):
                tool_result_by_call.setdefault(str(message["tool_call_id"]), []).append(idx)

        changed = True
        while changed:
            changed = False
            for call_id, parent_idx in tool_parent_by_call.items():
                result_ids = tool_result_by_call.get(call_id, [])
                if parent_idx in selected:
                    for rid in result_ids:
                        if rid not in selected:
                            selected.add(rid)
                            changed = True
                if any(rid in selected for rid in result_ids) and parent_idx not in selected:
                    selected.add(parent_idx)
                    changed = True

        ordered = sorted(selected)
        for pos, mid in enumerate(ordered):
            if messages[mid].get("role") == "user":
                return ordered[pos:]
        return []

    @staticmethod
    def history_from_ids(messages: list[dict[str, Any]], ids: list[int]) -> list[dict[str, Any]]:
        """Project the selected messages down to provider-safe keys."""
        history: list[dict[str, Any]] = []
        for mid in ids:
            clean = {k: v for k, v in messages[mid].items() if k in _ALLOWED_KEYS}
            if clean.get("role"):
                history.append(clean)
        return history

    @staticmethod
    def structural_errors(messages: list[dict[str, Any]]) -> list[str]:
        """Tool-call closure validation over a built message list."""
        errors: list[str] = []
        open_calls: set[str] = set()
        for msg in messages:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        open_calls.add(str(tc["id"]))
            if msg.get("role") == "tool":
                call_id = str(msg.get("tool_call_id", ""))
                if call_id not in open_calls:
                    errors.append(f"tool result {call_id} has no parent assistant tool_call")
                else:
                    open_calls.remove(call_id)
        if open_calls:
            errors.append(f"assistant tool_calls missing results: {sorted(open_calls)}")
        return errors

    @staticmethod
    def _first_droppable(ids: list[int], protected_ids: set[int]) -> int | None:
        """Position of the first non-protected id (falls back to 0)."""
        for pos, mid in enumerate(ids):
            if mid not in protected_ids:
                return pos
        return 0 if ids else None

    # ------------------------------------------------------------------
    # Budget-driven trimming
    # ------------------------------------------------------------------

    def trim(
        self,
        *,
        session_messages: list[dict[str, Any]],
        ids: list[int],
        protected_ids: set[int],
        reserved_output: int,
        build_messages: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
    ) -> tuple[list[dict[str, Any]], TrimOutcome]:
        """Close ``ids``, build, and drop until under budget.

        ``build_messages`` maps a history list to the full message list
        (system + history + user) — the caller owns prompt composition
        (segments, working state, router skills), the trimmer only owns
        history selection. Returns the final ``messages`` and a
        :class:`TrimOutcome`.
        """
        canon = self.canonical_ids(session_messages, ids)
        history = self.history_from_ids(session_messages, canon)
        messages = build_messages(history)

        estimated, source = estimate_prompt_tokens_chain(
            self.provider,
            self.model,
            messages,
            self.get_tool_definitions(),
        )
        max_prompt = max(1, self.context_window_tokens - reserved_output)
        warnings: list[str] = []
        trimmed_ids = list(canon)
        while estimated > max_prompt and trimmed_ids:
            drop_idx = self._first_droppable(trimmed_ids, protected_ids)
            if drop_idx is None:
                break
            dropped = trimmed_ids.pop(drop_idx)
            warnings.append(f"dropped message {dropped} to fit budget")
            history = self.history_from_ids(session_messages, trimmed_ids)
            messages = build_messages(history)
            estimated, source = estimate_prompt_tokens_chain(
                self.provider,
                self.model,
                messages,
                self.get_tool_definitions(),
            )

        return messages, TrimOutcome(
            history=history,
            included_ids=trimmed_ids,
            estimated_tokens=estimated,
            max_prompt_tokens=max_prompt,
            source=source,
            warnings=warnings,
        )


__all__ = ["HistoryTrimmer", "TrimOutcome"]
