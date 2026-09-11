"""History trimming — the Curator's contribution to ``*history``.

Owned by :class:`CuratorAssembler`, which calls these operations to decide
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
  it fits -- a tool call and its results as one unit (:meth:`tool_group`),
  so what fits is also what the provider accepts.

This is the *only* code path that selects ``*history``. The
``# Curator Working State`` section is rendered by
:class:`CuratorSegmentBuilder` from the plan's working-state text — it is
not this module's concern.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from raven.contracts.llm_provider import LLMProvider
from raven.providers.binding import ModelBinding, active_window, resolve
from raven.utils.tokens import estimate_prompt_tokens_chain

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
        self._fallback = ModelBinding(provider, model)
        self.get_tool_definitions = get_tool_definitions
        self._fallback_window = int(context_window_tokens)

    @property
    def provider(self) -> "LLMProvider":
        """The provider of the turn's binding; the build-time one outside a turn."""
        return resolve(None, self._fallback).provider

    @property
    def model(self) -> str:
        """The model of the turn's binding; the build-time one outside a turn."""
        return resolve(None, self._fallback).model

    def set_provider(self, provider: LLMProvider, model: str) -> None:
        """Adopt the provider a live ``/model`` switch just built, so token
        estimates keep matching the model actually being called."""
        self._fallback = ModelBinding(provider, model)

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
    def tool_group(messages: list[dict[str, Any]], mid: int) -> set[int]:
        """The ids that stand or fall with ``mid``.

        An assistant carrying ``tool_calls`` goes with every result answering
        it; a tool result goes with its parent and the parent's other results;
        any other message stands alone. Dropping one member without the rest
        leaves the provider a dangling call or an orphan result, which a strict
        backend refuses outright -- measured 2026-09-11 on DeepSeek's API,
        where one such request failed every turn until the history was
        re-selected.
        """
        if not (0 <= mid < len(messages)):
            return {mid}
        msg = messages[mid]
        parent: int | None = None
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            parent = mid
        elif msg.get("role") == "tool" and msg.get("tool_call_id"):
            call_id = str(msg["tool_call_id"])
            for idx in range(mid - 1, -1, -1):
                calls = messages[idx].get("tool_calls") or [] if messages[idx].get("role") == "assistant" else []
                if any(isinstance(tc, dict) and str(tc.get("id")) == call_id for tc in calls):
                    parent = idx
                    break
        if parent is None:
            return {mid}
        call_ids = {
            str(tc["id"]) for tc in (messages[parent].get("tool_calls") or []) if isinstance(tc, dict) and tc.get("id")
        }
        group = {parent}
        for idx in range(parent + 1, len(messages)):
            m = messages[idx]
            if m.get("role") == "tool" and str(m.get("tool_call_id", "")) in call_ids:
                group.add(idx)
        return group

    @classmethod
    def _offenders(cls, messages: list[dict[str, Any]], ids: list[int]) -> set[int]:
        """Selected ids whose tool pairing is broken *within the selection*:
        a parent missing any of its results, or a result whose parent is not
        selected. Empty when the selection is provider-safe."""
        selected = set(ids)
        offenders: set[int] = set()
        for mid in ids:
            group = cls.tool_group(messages, mid)
            if len(group) > 1 and not group <= selected:
                offenders |= group & selected
            elif messages[mid].get("role") == "tool" and group == {mid}:
                offenders.add(mid)  # a result whose parent the session no longer holds
        return offenders

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

    @classmethod
    def _choose_drop(
        cls,
        messages: list[dict[str, Any]],
        ids: list[int],
        protected_ids: set[int],
    ) -> tuple[set[int], list[int], list[int]] | None:
        """The next drop: ``(group, remaining, reclosed)`` or None when ``ids`` is empty.

        Whole group, not one id: the closure that selected these ids kept every
        call beside its results, and a drop has to undo it the same way or the
        next request carries an orphan. Re-closing afterwards adds nothing back
        (neither parent nor results are selected any more) and re-anchors the
        history on a user message.

        Ranked by the group a candidate would remove, not by the candidate:
        a group with any protected member is a protected group, so a
        protection boundary that falls inside a parallel-call exchange
        (``protect_first_n`` counting the call but not its results) cannot be
        used to take the protected call out through an unprotected result.
        Unprotected groups go first, in selection order,
        skipping one whose removal would re-anchor a protected id out of the
        history; protected groups go only after that, under the same rule;
        when nothing goes cleanly the first candidate goes anyway -- the last
        resort trimming has always had, so a prompt that cannot fit still
        shrinks.
        """
        if not ids:
            return None
        seen: set[int] = set()
        unprotected: list[tuple[set[int], list[int], list[int]]] = []
        protected: list[tuple[set[int], list[int], list[int]]] = []
        for candidate in ids:
            if candidate in seen:
                continue
            group = cls.tool_group(messages, candidate)
            seen |= group
            remaining = [mid for mid in ids if mid not in group]
            choice = (group, remaining, cls.canonical_ids(messages, remaining))
            (protected if group & protected_ids else unprotected).append(choice)
        # Unprotected groups first, then protected ones, each only if it leaves
        # every other protected id anchored; when nothing goes cleanly the first
        # candidate goes anyway, so a prompt that cannot fit still shrinks.
        for tier in (unprotected, protected):
            for group, remaining, reclosed in tier:
                lost = (protected_ids & set(remaining)) - set(reclosed)
                if not lost:
                    return group, remaining, reclosed
        return (unprotected + protected)[0]

    # ------------------------------------------------------------------
    # Budget-driven trimming
    # ------------------------------------------------------------------

    @property
    def context_window_tokens(self) -> int:
        """The running turn's window; the one built with, outside a turn.

        A property because this object outlives any number of turns and two
        sessions can be on models of different sizes at once -- an int copied
        at construction answers for whichever session happened to build it.
        """
        return active_window(self._fallback_window)

    @context_window_tokens.setter
    def context_window_tokens(self, tokens: int) -> None:
        self._fallback_window = int(tokens)

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
            choice = self._choose_drop(session_messages, trimmed_ids, protected_ids)
            if choice is None:
                break
            group, remaining, reclosed = choice
            for mid in sorted(group):
                label = "protected message" if mid in protected_ids else "message"
                warnings.append(f"dropped {label} {mid} to fit budget")
            for mid in sorted(set(remaining) - set(reclosed)):
                warnings.append(f"dropped message {mid}: nothing before the first remaining user message is kept")
            trimmed_ids = reclosed
            history = self.history_from_ids(session_messages, trimmed_ids)
            messages = build_messages(history)
            estimated, source = estimate_prompt_tokens_chain(
                self.provider,
                self.model,
                messages,
                self.get_tool_definitions(),
            )

        # A selection that arrived broken -- a plan naming a result whose parent
        # the session no longer holds -- is not shipped either; each pass drops
        # at least one id, so this ends.
        offenders = self._offenders(session_messages, trimmed_ids)
        while offenders:
            trimmed_ids = [mid for mid in trimmed_ids if mid not in offenders]
            for mid in sorted(offenders):
                warnings.append(f"dropped message {mid}: its tool call or result is not in the selection")
            history = self.history_from_ids(session_messages, trimmed_ids)
            messages = build_messages(history)
            estimated, source = estimate_prompt_tokens_chain(
                self.provider,
                self.model,
                messages,
                self.get_tool_definitions(),
            )
            offenders = self._offenders(session_messages, trimmed_ids)

        return messages, TrimOutcome(
            history=history,
            included_ids=trimmed_ids,
            estimated_tokens=estimated,
            max_prompt_tokens=max_prompt,
            source=source,
            warnings=warnings,
        )


__all__ = ["HistoryTrimmer", "TrimOutcome"]
