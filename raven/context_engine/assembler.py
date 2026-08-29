"""ContextAssembler — the single context engine.

Assembles a uniform list of :class:`SegmentBuilder` into the turn's
message array. Two phases:

- **Phase A (parallel)** — every builder with ``needs_prefix=False``
  (seg1–5: identity / bootstrap / memory / active-skills / skills) runs
  concurrently. Their ``text`` joins into the system prefix; their
  ``meta`` merges into the assembled metadata.
- **Phase B (serial)** — builders with ``needs_prefix=True`` (the
  Curator) run with ``ctx.prefix`` populated (the assembled prefix +
  user message + tool defs), so they size ``*history`` against the exact
  fixed overhead. The Curator contributes segment 6 (``text``) and the
  history slot (``history``).

The user message is a structural built-in (every turn has exactly one),
not a pluggable builder. Tools are a side channel — passed to the LLM
alongside ``messages`` and counted in the budget, never rendered into a
segment.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from loguru import logger

from raven.context_engine.scent import ScentMenu
from raven.context_engine.segments import render
from raven.contracts.assembled import AssembledContext, TokenBudget
from raven.contracts.context import AssembledPrefix, AssemblyContext, ContextEngine, SegmentBuilder
from raven.providers.prompt_cache import STABLE_PREFIX_KEY

if TYPE_CHECKING:
    from raven.contracts.context import TurnContext
    from raven.providers.base import LLMProvider


_SEG_SEP = "\n\n---\n\n"


def _stable_prefix_chars(parts: list[tuple[int, str, bool]], *, tail_follows: bool = False) -> int:
    """Characters of the system prefix that read the same on the next turn.

    Only the unbroken run of stable segments at the *start* counts. A prompt
    cache keys on everything up to its breakpoint, so the first volatile segment
    ends the run: a stable segment behind it would be cached under a key that
    changes every turn, paying the write and never reading it back.

    The separator that follows the run is counted in, so the boundary falls
    between two segments rather than inside the join.

    ``tail_follows`` says something is appended after these parts -- phase B,
    which is the Curator. It decides the case where the run *is* every phase-A
    part. On its own that boundary would sit at the end of the message and cache
    nothing the end-of-message breakpoint does not, so it is not declared. With
    volatile text appended after it, it is the only breakpoint in the message
    that survives a turn, and declining to declare it gave up the whole point of
    measuring this: identity and bootstrap present, memory and both skill
    segments contributing nothing, Curator appending -- a reachable
    configuration, and one where the assembled message went out as
    ``IDENTITY<sep>CURATOR`` with no key at all.

    A stable phase-B builder would extend the head further; none exists, and
    treating phase B as volatile only caches less than it could, never wrongly.
    """
    run = 0
    for _, text, stable in parts:
        if not stable:
            break
        run += 1
    if run == 0:
        return 0
    if run < len(parts):
        return len(_SEG_SEP.join(text for _, text, _ in parts[:run])) + len(_SEG_SEP)
    if tail_follows:
        return len(_SEG_SEP.join(text for _, text, _ in parts)) + len(_SEG_SEP)
    return 0


class ContextAssembler(ContextEngine):
    """The one engine. Assembles SegmentBuilders into the turn context."""

    def __init__(
        self,
        builders: list[SegmentBuilder],
        get_tool_definitions: Callable[[], list[dict[str, Any]]],
        now_fn: Callable[[], datetime] | None = None,
        get_tool_notices: Callable[[], list[str]] | None = None,
        scent: "ScentMenu | None" = None,
    ) -> None:
        self._scent = scent
        self.skills_router = None
        """Set by the factory: the SkillForgeRouter behind find_skill."""
        self._builders = sorted(builders, key=lambda b: b.order)
        self._phase_a = [b for b in self._builders if not b.needs_prefix]
        self._phase_b = [b for b in self._builders if b.needs_prefix]
        self.get_tool_definitions = get_tool_definitions
        self.get_tool_notices = get_tool_notices
        self._now_fn = now_fn or datetime.now

    @property
    def name(self) -> str:
        return "context_assembler"

    @property
    def owns_compaction(self) -> bool:
        # The Curator lane archives history itself, so AgentLoop hands it
        # the full append-only log and skips the host MemoryConsolidator.
        return True

    def set_provider(self, provider: "LLMProvider", model: str) -> None:
        # Duck-typed on purpose: only the builders that actually call an LLM
        # implement it, and putting it on the SegmentBuilder protocol would
        # force an empty override onto every purely textual builder.
        for builder in self._builders:
            setter = getattr(builder, "set_provider", None)
            if callable(setter):
                setter(provider, model)

    async def assemble(
        self,
        session_key: str,
        session_messages: list[dict[str, Any]],
        budget: TokenBudget,
        *,
        turn: "TurnContext",
    ) -> AssembledContext:
        ctx = AssemblyContext(
            session_key=session_key,
            current_message=turn.current_message,
            media=turn.media,
            can_see_images=turn.can_see_images,
            describe_tool=turn.describe_tool,
            channel=turn.channel,
            chat_id=turn.chat_id,
            surface=turn.surface,
            session_messages=session_messages,
            budget=budget,
        )

        # ── Phase A — independent segment builders, concurrent ──────
        # One failing builder degrades its segment, never the turn; the names
        # are recorded in metadata so the loop can surface a Notice.
        a_segs = await asyncio.gather(*[b.build(ctx) for b in self._phase_a], return_exceptions=True)
        degraded: list[str] = []
        for builder, seg in zip(self._phase_a, a_segs):
            if isinstance(seg, Exception):
                degraded.append(builder.name)
                logger.opt(exception=seg).error("segment builder {} failed; assembling without it", builder.name)
        meta: dict[str, Any] = {}
        prefix_parts: list[tuple[int, str, bool]] = []
        for builder, seg in zip(self._phase_a, a_segs):
            if seg is None or isinstance(seg, Exception):
                continue
            meta |= seg.meta
            if seg.text:
                prefix_parts.append((builder.order, seg.text, bool(getattr(builder, "stable", False))))
        prefix_parts.sort(key=lambda t: t[0])
        system_prefix = _SEG_SEP.join(text for _, text, _ in prefix_parts)

        if self._scent is not None:
            scent = await self._scent.build(ctx.current_message, ctx.session_messages)
            if scent:
                ctx = replace(ctx, scent_text=scent.text)
                # Under pull no SkillsSegmentBuilder runs, so the menu is the
                # only writer of this key: the after-turn backend feedback
                # (FB-1) keeps receiving the skills the model was offered.
                meta |= {"injected_skill_ids": list(scent.skill_ids)}
        user_msg = self._build_user(ctx)

        # ── Phase B — prefix-dependent builders (Curator), serial ───
        ctx_b = replace(
            ctx,
            prefix=AssembledPrefix(
                system_prefix=system_prefix,
                user_message=user_msg,
                tool_defs=self.get_tool_definitions(),
            ),
        )
        # The same rule as Phase A: a failing Curator loses its segment (the
        # turn runs on the prefix and the raw history), never the turn.
        b_segs = await asyncio.gather(*[b.build(ctx_b) for b in self._phase_b], return_exceptions=True)
        for builder, seg in zip(self._phase_b, b_segs):
            if isinstance(seg, Exception):
                degraded.append(builder.name)
                logger.opt(exception=seg).error("segment builder {} failed; assembling without it", builder.name)
        if degraded:
            meta["degraded_segments"] = degraded

        system = system_prefix
        history: list[dict[str, Any]] = []
        seg6_parts: list[tuple[int, str]] = []
        for builder, seg in zip(self._phase_b, b_segs):
            if seg is None or isinstance(seg, Exception):
                continue
            meta |= seg.meta
            if seg.text:
                seg6_parts.append((builder.order, seg.text))
            if seg.history is not None:
                history = seg.history
        seg6_parts.sort(key=lambda t: t[0])
        for _, text in seg6_parts:
            system = system + "\n\n---\n\n" + text

        # Measured now, not after phase A: whether the stable run reaches the end
        # of the message depends on whether phase B appended anything to it.
        stable_chars = _stable_prefix_chars(prefix_parts, tail_follows=bool(seg6_parts))

        system_msg: dict[str, Any] = {"role": "system", "content": system}
        if stable_chars:
            # Phase B only ever appends, so the boundary measured over the phase-A
            # parts still points at the same character of the finished message.
            system_msg[STABLE_PREFIX_KEY] = stable_chars
        messages = [system_msg, *_coalesce_assistant(history), user_msg]
        return AssembledContext(
            messages=messages,
            metadata=meta | {"engine": self.name},
        )

    async def after_turn(
        self,
        session_key: str,
        outcome: dict[str, Any],
        usage: dict[str, int] | None = None,
    ) -> None:
        # Delegate to any builder that keeps per-turn bookkeeping (Curator).
        for builder in self._builders:
            hook = getattr(builder, "after_turn", None)
            if hook is not None:
                await hook(session_key, outcome, usage)

    def set_context_window(self, tokens: int) -> None:
        # Delegate to any builder that sized itself against the window at
        # construction (only the Curator does; seg1-5 carry no budget).
        for builder in self._builders:
            setter = getattr(builder, "set_context_window", None)
            if setter is not None:
                setter(tokens)

    def _build_user(self, ctx: AssemblyContext) -> dict[str, Any]:
        """The single structural user message: runtime context + content.

        The scent menu (pull-mode skill discovery) rides the same envelope
        as the clock: per-turn material belongs at the sequence tail, where
        appending never invalidates the cached prefix.

        The menu must stay inside the envelope's *first* paragraph: the
        session persist (``AgentLoop._save_turn``) strips the runtime-context
        block from a stored user message by dropping everything before the
        first blank line, and the menu is per-turn material that must go with
        it — stored as the user's own text it would render as such on resume
        and feed the next turn's novelty window its own output. Hence the
        single-newline join and the collapse of any blank lines within.
        """
        notices = self.get_tool_notices() if self.get_tool_notices is not None else None
        runtime_ctx = render.build_runtime_context(
            self._now_fn, ctx.channel, ctx.chat_id, surface=ctx.surface, tool_notices=notices
        )
        if ctx.scent_text:
            scent = re.sub(r"\n{2,}", "\n", ctx.scent_text.strip())
            runtime_ctx = f"{runtime_ctx}\n{scent}"
        user_content = render.build_user_content(
            ctx.current_message,
            ctx.media,
            can_see_images=ctx.can_see_images,
            describe_tool=ctx.describe_tool,
        )
        if isinstance(user_content, str):
            merged: Any = f"{runtime_ctx}\n\n{user_content}"
        else:
            merged = [{"type": "text", "text": runtime_ctx}] + user_content
        return {"role": "user", "content": merged}


def _coalesce_assistant(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge adjacent plain-assistant messages into one.

    A verbatim ``deliver_text`` turn records the bot's answer as an assistant
    message; it can land right after a prior assistant reply (the "on it" ack),
    leaving two adjacent assistant turns. Some providers reject consecutive
    same-role messages and nothing else in the pipeline merges them. Only plain
    assistants (no ``tool_calls``, str content) merge — an assistant carrying
    tool_calls is always followed by its tool result, never another assistant,
    and merging it would break tool-call adjacency. The merged-in message must
    also carry no reasoning fields, so the merge never silently drops the
    reasoning_content / thinking_blocks history projection preserves (deliver_text
    answers have none; this only guards a hypothetical future adjacency source).
    """
    out: list[dict[str, Any]] = []
    for msg in history:
        prev = out[-1] if out else None
        if (
            prev is not None
            and msg.get("role") == "assistant"
            and prev.get("role") == "assistant"
            and not msg.get("tool_calls")
            and not prev.get("tool_calls")
            and not msg.get("reasoning_content")
            and not msg.get("thinking_blocks")
            and isinstance(msg.get("content"), str)
            and isinstance(prev.get("content"), str)
        ):
            merged = dict(prev)
            merged["content"] = f"{prev['content']}\n\n{msg['content']}"
            out[-1] = merged
            continue
        out.append(msg)
    return out


__all__ = ["ContextAssembler"]
