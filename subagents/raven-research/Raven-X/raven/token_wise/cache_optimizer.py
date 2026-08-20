"""CacheOptimizer — places Anthropic ``cache_control`` breakpoints optimally.

Anthropic allows up to 4 ephemeral cache breakpoints per request. The cache
key for each breakpoint is every block *up to and including* that breakpoint,
so placement determines what is actually cacheable.

v2 strategy (informed by head-to-head benchmarking against Hermes Agent's
``system_and_3`` strategy — see ``EXPERIMENT_REPORT_HERMES_VS_RAVEN.md``):

When tools are present (common agent scenario):
    1. Tools list end — tool schemas rarely change; caching them saves the
       full schema re-send cost every call.
    2. System prompt tail — SOUL + USER + MEMORY + built-ins is stable.
    3. ``messages[-2]`` — rolling tail; covers the intra-turn tool-chain
       prefix so each iteration only pays fresh for the newest result.
    4. ``messages[-1]`` — rolling tail; written as cache this call, read as
       cache next call; completes the rolling coverage.

When no tools are present (pure conversation):
    1. System prompt tail.
    2–4. Last 3 non-system messages (rolling window identical to Hermes's
         ``system_and_3`` — proven optimal for cross-turn prefix matching).

For models that do not support prompt caching, this strategy is a no-op.
Original messages and tools are *never* mutated; deep copies are taken for
every block that gets a ``cache_control`` marker.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from loguru import logger

from raven.providers.prompt_cache import CACHE_CONTROL
from raven.token_wise.base import TokenStrategy

_CACHE_CONTROL = CACHE_CONTROL


def _supports_cache_control(model: str) -> bool:
    """Model-string fallback, used when no provider probe was injected.

    Asked of ``providers.prompt_cache`` -- see it for why (wire x family). It
    is still the weaker answer than the injected ``supports_caching`` probe:
    it cannot see the gateway, so a Claude-looking model routed through a
    non-caching gateway resolves True here and False at the provider.
    """
    from raven.providers.prompt_cache import accepts_cache_control

    return accepts_cache_control(model)


def _last_index(messages: list[dict[str, Any]], *, role: str) -> int | None:
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == role:
            return i
    return None


def _mark_cache(block: dict[str, Any]) -> dict[str, Any]:
    return {**block, "cache_control": _CACHE_CONTROL}


def _mark_message_tail(msg: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``msg`` with ``cache_control`` on the last content block.

    - str content → wrapped into a single text block with cache_control
    - list content → last block gets cache_control
    - other (None/dict/unknown) → returned unchanged
    """
    content = msg.get("content")
    if isinstance(content, str):
        new_content = [{"type": "text", "text": content, "cache_control": _CACHE_CONTROL}]
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


class CacheOptimizer(TokenStrategy):
    """Adaptive cache breakpoint placement.

    Uses all 4 Anthropic breakpoints. The allocation adapts to whether tools
    are present:

    - **With tools**: tools + system + msg[-2] + msg[-1]  (2 rolling)
    - **Without tools**: system + msg[-3] + msg[-2] + msg[-1]  (3 rolling,
      equivalent to Hermes ``system_and_3``)

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

        Pass the provider's own :meth:`supports_prompt_caching` so the decision is
        made once, by the object that knows which endpoint terminates the request.
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

        # ── bp2: system prompt tail ──
        sys_idx = _last_index(new_messages, role="system")
        if sys_idx is not None and budget > 0:
            new_messages[sys_idx] = _mark_message_tail(new_messages[sys_idx])
            marked_indices.add(sys_idx)
            budget -= 1

        # ── bp3..4 (or bp2..4 when no tools): rolling tail window ──
        # Place breakpoints on the last N non-system messages, where
        # N = remaining budget. This is the rolling-window approach that
        # covers both intra-turn tool chains and cross-turn prefix reuse.
        if budget > 0:
            non_sys = [
                i
                for i in range(len(new_messages))
                if i not in marked_indices and new_messages[i].get("role") != "system"
            ]
            for idx in non_sys[-budget:]:
                new_messages[idx] = _mark_message_tail(new_messages[idx])
                marked_indices.add(idx)
                budget -= 1

        used = self.max_breakpoints - budget
        logger.debug("CacheOptimizer: placed {} breakpoint(s) on model={}", used, model)
        return new_messages, new_tools, model
