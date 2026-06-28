"""Faithful reproduction of Hermes Agent's ``system_and_3`` prompt-caching strategy.

Source: ``NousResearch/hermes-agent/agent/prompt_caching.py`` (v0.9.0, 2026-04-13)

Strategy: place up to 4 ``cache_control`` breakpoints —
    1. System prompt (index 0, if role == system)
    2–4. The **last 3 non-system messages** (rolling window at the tail)

This module wraps the Hermes logic as a ``TokenStrategy`` so it can be
installed in Raven's ``StrategyRegistry`` side-by-side with our own
``CacheOptimizer`` for A/B comparison.

Key behavioral differences vs Raven's CacheOptimizer:
    - Does NOT mark the tools schema (all 4 breakpoints go to messages)
    - Does NOT place a mid-history breakpoint
    - Uses a **rolling tail window** that shifts every iteration / turn
    - Mutates via deep-copy (same as Hermes original)
"""

from __future__ import annotations

import copy
from typing import Any

from loguru import logger

from raven.token_wise.base import TokenStrategy
from raven.providers.registry import find_by_model

_CACHE_CONTROL = {"type": "ephemeral"}


def _supports_cache_control(model: str) -> bool:
    if not model:
        return False
    spec = find_by_model(model)
    return spec is not None and spec.supports_prompt_caching


def _apply_cache_marker(msg: dict[str, Any]) -> None:
    """Add cache_control to a single message — handles str / list / None content.

    This is the same logic as Hermes's ``_apply_cache_marker`` (minus the
    ``native_anthropic`` flag which only affects the ``tool`` role — we don't
    mark tool messages in this reproduction because Hermes only marks
    non-system messages and tool-role messages get the same treatment as
    assistant/user via the content-list path).
    """
    content = msg.get("content")

    if content is None or content == "":
        msg["cache_control"] = _CACHE_CONTROL
        return

    if isinstance(content, str):
        msg["content"] = [
            {"type": "text", "text": content, "cache_control": _CACHE_CONTROL}
        ]
        return

    if isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict):
            last["cache_control"] = _CACHE_CONTROL


class SystemAndTailCacheStrategy(TokenStrategy):
    """``system + tail`` cache placement — a faithful reproduction of Hermes
    Agent's ``system_and_3`` strategy (see module docstring for source/credit).

    Breakpoints:
        bp1  — system message (index 0)
        bp2–4 — last 3 non-system messages (rolling)

    The strategy deep-copies all messages before mutating, so the caller's
    original list is never touched.
    """

    name = "system_and_tail"

    async def before_llm_call(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None, str]:
        if not _supports_cache_control(model):
            return messages, tools, model

        new_messages = copy.deepcopy(messages)

        breakpoints_used = 0

        # bp1: system prompt
        if new_messages and new_messages[0].get("role") == "system":
            _apply_cache_marker(new_messages[0])
            breakpoints_used += 1

        # bp2–4: last N non-system messages (N = 4 - breakpoints_used)
        remaining = 4 - breakpoints_used
        non_sys_indices = [
            i for i in range(len(new_messages))
            if new_messages[i].get("role") != "system"
        ]

        for idx in non_sys_indices[-remaining:]:
            _apply_cache_marker(new_messages[idx])

        used = breakpoints_used + min(remaining, len(non_sys_indices))
        logger.debug(
            "SystemAndTailCacheStrategy: placed {} breakpoint(s) on model={}", used, model
        )

        # Hermes does NOT mark tools — all 4 breakpoints go to messages.
        return new_messages, tools, model
