"""Channel-bound tools: what a turn's channel is allowed to see.

One gateway process answers the web channel and every enabled IM channel from a
single registry, so "which tools exist" is a per-process fact while "which tools
work here" is a per-turn one. These cover the second.
"""

from __future__ import annotations

import asyncio
from typing import Any

from raven.agent.tools.base import Tool
from raven.agent.tools.registry import ToolRegistry


class _Everywhere(Tool):
    @property
    def name(self) -> str:
        return "everywhere"

    @property
    def description(self) -> str:
        return "Works on any channel."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


class _WebOnly(_Everywhere):
    channels = frozenset({"web"})

    @property
    def name(self) -> str:
        return "web_only"


def _registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register(_Everywhere())
    r.register(_WebOnly())
    return r


def _names(reg: ToolRegistry) -> set[str]:
    return {d["function"]["name"] for d in reg.get_definitions()}


def test_unset_channel_advertises_everything() -> None:
    """The sub-agent and curator registries never set a channel, and they must
    keep seeing the full set rather than silently losing tools."""
    assert _names(_registry()) == {"everywhere", "web_only"}


def test_matching_channel_sees_the_bound_tool() -> None:
    reg = _registry()
    reg.set_channel("web")
    assert _names(reg) == {"everywhere", "web_only"}


def test_other_channel_does_not_see_the_bound_tool() -> None:
    reg = _registry()
    reg.set_channel("whatsapp")
    assert _names(reg) == {"everywhere"}


def test_channel_is_turn_local_not_shared() -> None:
    """Two channels are answered concurrently by one registry. A plain attribute
    would let whichever turn ran last decide what the other one is shown, which
    is the bug this filter exists to prevent -- not one it may introduce."""
    reg = _registry()
    seen: dict[str, set[str]] = {}

    async def turn(channel: str, hold: asyncio.Event, release: asyncio.Event) -> None:
        reg.set_channel(channel)
        release.set()
        await hold.wait()
        seen[channel] = _names(reg)

    async def drive() -> None:
        a_hold, a_set = asyncio.Event(), asyncio.Event()
        b_hold, b_set = asyncio.Event(), asyncio.Event()
        ta = asyncio.create_task(turn("web", a_hold, a_set))
        tb = asyncio.create_task(turn("whatsapp", b_hold, b_set))
        await a_set.wait()
        await b_set.wait()
        # Both turns have set their channel; only now let either read back.
        a_hold.set()
        b_hold.set()
        await asyncio.gather(ta, tb)

    asyncio.run(drive())

    assert seen["web"] == {"everywhere", "web_only"}
    assert seen["whatsapp"] == {"everywhere"}
