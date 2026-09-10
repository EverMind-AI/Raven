"""The default Capability strategy: the registry's own view, unchanged.

``ToolRegistry.get_definitions`` is already the answer to "what does this
iteration show the model" -- it applies the session overlay, the withheld
set, the schema-hidden declarations, the dynamic-schema tools and this
turn's registered names, in that order. So the default reads it and reports
it, which keeps the array the model receives byte-identical to the array it
received before this seam existed.

The registry is asked per iteration rather than captured once because it is
live within a turn in one direction: a tool can be turned off mid-turn (the
withheld union only grows), while a late registration does not appear until
the next turn -- the array is the prompt-cache prefix and must not move
between two model calls of one turn.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from raven.contracts.harness import CapabilityRequest, CapabilitySelection

if TYPE_CHECKING:
    from raven.agent.tools.registry import ToolRegistry


class DefaultCapability:
    """Report the tool definitions the registry currently offers.

    Takes a provider callable rather than the registry itself: the loop
    rebuilds its registry across a generation swap, and a captured instance
    would keep answering for the retired one.
    """

    def __init__(self, registry_provider: Callable[[], "ToolRegistry"]) -> None:
        self._registry_provider = registry_provider

    async def select(self, request: CapabilityRequest) -> CapabilitySelection:
        return CapabilitySelection(tools=self._registry_provider().get_definitions())


__all__ = ["DefaultCapability"]
