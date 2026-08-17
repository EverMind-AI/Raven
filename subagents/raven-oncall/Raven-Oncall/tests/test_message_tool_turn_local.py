"""MessageTool per-turn state is turn-local: concurrent turns running in
their own tasks do not clobber each other's reply routing."""

import asyncio

import pytest

from raven.agent.tools.message import MessageTool

pytestmark = pytest.mark.asyncio


async def test_concurrent_turns_do_not_clobber_message_routing():
    tool = MessageTool()
    sink_a: list[str] = []
    sink_b: list[str] = []

    async def cb_a(content: str, media: list[str]) -> None:
        sink_a.append(content)

    async def cb_b(content: str, media: list[str]) -> None:
        sink_b.append(content)

    barrier = asyncio.Barrier(2)

    async def turn(channel: str, chat_id: str, cb, content: str) -> None:
        tool.set_context(channel, chat_id)
        tool.set_send_callback(cb)
        # Both turns finish setting their context before either sends, so a
        # shared-instance implementation would route both to the last setter.
        await barrier.wait()
        await tool.execute(content=content)

    await asyncio.gather(
        asyncio.create_task(turn("telegram", "A", cb_a, "to-A")),
        asyncio.create_task(turn("weixin", "B", cb_b, "to-B")),
    )

    assert sink_a == ["to-A"]
    assert sink_b == ["to-B"]
    # The test's own task never set a context/callback, so it sees the default.
    assert tool.sent_in_turn is False
