"""AgentHook abstraction for AgentLoop lifecycle.

AgentLoop's scattered callback fields (``response_modifier`` /
``on_user_inbound`` / ``decision_consumer`` / ``enable_personalization``)
are ported onto this contract. eval_engine adds the three iteration-phase
hooks (before_iteration / before_execute_tools / after_iteration).

Public surface:

- :class:`AgentHook`        — base class (all methods default to no-op).
- :class:`AgentHookContext` — per-turn state carried through the chain.
- :class:`HookDecision`     — what a hook chose: pass-through,
                              short-circuit, or content modification.
- :class:`CompositeHook`    — aggregate multiple hooks into one,
                              with short-circuit + content-chain
                              semantics and exception isolation.
"""

from raven.agent.hook.adapters import (
    DecisionConsumerAdapter,
    OnUserInboundAdapter,
    ResponseModifierAdapter,
)
from raven.agent.hook.base import AgentHook, AgentHookContext, HookDecision
from raven.agent.hook.composite import CompositeHook

__all__ = [
    "AgentHook",
    "AgentHookContext",
    "HookDecision",
    "CompositeHook",
    # Legacy-callback adapters
    "DecisionConsumerAdapter",
    "OnUserInboundAdapter",
    "ResponseModifierAdapter",
]
