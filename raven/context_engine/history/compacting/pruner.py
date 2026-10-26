"""The tool-result pruner -- reclaim by shortening, not by removing.

The first rung of the ladder above "store it all". A tool result is usually the
largest thing in a window and the least often read twice: forty thousand
characters of stack trace, or a file the model has already acted on. Replacing
one with a record of the call it answered gives back nearly all of it without
removing a message, so tool calls stay paired, the conversation stays whole,
and nothing has to be summarised.

What replaces the body is one line::

    [tool:exec command=pytest -x exit=1 39,914 chars] [[ref session/<log_id>@0#118]]

and nothing else -- no head, no tail. That is a deliberate departure from the
harnesses that keep the first few kilobytes: keeping a slice is what you do
when the rest is gone for good. Here the rest is one ``context_retrieve`` call
away, so a slice would be a second copy of something already reachable, paid
for every turn. The record line carries the structured part a model actually
decides on (which tool, which argument, whether it failed), and the ref carries
the way back to the exact bytes.

Two properties this rests on:

- **It never removes a message and never reorders one.** Only the body of a
  ``role="tool"`` message and oversized arguments on the assistant message that
  produced it. Index ``i`` still names the same exchange afterwards, which is
  what lets the ref be an ordinal into the session log.
- **It is idempotent by arithmetic rather than by inspection.** The line it
  writes is bounded by ``PRUNE_LINE_MAX_CHARS`` and the config forbids a
  ``minChars`` at or below that, so a second pass cannot select what the first
  wrote. No "have I already pruned this" guard to keep in sync.
- **What it rewrote once it rewrites every turn after.** The caller keeps the
  ordinals a pass rewrote and hands them back through :meth:`reapply`, which
  writes the identical line whether or not the turn is under pressure. Without
  that the original body would flow back in on the next turn below the
  trigger, and the history prefix a provider cached would change under it.

Tool results are matched to their calls by position, through the same pairing
``HistoryTrimmer`` uses, because a session may reuse a tool-call id.

The messages handed in are the session log's own dicts. Nothing here mutates
one -- a pruned message is a new dict -- because the caller's list is a shallow
copy of ``session.messages`` and writing through it would edit the append-only
log this module exists to keep verbatim.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Sequence

from raven.config.raven import PRUNE_LINE_MAX_CHARS
from raven.context_engine.history_trimmer import _group_index, _tool_parents
from raven.session.refs import session_ref
from raven.utils.refs import SourceRef
from raven.utils.tokens import estimate_message_tokens

if TYPE_CHECKING:
    from raven.config.raven import ContextPruneConfig
    from raven.contracts.context import SessionRevision

_EXIT_CODE = re.compile(r"Exit code: (\d+)\s*$")
"""How a shell result ends (``sandbox/interfaces.py``), which is the one piece
of structure a tool's *output* can be read for without guessing."""

_INTERESTING_ARGS = ("path", "file_path", "pattern", "url", "query", "command", "skill_id", "name")
"""Argument names worth putting in a record line, most identifying first.

Read from the call's JSON rather than from the result's text: the arguments are
structured and the output is not.
"""

_ARG_VALUE_MAX_CHARS = 48
_ARG_ELISION_MARGIN = 96
"""How much longer than the limit an argument must be before shortening it is
worth doing. The note left in its place costs about this much, so a value only
just over the limit would be replaced by something larger than itself."""
_ERROR_PREFIX = "Error"
"""What ``ToolRegistry.execute`` puts in front of a failure, and what the loop
itself tests for when it decides whether a result reads as one."""


@dataclass(frozen=True)
class PruneOutcome:
    """What one pass over the history did."""

    messages: list[dict[str, Any]]
    reclaimed_tokens: int
    pruned_count: int
    refs: tuple[SourceRef, ...]
    ordinals: frozenset[int] = frozenset()
    """Every index this pass rewrote -- what the caller keeps for :meth:`ToolResultPruner.reapply`."""

    @property
    def changed(self) -> bool:
        return self.pruned_count > 0


