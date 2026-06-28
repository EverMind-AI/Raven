"""Adapter hooks that wrap the legacy AgentLoop callback parameters.

These let AgentLoop keep accepting the existing
``response_modifier`` / ``on_user_inbound`` / ``decision_consumer``
constructor parameters (preserving the test surface and Sentinel
wire-up) while the internal call sites switch to a single
``CompositeHook`` chain. Each adapter is a thin shim — its only job
is to translate the legacy callable signature into a hook method.

Each adapter is intentionally restricted to a single phase. The
``name`` property tags them with ``Legacy`` so logs / debug output
distinguish them from purpose-built hooks (Sentinel, eval_engine, …).

When the legacy callback parameter is ``None`` the adapter is simply
not constructed — AgentLoop only adds adapters for non-None callbacks.

Future work (post-Phase-6): once external callers stop passing
the legacy parameters and adopt the AgentHook contract directly, these
adapters can be deleted.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Awaitable, Callable, Union

from raven.agent.hook.base import AgentHook, AgentHookContext, HookDecision

logger = logging.getLogger(__name__)


_OnInboundCallable = Callable[[Any], Union[None, Awaitable[None]]]
_DecisionConsumerCallable = Callable[[Any], Awaitable[Any]]
_ResponseModifierCallable = Callable[[str, str], str]


class OnUserInboundAdapter(AgentHook):
    """Wrap a legacy ``on_user_inbound`` callback as a hook.

    The legacy contract was an observer — it received the inbound
    message and could not short-circuit. We honor that: the adapter
    always returns a pass-through ``HookDecision``. Sync callables are
    invoked directly; async callables are awaited.

    Exceptions are logged and swallowed so a flaky Sentinel feedback
    tracker can't crash a user turn (this used to live inline in
    AgentLoop with a try/except; the hook adapter preserves that
    semantic).
    """

    def __init__(self, callback: _OnInboundCallable) -> None:
        self._callback = callback

    @property
    def name(self) -> str:
        return "Legacy(on_user_inbound)"

    async def before_user_inbound(
        self, ctx: AgentHookContext
    ) -> HookDecision:
        req = ctx.turn_request
        if req is None:
            return HookDecision()
        try:
            result = self._callback(req)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001 — observer must not crash chain
            logger.warning(
                "on_user_inbound legacy callback raised %s: %s",
                type(exc).__name__, exc,
            )
        return HookDecision()


class DecisionConsumerAdapter(AgentHook):
    """Wrap a legacy ``decision_consumer`` callback as a hook.

    The legacy contract: an async callable taking the turn request
    and returning either a reply (meaning "I handled it; short-circuit")
    or ``None`` (meaning "fall through to normal processing").

    We translate that to a ``HookDecision``:
      - non-None return → ``short_circuit_result=(content, media)``
      - None return     → pass-through

    Exceptions in the underlying callback are logged and treated as a
    pass-through (same as the inline try/except this replaces).
    """

    def __init__(self, callback: _DecisionConsumerCallable) -> None:
        self._callback = callback

    @property
    def name(self) -> str:
        return "Legacy(decision_consumer)"

    async def before_user_inbound(
        self, ctx: AgentHookContext
    ) -> HookDecision:
        req = ctx.turn_request
        if req is None:
            return HookDecision()
        try:
            handled = await self._callback(req)
        except Exception as exc:  # noqa: BLE001 — match legacy semantics
            logger.warning(
                "decision_consumer legacy callback raised %s: %s",
                type(exc).__name__, exc,
            )
            return HookDecision()
        if handled is not None:
            # The consumer replies with a MenuReply; _process_message now
            # carries a reply as a (content, media) tuple, so the short-circuit
            # result must match that shape.
            return HookDecision(
                short_circuit_result=(handled.content, handled.media or [])
            )
        return HookDecision()


class ResponseModifierAdapter(AgentHook):
    """Wrap a legacy ``response_modifier`` callback as a hook.

    Legacy contract: ``(session_key, content) -> str``. Synchronous,
    returns the new content (which may equal the input if no change).

    Translation:
      - call the modifier with ``(ctx.session_key, ctx.outbound_content)``
      - if it returns a string distinct from the input, emit
        ``HookDecision(modified_content=<new>)``
      - exceptions are logged and treated as pass-through (matches the
        inline try/except this replaces)

    The modifier is called even when ``outbound_content`` is empty
    (matching the legacy code path), since callers may want to inject
    content into an empty reply.
    """

    def __init__(self, callback: _ResponseModifierCallable) -> None:
        self._callback = callback

    @property
    def name(self) -> str:
        return "Legacy(response_modifier)"

    async def after_send(self, ctx: AgentHookContext) -> HookDecision:
        original = ctx.outbound_content or ""
        try:
            modified = self._callback(ctx.session_key, original)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "response_modifier legacy callback raised %s: %s",
                type(exc).__name__, exc,
            )
            return HookDecision()
        if not isinstance(modified, str) or modified == original:
            return HookDecision()
        return HookDecision(modified_content=modified)


__all__ = [
    "OnUserInboundAdapter",
    "DecisionConsumerAdapter",
    "ResponseModifierAdapter",
]
