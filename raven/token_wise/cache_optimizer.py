"""CacheOptimizer — places Anthropic ``cache_control`` breakpoints optimally.

Anthropic allows up to 4 ephemeral cache breakpoints per request. The cache
key for each breakpoint is every block *up to and including* that breakpoint,
so placement determines what is actually cacheable.

Placement, measured against the ``system_and_3`` strategy in
:mod:`raven.token_wise.system_and_tail_cache`:

When tools are present (common agent scenario):
    1. Tools list end — tool schemas rarely change; caching them saves the
       full schema re-send cost every call.
    2. The system message — on its **stable head** when it declares one
       (``prompt_cache.STABLE_PREFIX_KEY``), on its tail otherwise. The head is
       the only mark in this whole set that survives a turn: the rest of that
       message is the Curator's working state -- and, with turn scratch off,
       the memory recall and the router's hits -- all derived from what the
       user just said, and a cache covers everything in front of its
       breakpoint. With turn scratch on and nothing appended by the history
       builder, the whole message is stable and the tail mark survives too.
    3. ``messages[-2]`` — rolling tail; covers the intra-turn tool-chain
       prefix so each iteration only pays fresh for the newest result.
    4. ``messages[-1]`` — rolling tail; written as cache this call, read as
       cache next call; completes the rolling coverage.

    An end-of-system mark is *not* added beside the head one. Four is the whole
    budget, and 3 subsumes it: a cache keyed at ``messages[-2]`` already carries
    the entire system message, for exactly the calls where that message is
    unchanged -- the iterations within one turn. Across turns neither survives.

When no tools are present (pure conversation):
    1. The system message, same rule as above.
    2–4. Last 3 non-system messages (rolling window identical to Hermes's
         ``system_and_3`` — proven optimal for cross-turn prefix matching).

When a history message carries ``prompt_cache.COMPACTION_BOUNDARY_KEY``, the
oldest rolling slot moves to it, provided the newest one is left. The compacting
history strategy summarises by replaying an earlier request up to the end of
what it removes, which lies behind the rolling window, so this is the only mark
the summary can read back past the system head.

For models that do not support prompt caching, this strategy is a no-op.
Original messages and tools are *never* mutated; deep copies are taken for
every block that gets a ``cache_control`` marker.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from loguru import logger

from raven.contracts.token_strategy import TokenStrategy
from raven.providers.prompt_cache import (
    COMPACTION_BOUNDARY_KEY,
    STABLE_PREFIX_KEY,
    cache_control,
    claim_marks,
    split_stable_prefix,
)


def _supports_cache_control(model: str) -> bool:
    """Model-string fallback, used when no provider probe was injected.

    Asked of ``providers.prompt_cache`` -- see it for why (wire x family). It is
    the weaker of the two answers: it cannot see the gateway, so a Claude-looking
    model routed through a non-caching one resolves True here and False at the
    provider that has to send it.
    """
    from raven.providers.prompt_cache import accepts_cache_control

    return accepts_cache_control(model)


def _last_index(messages: list[dict[str, Any]], *, role: str) -> int | None:
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == role:
            return i
    return None


def _mark_cache(block: dict[str, Any]) -> dict[str, Any]:
    return {**block, "cache_control": cache_control()}


def _mark_message_tail(msg: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``msg`` with ``cache_control`` on the last content block.

    - str content → wrapped into a single text block with cache_control
    - list content → last block gets cache_control
    - other (None/dict/unknown) → returned unchanged
    """
    content = msg.get("content")
    if isinstance(content, str):
        new_content = [{"type": "text", "text": content, "cache_control": cache_control()}]
    elif isinstance(content, list) and content:
        new_content = list(content)
        last = new_content[-1]
        if isinstance(last, dict):
            new_content[-1] = _mark_cache(last)
        else:
            return msg
    else:
        return msg
    return {**msg, "content": new_content}


def _compaction_boundary(messages: list[dict[str, Any]]) -> int | None:
    """The flagged boundary message, or the nearest one before it that can hold a mark.

    Never one after it: a summary replays up to the flag and no further, so a
    mark past it is one the summary never sends. The walk back steps over what
    has no block to carry the field -- an assistant turn that only called a
    tool, empty content -- since wrapping empty text into a marked block is
    refused upstream.
    """
    flagged = next(
        (i for i, m in enumerate(messages) if m.get(COMPACTION_BOUNDARY_KEY) and m.get("role") != "system"),
        None,
    )
    if flagged is None:
        return None
    for i in range(flagged, -1, -1):
        msg = messages[i]
        if msg.get("role") == "system":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return i
        if isinstance(content, list) and content and isinstance(content[-1], dict):
            return i
    return None


