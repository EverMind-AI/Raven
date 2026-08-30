"""AgentHook abstraction for AgentLoop lifecycle.

The loop fires six phases through one ``CompositeHook`` chain: one on the
way in (before_user_inbound), three per ReAct iteration (before_iteration /
before_execute_tools / after_iteration), one when a turn ends with nothing
to show (terminal_answerless), one on the way out (after_send). eval_engine
adds three concrete iteration-phase hooks; a product steers the loop with
its own on the same six.

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
from raven.agent.hook.composite import CompositeHook
from raven.contracts.loop_hooks import AgentHook, AgentHookContext, HookDecision

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