_NOTHING = PruneOutcome(messages=[], reclaimed_tokens=0, pruned_count=0, refs=())


class ToolResultPruner:
    """Replaces tool-result bodies with a record of the call plus a ref."""

    def __init__(self, cfg: "ContextPruneConfig") -> None:
        self.cfg = cfg

    def run(
        self,
        messages: list[dict[str, Any]],
        *,
        revision: "SessionRevision",
        protect_from: int | None = None,
        protected_ids: "frozenset[int] | set[int]" = frozenset(),
        prices: "Sequence[int] | None" = None,
    ) -> PruneOutcome:
        """Prune everything eligible below the protected suffix.

        ``protect_from`` is the caller's own floor -- the compacting engine
        passes the start of the tail, so what the reader is currently working
        through is never rewritten. It is taken together with
        ``keepRecentPairs``, which expresses the same protection in exchanges
        rather than in positions; the earlier of the two wins, because each is
        a floor and neither subsumes the other.

        ``protected_ids`` are held out entirely. Pinned skill bodies come
        through here: they are kept verbatim precisely because their absence is
        silently wrong, and a ref the model has to spend a call on is not the
        same as having the text.
        """
        if not self.cfg.enabled or not messages:
            return replace(_NOTHING, messages=messages)

        floor = self._floor(messages, protect_from)
        if floor <= 0:
            return replace(_NOTHING, messages=messages)

        protected = frozenset(protected_ids)
        targets = self._targets(messages, floor, protected)
        arg_targets = self._call_arg_targets(messages, floor, protected)
        if not targets and not arg_targets:
            return replace(_NOTHING, messages=messages)
        return self._rewrite(messages, revision, targets, arg_targets, prices=prices)

    def reapply(
        self,
        messages: list[dict[str, Any]],
        *,
        revision: "SessionRevision",
        ordinals: "frozenset[int] | set[int]",
        protected_ids: "frozenset[int] | set[int]" = frozenset(),
    ) -> PruneOutcome:
        """Rewrite exactly ``ordinals`` again, whatever this turn's pressure.

        The line is a function of the message, its call and its ordinal, all of
        which an append leaves alone, so the bytes come out identical to the
        pass that first chose them. The thresholds are not consulted again: an
        ordinal is here because a pass already decided it, and re-deciding
        would let a later, roomier turn put the body back.

        ``protected_ids`` still wins. A body that has become pinned since it was
        rewritten is held whole, because a missing pinned body is the failure
        pinning exists to prevent.

        Nothing is priced: what a re-application reclaims was reported on the
        turn that chose it, and pricing every body again each turn is the cost
        of a long session. ``reclaimed_tokens`` is ``0``.
        """
        if not self.cfg.enabled or not messages or not ordinals:
            return replace(_NOTHING, messages=messages)
        live = {index for index in ordinals if 0 <= index < len(messages)} - set(protected_ids)
        targets = frozenset(
            index
            for index in live
            if messages[index].get("role") == "tool"
            and isinstance(messages[index].get("content"), str)
            and len(messages[index]["content"]) > PRUNE_LINE_MAX_CHARS
        )
        arg_targets = frozenset(
            index
            for index in live
            if messages[index].get("role") == "assistant"
            and _has_oversized_argument(messages[index].get("tool_calls"), self.cfg.call_args_max_chars)
        )
        if not targets and not arg_targets:
            return replace(_NOTHING, messages=messages)
        return self._rewrite(messages, revision, targets, arg_targets, account=False)

    def _rewrite(
        self,
        messages: list[dict[str, Any]],
        revision: "SessionRevision",
        targets: frozenset[int],
        arg_targets: frozenset[int],
        *,
        account: bool = True,
        prices: "Sequence[int] | None" = None,
    ) -> PruneOutcome:
        call_by_result = self._calls_by_result(messages)
        out: list[dict[str, Any]] = []
        refs: list[SourceRef] = []
        ordinals: set[int] = set()
        reclaimed = 0
        for index, message in enumerate(messages):
            if index in targets:
                ref = session_ref(revision, index)
                replaced = dict(message)
                replaced["content"] = self._record_line(message, call_by_result.get(index), ref)
            elif index in arg_targets:
                ref = session_ref(revision, index)
                replaced = self._with_shortened_args(message, ref)
                if replaced is message:
                    out.append(message)
                    continue
            else:
                out.append(message)
                continue
            refs.append(ref)
            ordinals.add(index)
            if account:
                before = prices[index] if prices is not None and index < len(prices) else None
                reclaimed += (before or estimate_message_tokens(message)) - estimate_message_tokens(replaced)
            out.append(replaced)

        return PruneOutcome(
            messages=out,
            reclaimed_tokens=max(0, reclaimed),
            pruned_count=len(refs),
            refs=tuple(refs),
            ordinals=frozenset(ordinals),
        )

    # ------------------------------------------------------------------

    def _floor(self, messages: list[dict[str, Any]], protect_from: int | None) -> int:
        """The first index this pass may not touch."""
        caller = len(messages) if protect_from is None else max(0, min(protect_from, len(messages)))
        return min(caller, self._recent_pairs_floor(messages))

    def _recent_pairs_floor(self, messages: list[dict[str, Any]]) -> int:
        """Where the last ``keepRecentPairs`` tool exchanges start.

        Counted in exchanges rather than in messages: one assistant turn can
        answer with four parallel calls, and protecting "the last three
        messages" there would leave most of that turn's output prunable while
        the model is still reading it.
        """
        keep = self.cfg.keep_recent_pairs
        if keep <= 0:
            return len(messages)
        groups = _group_index(messages)
        starts = sorted({min(group) for group in groups.values()})
        return starts[-keep] if len(starts) > keep else (starts[0] if starts else len(messages))

    def _targets(self, messages: list[dict[str, Any]], floor: int, protected: frozenset[int]) -> frozenset[int]:
        """Tool results eligible to be replaced."""
        eligible = [
            index
            for index in range(floor)
            if index not in protected
            and messages[index].get("role") == "tool"
            and isinstance(messages[index].get("content"), str)
            and len(messages[index]["content"]) > PRUNE_LINE_MAX_CHARS
        ]
        chosen = {index for index in eligible if len(messages[index]["content"]) >= self.cfg.min_chars}
        if self.cfg.dedup:
            chosen |= self._earlier_duplicates(messages, eligible)
        return frozenset(chosen)

    @staticmethod
    def _earlier_duplicates(messages: list[dict[str, Any]], eligible: list[int]) -> set[int]:
        """Every repeat of a body except its last occurrence.

        The last one is kept whole because it is the one the conversation is
        still working from; the earlier copies say nothing the later one does
        not, and a ref still points at each of them individually.

        "Last" is read over the whole history rather than over the prunable
        part of it, so re-reading a file this turn is what retires the copy
        from ten turns ago -- which is the case that motivates dedup at all.
        """
        last_at: dict[str, int] = {}
        for index, message in enumerate(messages):
            if message.get("role") == "tool" and isinstance(message.get("content"), str):
                last_at[message["content"]] = index
        return {index for index in eligible if last_at[messages[index]["content"]] != index}

    def _call_arg_targets(
        self,
        messages: list[dict[str, Any]],
        floor: int,
        protected: frozenset[int],
    ) -> frozenset[int]:
        """Assistant messages carrying an argument value over the limit.

        Independent of whether that call's result is being replaced: a
        ``write_file`` of 50KB returns a one-line confirmation, so the result
        was never the expensive half.
        """
        if not self.cfg.prune_call_args:
            return frozenset()
        limit = self.cfg.call_args_max_chars
        return frozenset(
            index
            for index in range(floor)
            if index not in protected
            and messages[index].get("role") == "assistant"
            and _has_oversized_argument(messages[index].get("tool_calls"), limit)
        )

    @staticmethod
    def _calls_by_result(messages: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        """Each tool result's originating call, by result index.

        The parent is found by position, then the call by id within that one
        parent, so a reused id names the call the result actually answered.
        """
        found: dict[int, dict[str, Any]] = {}
        for index, parent in _tool_parents(messages).items():
            call_id = str(messages[index].get("tool_call_id", ""))
            for call in messages[parent].get("tool_calls") or []:
                if isinstance(call, dict) and str(call.get("id", "")) == call_id:
                    found[index] = call
        return found

    @staticmethod
    def _record_line(message: dict[str, Any], call: dict[str, Any] | None, ref: SourceRef) -> str:
        """The one line that stands in for a result, clipped to the bound."""
        content = str(message.get("content", ""))
        name = str(message.get("name") or _call_name(call) or "tool")
        parts = [f"tool:{name}"]
        if (argument := _identifying_argument(call)) is not None:
            parts.append(argument)
        if (exit_code := _EXIT_CODE.search(content.rstrip())) is not None:
            # An exit code is the more precise statement of the same thing, and
            # the two side by side read as a contradiction: a command that
            # exited 1 is reported "ok" by the registry, whose subject is the
            # call rather than the command.
            parts.append(f"exit={exit_code.group(1)}")
        else:
            parts.append("error" if content.startswith(_ERROR_PREFIX) else "ok")
        parts.append(f"{len(content):,} chars")
        rendered = ref.render()
        head = f"[{' '.join(parts)}]"
        # The ref is the half that must survive intact -- a clipped one cannot
        # be resolved, and then the body really is gone.
        room = PRUNE_LINE_MAX_CHARS - len(rendered) - 1
        if len(head) > room:
            head = head[: max(1, room - 1)] + "]"
        return f"{head} {rendered}"

    def _with_shortened_args(self, message: dict[str, Any], ref: SourceRef) -> dict[str, Any]:
        """A copy of ``message`` with oversized argument values clipped.

        Rewritten inside the parsed JSON and re-serialised, never by cutting
        the argument string: a truncated JSON document is rejected by the
        provider on this turn and on every turn after it, because the message
        stays in the history.
        """
        limit = self.cfg.call_args_max_chars
        calls: list[Any] = []
        changed = False
        for call in message.get("tool_calls") or []:
            shortened = _shorten_call(call, limit, ref)
            changed = changed or shortened is not call
            calls.append(shortened)
        if not changed:
            return message
        return {**message, "tool_calls": calls}


def _call_name(call: dict[str, Any] | None) -> str:
    return str(((call or {}).get("function") or {}).get("name") or "")


def _call_arguments(call: dict[str, Any] | None) -> dict[str, Any]:
    raw = ((call or {}).get("function") or {}).get("arguments")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _identifying_argument(call: dict[str, Any] | None) -> str | None:
    """The one argument worth naming, as ``key=value``."""
    arguments = _call_arguments(call)
    for key in _INTERESTING_ARGS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            flat = " ".join(value.split())
            if len(flat) > _ARG_VALUE_MAX_CHARS:
                flat = flat[: _ARG_VALUE_MAX_CHARS - 3] + "..."
            return f"{key}={flat}"
    return None


def _is_oversized(value: Any, limit: int) -> bool:
    return isinstance(value, str) and len(value) > limit + _ARG_ELISION_MARGIN


def _has_oversized_argument(calls: Any, limit: int) -> bool:
    return any(_is_oversized(value, limit) for call in calls or [] for value in _call_arguments(call).values())


def _shorten_call(call: Any, limit: int, ref: SourceRef) -> Any:
    if not isinstance(call, dict):
        return call
    arguments = _call_arguments(call)
    oversized = {key: value for key, value in arguments.items() if _is_oversized(value, limit)}
    if not oversized:
        return call
    shortened = dict(arguments)
    for key, value in oversized.items():
        elided = len(value) - limit
        shortened[key] = f"{value[:limit]}... ({elided:,} chars elided, {ref.render()})"
    function = {**(call.get("function") or {}), "arguments": json.dumps(shortened, ensure_ascii=False)}
    return {**call, "function": function}


__all__ = ["PruneOutcome", "ToolResultPruner"]