class CacheOptimizer(TokenStrategy):
    """Adaptive cache breakpoint placement.

    Uses all 4 Anthropic breakpoints. The allocation adapts to whether tools
    are present:

    - **With tools**: tools + system + msg[-2] + msg[-1]  (2 rolling)
    - **Without tools**: system + msg[-3] + msg[-2] + msg[-1]  (3 rolling,
      equivalent to Hermes ``system_and_3``)
    - **With a compaction boundary flagged**: the boundary takes the oldest
      rolling slot, as long as one rolling mark is left

    The rolling tail ensures that intra-turn tool chains are cached
    incrementally (each iteration's new tool_result becomes cached prefix
    for the next iteration), and cross-turn prefixes hit the cache through
    the natural overlap between turn N's tail and turn N+1's window.
    """

    name = "cache_optimizer"

    def __init__(
        self,
        max_breakpoints: int = 4,
        *,
        supports_caching: "Callable[[str], bool] | None" = None,
    ):
        """``supports_caching`` overrides the model-string lookup.

        Pass the provider's own :meth:`supports_prompt_caching` so the decision
        is made by the object that knows which endpoint terminates the request.
        """
        if max_breakpoints < 1:
            raise ValueError("max_breakpoints must be >= 1")
        self.max_breakpoints = max_breakpoints
        self._supports_caching = supports_caching or _supports_cache_control

    async def before_llm_call(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None, str]:
        if not self._supports_caching(model):
            return messages, tools, model

        budget = self.max_breakpoints
        new_tools = tools
        new_messages = list(messages)
        marked_indices: set[int] = set()

        # ── bp1: tools (only when tools are present) ──
        if tools and budget > 0:
            new_tools = copy.deepcopy(tools)
            new_tools[-1] = _mark_cache(new_tools[-1])
            budget -= 1

        # ── bp2: the system message ──
        # On its stable head when it declares one, on its tail when it does not.
        #
        # Not both: four is the whole budget and the two rolling marks below are
        # worth more than an end-of-message one, which they subsume. A cache
        # covers everything in front of its breakpoint, so a mark on `msg[-2]`
        # already carries the entire system message with it -- for the calls
        # inside one turn, where the message does not change. Across turns
        # neither of them survives; only the head does, which is the one placed
        # here.
        sys_idx = _last_index(new_messages, role="system")
        if sys_idx is not None and budget > 0:
            sys_msg = new_messages[sys_idx]
            blocks = split_stable_prefix(sys_msg.get("content"), int(sys_msg.get(STABLE_PREFIX_KEY) or 0))
            if blocks is not None:
                blocks[0] = _mark_cache(blocks[0])
                new_messages[sys_idx] = {**sys_msg, "content": blocks}
            else:
                new_messages[sys_idx] = _mark_message_tail(sys_msg)
            marked_indices.add(sys_idx)
            budget -= 1

        non_sys = [
            i for i in range(len(new_messages)) if i not in marked_indices and new_messages[i].get("role") != "system"
        ]

        # ── the compaction boundary, when the history flags one ──
        # Taken from the rolling window's oldest slot, and only while the
        # newest is left for the next call to read. The provider's look-back
        # from that newest mark still finds what the oldest one would have
        # held, so the window loses little; a boundary already inside it needs
        # no slot of its own.
        boundary = _compaction_boundary(new_messages)
        if boundary is not None and budget > 1 and boundary not in non_sys[-budget:]:
            new_messages[boundary] = _mark_message_tail(new_messages[boundary])
            marked_indices.add(boundary)
            budget -= 1

        # ── bp3..4 (or bp2..4 when no tools): rolling tail window ──
        # Place breakpoints on the last N non-system messages, where
        # N = remaining budget. This is the rolling-window approach that
        # covers both intra-turn tool chains and cross-turn prefix reuse.
        if budget > 0:
            for idx in non_sys[-budget:]:
                new_messages[idx] = _mark_message_tail(new_messages[idx])
                marked_indices.add(idx)
                budget -= 1

        used = self.max_breakpoints - budget
        logger.debug("CacheOptimizer: placed {} breakpoint(s) on model={}", used, model)
        if used:
            # Tell the provider this request is spoken for. Only when something
            # was actually placed: a call this strategy declined to mark is one
            # the provider should still handle itself.
            new_messages = claim_marks(new_messages)
        return new_messages, new_tools, model
