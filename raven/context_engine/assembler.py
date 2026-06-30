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
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from raven.context_engine.base import (
    AssembledPrefix,
    AssemblyContext,
    ContextEngine,
    SegmentBuilder,
)
from raven.context_engine.segments import render
from raven.memory_engine.base import AssembledContext, TokenBudget

if TYPE_CHECKING:
    from raven.context_engine.curator import TurnContext


class ContextAssembler(ContextEngine):
    """The one engine. Assembles SegmentBuilders into the turn context."""

    def __init__(
        self,
        builders: list[SegmentBuilder],
        get_tool_definitions: Callable[[], list[dict[str, Any]]],
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._builders = sorted(builders, key=lambda b: b.order)
        self._phase_a = [b for b in self._builders if not b.needs_prefix]
        self._phase_b = [b for b in self._builders if b.needs_prefix]
        self.get_tool_definitions = get_tool_definitions
        self._now_fn = now_fn or datetime.now

    @property
    def name(self) -> str:
        return "context_assembler"

    @property
    def owns_compaction(self) -> bool:
        # The Curator lane archives history itself, so AgentLoop hands it
        # the full append-only log and skips the host MemoryConsolidator.
        return True

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
            channel=turn.channel,
            chat_id=turn.chat_id,
            session_messages=session_messages,
            budget=budget,
        )

        # ── Phase A — independent segment builders, concurrent ──────
        a_segs = await asyncio.gather(*[b.build(ctx) for b in self._phase_a])
        meta: dict[str, Any] = {}
        prefix_parts: list[tuple[int, str]] = []
        for builder, seg in zip(self._phase_a, a_segs):
            if seg is None:
                continue
            meta |= seg.meta
            if seg.text:
                prefix_parts.append((builder.order, seg.text))
        prefix_parts.sort(key=lambda t: t[0])
        system_prefix = "\n\n---\n\n".join(text for _, text in prefix_parts)

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
        b_segs = await asyncio.gather(*[b.build(ctx_b) for b in self._phase_b])

        system = system_prefix
        history: list[dict[str, Any]] = []
        seg6_parts: list[tuple[int, str]] = []
        for builder, seg in zip(self._phase_b, b_segs):
            if seg is None:
                continue
            meta |= seg.meta
            if seg.text:
                seg6_parts.append((builder.order, seg.text))
            if seg.history is not None:
                history = seg.history
        seg6_parts.sort(key=lambda t: t[0])
        for _, text in seg6_parts:
            system = system + "\n\n---\n\n" + text

        messages = [{"role": "system", "content": system}, *history, user_msg]
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

    def _build_user(self, ctx: AssemblyContext) -> dict[str, Any]:
        """The single structural user message: runtime context + content."""
        runtime_ctx = render.build_runtime_context(self._now_fn, ctx.channel, ctx.chat_id)
        user_content = render.build_user_content(ctx.current_message, ctx.media)
        if isinstance(user_content, str):
            merged: Any = f"{runtime_ctx}\n\n{user_content}"
        else:
            merged = [{"type": "text", "text": runtime_ctx}] + user_content
        return {"role": "user", "content": merged}


__all__ = ["ContextAssembler"]
