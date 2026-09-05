"""OriginPools: per-origin pool routing with no cross-pool borrowing."""

import asyncio

import pytest

from raven.spine import ChatType, Origin, Source, TurnOutcome, TurnRequest, Usage
from raven.spine.scheduler import Lane, OriginPools


def _req(origin: Origin, text: str = "x") -> TurnRequest:
    src = Source(channel="t", chat_id=text, sender_id="u", chat_type=ChatType.DM)
    return TurnRequest(origin=origin, source=src, text=text)


def _direct_req(text: str = "d") -> TurnRequest:
    src = Source(channel="t", chat_id=text, sender_id="u", chat_type=ChatType.DM)
    return TurnRequest(origin=Origin.USER, source=src, text=text, direct_target=("agent", "inst"))


async def _sink(event) -> None:
    pass


class Quick:
    async def run(self, req, emit, drain) -> TurnOutcome:
        return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=False)


def test_for_origin_maps_user_separately_from_proactive_origins():
    pools = OriginPools(user=1, system=1)
    user = pools._for_origin(Origin.USER)
    system = pools._for_origin(Origin.CRON)
    assert user is not system
    for origin in (Origin.SENTINEL, Origin.CRON, Origin.HEARTBEAT, Origin.SUBAGENT):
        assert pools._for_origin(origin) is system  # proactive origins share the system pool


def test_for_origin_rejects_unknown_origin():
    # fail-loud: a value outside the mapped set must raise, not fall through to
    # the system pool — a new origin has to consciously pick a pool.
    pools = OriginPools(user=1, system=1)
    with pytest.raises(ValueError):
        pools._for_origin("not-an-origin")


async def test_user_pool_is_independent_of_a_full_system_pool():
    # Per-event guarantee: a user turn never waits for an LLM slot behind a
    # proactive task. Hold the only system slot; a USER turn still runs.
    pools = OriginPools(user=1, system=1)
    await pools._for_origin(Origin.CRON).acquire()  # system pool now full
    lane = Lane(runner=Quick(), pools=pools, sink=_sink, conversation_id="c")
    fut = lane.submit(_req(Origin.USER, "user"))
    assert isinstance(await asyncio.wait_for(fut, timeout=1.0), TurnOutcome)  # ran despite full system pool


async def test_no_cross_pool_borrow():
    # A full user pool blocks a user turn even when the system pool is idle.
    pools = OriginPools(user=1, system=5)
    await pools._for_origin(Origin.USER).acquire()  # hold the only user slot
    lane = Lane(runner=Quick(), pools=pools, sink=_sink, conversation_id="c")
    blocked = lane.submit(_req(Origin.USER, "b"))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(blocked), timeout=0.1)  # stays blocked, no borrow
    pools._for_origin(Origin.USER).release()  # free the slot; the turn then runs
    assert isinstance(await asyncio.wait_for(blocked, timeout=1.0), TurnOutcome)  # then runs


def test_a_direct_chat_routes_to_the_direct_pool_not_the_user_pool():
    # A direct chat IS a user turn; the request field, not the origin, picks
    # the pool -- several sub-agents answering must not make the user queue.
    pools = OriginPools(user=1, system=1, direct=1)
    assert pools.for_request(_direct_req()) is pools._direct
    assert pools.for_request(_req(Origin.USER)) is pools._user


async def test_three_pools_coexist_a_full_direct_pool_blocks_no_one_else():
    # Hold the only direct slot: a user turn and a system-origin turn still
    # run, and a second direct chat queues (waits) rather than erroring.
    pools = OriginPools(user=1, system=1, direct=1)
    await pools.for_request(_direct_req()).acquire()
    lane = Lane(runner=Quick(), pools=pools, sink=_sink, conversation_id="c")
    user_fut = lane.submit(_req(Origin.USER, "u"))
    assert isinstance(await asyncio.wait_for(user_fut, timeout=1.0), TurnOutcome)
    cron_fut = lane.submit(_req(Origin.CRON, "s"))
    assert isinstance(await asyncio.wait_for(cron_fut, timeout=1.0), TurnOutcome)
    queued = lane.submit(_direct_req("d2"))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(queued), timeout=0.1)
    pools._direct.release()
    assert isinstance(await asyncio.wait_for(queued, timeout=1.0), TurnOutcome)
