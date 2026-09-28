"""A worker turn waits for the sub-agents it started and runs their announcements as follow-up turns."""

import asyncio
from dataclasses import dataclass

from experimental.curator.raven_adapter.worker import follow_up


@dataclass(frozen=True)
class Announcement:
    text: str
    turn_id: str | None = None


class Background:
    """In flight until each pending result has been announced."""

    def __init__(self, announcements, results):
        self.announcements, self.results = announcements, list(results)

    def __call__(self):
        if self.results:
            self.announcements.put_nowait(self.results.pop(0))
            return True
        return False


async def test_announcements_run_in_order_and_the_turn_ends_when_nothing_is_left():
    announcements, ran = asyncio.Queue(), []
    busy = Background(announcements, [Announcement("page ready")])

    async def run_turn(request):
        ran.append(request)
        if request.text == "page ready":
            announcements.put_nowait(Announcement("follow-up of the follow-up", "t2"))

    await follow_up(busy, announcements, run_turn, poll=0)
    assert [request.text for request in ran] == ["page ready", "follow-up of the follow-up"]
    assert ran[0].turn_id and ran[1].turn_id == "t2"


async def test_a_turn_that_started_nothing_returns_at_once():
    ran = []
    await follow_up(Background(asyncio.Queue(), []), asyncio.Queue(), ran.append, poll=0)
    assert ran == []
