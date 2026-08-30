"""Assembly-root helper for AgentLoop's hook chain.

Composes a :class:`CompositeHook` from optional sub-stacks:

- Eval Engine hooks from :func:`build_eval_stack`.
- Caller-supplied hooks (the entrances' adapter-wrapped callbacks among them;
  the loop takes finished hooks only).

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

    The entrances wrap their own callbacks into adapter hooks and pass them
    here as ``extra_hooks``; nothing is wrapped on their behalf.
    """
    chain = CompositeHook()
    if eval_engine is not None:
        chain.extend(eval_engine.hooks())
        logger.debug("Hooks stack: added %d Eval Engine hooks", len(eval_engine.hooks()))
    if extra_hooks is not None:
        for hook in extra_hooks:
            chain.append(hook)
            logger.debug("Hooks stack: appended %s", hook.name)
    return chain


__all__ = ["build_hooks_stack"]
