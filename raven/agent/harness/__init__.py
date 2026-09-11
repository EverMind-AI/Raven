"""The four harness strategy roles, and the default set the loop runs on.

``default_harness_modules`` is the behaviour-preserving set: Memory is this
generation's context engine plus the window's own turn decisions, Planning
passes messages through, Capability reports the registry's own view, and
Action dispatches the one call the loop used to dispatch inline. Binding it
changes nothing a turn does -- that is the point, and it is what the
same-query comparison against the previous revision checks.

Every organ reaches the roles through a callable rather than a captured
instance: a live ``/model`` switch rebuilds the provider and the window, and
a hot config apply rebuilds the registry, so anything read once at assembly
would answer for the retired one.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from raven.agent.harness.action import DefaultAction
from raven.agent.harness.capability import DefaultCapability
from raven.agent.harness.memory import DefaultMemory
from raven.agent.harness.memory import bind as bind_memory
from raven.agent.harness.planning import DefaultPlanning
from raven.contracts.harness import HarnessModules

if TYPE_CHECKING:
    from raven.agent.tools.registry import ToolRegistry
    from raven.contracts.context import ContextEngine


def default_harness_modules(
    engine: "ContextEngine",
    registry_provider: Callable[[], "ToolRegistry"],
    *,
    provider: Callable[[], Any],
    model: Callable[[], str],
    context_window_tokens: Callable[[], int],
    system_prompt: Callable[[list[Any] | None], str],
) -> HarnessModules:
    """Assemble the default four around this generation's own organs."""
    memory = DefaultMemory(
        engine,
        provider=provider,
        model=model,
        context_window_tokens=context_window_tokens,
        tool_definitions=lambda: registry_provider().get_definitions(),
        system_prompt=system_prompt,
    )
    return HarnessModules(
        memory=bind_memory(memory),
        planning=DefaultPlanning(),
        capability=DefaultCapability(registry_provider),
        action=DefaultAction(),
    )


__all__ = [
    "DefaultAction",
    "DefaultCapability",
    "DefaultMemory",
    "DefaultPlanning",
    "HarnessModules",
    "default_harness_modules",
]
