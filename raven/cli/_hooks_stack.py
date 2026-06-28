"""CLI assembly helper for AgentLoop's hook chain.

Composes a :class:`CompositeHook` from optional sub-stacks:

- Legacy Sentinel callbacks (still passed through AgentLoop's
  on_user_inbound / decision_consumer / response_modifier params —
  the AgentLoop constructor auto-wraps them via the legacy-callback
  adapters and registers them into ``self.hooks``; ``build_hooks_stack``
  does NOT duplicate that wiring).
- Eval Engine hooks from :func:`build_eval_stack`.
- Future caller-supplied hooks.

The helper is intentionally thin — most callers just hand
``EvalEngine.hooks()`` to AgentLoop's ``hooks=...`` constructor
parameter. ``build_hooks_stack`` is for callers that want to assemble
a chain across multiple sub-engines before the AgentLoop is
constructed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Iterable

from raven.agent.hook import AgentHook, CompositeHook

if TYPE_CHECKING:
    from raven.eval_engine import EvalEngine

logger = logging.getLogger(__name__)


def build_hooks_stack(
    *,
    eval_engine: "EvalEngine | None" = None,
    extra_hooks: Iterable[AgentHook] | None = None,
) -> CompositeHook:
    """Build a :class:`CompositeHook` from optional contributing engines.

    Order (matches the documented priority in agent/hook/__init__):
      1. Eval Engine's three hooks (before_iteration → tool_audit →
         after_iteration). All three are no-ops in default config.
      2. Caller-supplied ``extra_hooks``.

    The Sentinel adapter hooks (OnUserInboundAdapter / DecisionConsumerAdapter
    / ResponseModifierAdapter) are NOT added here — AgentLoop's constructor
    auto-wraps the matching legacy parameters into adapters and inserts
    them around any ``hooks=`` argument it receives. See
    ``raven.agent.loop.main.AgentLoop.__init__`` for the canonical
    ordering rationale.
    """
    chain = CompositeHook()
    if eval_engine is not None:
        chain.extend(eval_engine.hooks())
        logger.debug(
            "Hooks stack: added %d Eval Engine hooks", len(eval_engine.hooks())
        )
    if extra_hooks is not None:
        for hook in extra_hooks:
            chain.append(hook)
            logger.debug("Hooks stack: appended %s", hook.name)
    return chain


__all__ = ["build_hooks_stack"]
