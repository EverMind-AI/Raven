"""The queue drainer: what finishes a document the gateway stopped indexing."""

from __future__ import annotations

import asyncio

import pytest

from raven.knowledge import _library


class _Manager:
    def __init__(self, tried: int = 0, boom: Exception | None = None) -> None:
        self.tried = tried
        self.boom = boom
        self.calls = 0

    async def index_pending(self, base_id: str = "") -> int:
        self.calls += 1
        if self.boom:
            raise self.boom
        return self.tried


@pytest.fixture
def staged(monkeypatch: pytest.MonkeyPatch):
    made = _Manager()
    monkeypatch.setattr(_library, "knowledge_manager", lambda: made)
    return made


async def test_the_queue_is_drained_once_the_gateway_has_settled(staged) -> None:
    staged.tried = 2

    task = _library.drain_pending_in_background(delay_s=0)
    await task

    assert staged.calls == 1, "the queue nothing else drains"


async def test_a_queue_that_cannot_be_drained_does_not_take_the_boot_with_it(staged) -> None:
    """A boot path answers None rather than failing: a gateway that does not
    start is worse than a document that stays queued."""
    staged.boom = RuntimeError("the embedding endpoint is down")

    task = _library.drain_pending_in_background(delay_s=0)
    await task

    assert task.done() and task.exception() is None


async def test_it_waits_before_it_starts(staged) -> None:
    """The first page and the first turn are answered before this embeds."""
    task = _library.drain_pending_in_background(delay_s=30)
    await asyncio.sleep(0)

    assert staged.calls == 0, "nothing yet"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_outside_a_loop_there_is_nothing_to_drain_into(staged) -> None:
    """Imported by a CLI process with no running loop, it answers None rather
    than raising."""
    assert _library.drain_pending_in_background(delay_s=0) is None
    assert staged.calls == 0
