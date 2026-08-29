"""Generation model: RavenRuntime.dispose retires one generation in order.

The gateway swaps generations at a turn boundary (BUILD N+1 first, then
SWAP, then DISPOSE N -- see ``_serve_generations`` in
``raven/cli/gateway_commands.py``). DISPOSE is the step with an ordering
contract: sub-agents cancel before the MCP servers close, the loop stops
before the backend drains, and the backend stops last. These tests pin that
order against stubs, because a real gateway cannot be driven under unit
test (see the note in ``test_cli_gateway_commands.py``).
"""

from __future__ import annotations

import asyncio

from raven.core.runtime import RavenRuntime


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []


class _StubSubagents:
    def __init__(self, rec: _Recorder) -> None:
        self._rec = rec

    async def cancel_all(self) -> None:
        self._rec.calls.append("subagents.cancel_all")


class _StubLoop:
    def __init__(self, rec: _Recorder) -> None:
        self._rec = rec
        self.subagents = _StubSubagents(rec)

    async def close_mcp(self) -> None:
        self._rec.calls.append("close_mcp")

    def stop(self) -> None:
        self._rec.calls.append("stop")

    async def drain_backend_stores(self) -> None:
        self._rec.calls.append("drain_backend_stores")


class _StubBackend:
    def __init__(self, rec: _Recorder) -> None:
        self._rec = rec

    async def stop(self) -> None:
        self._rec.calls.append("backend.stop")


def _runtime(rec: _Recorder, *, backend: bool) -> RavenRuntime:
    return RavenRuntime(
        loop=_StubLoop(rec),
        plugin_registry=None,
        backend=_StubBackend(rec) if backend else None,
        strategies=None,
        deliverables=None,
    )


def test_dispose_order_with_backend() -> None:
    rec = _Recorder()
    asyncio.run(_runtime(rec, backend=True).dispose())
    assert rec.calls == [
        "subagents.cancel_all",
        "close_mcp",
        "stop",
        "drain_backend_stores",
        "backend.stop",
    ]


def test_dispose_without_backend_skips_the_drain() -> None:
    rec = _Recorder()
    asyncio.run(_runtime(rec, backend=False).dispose())
    assert rec.calls == ["subagents.cancel_all", "close_mcp", "stop"]


# ---------------------------------------------------------------------------
# SwapCoordinator: one swap at a time, released only right before run()
# ---------------------------------------------------------------------------


def _candidate(rec: _Recorder | None = None):
    from raven.core.runtime import SwapCandidate

    return SwapCandidate(
        config=None, ec_config=None, provider=None, router=None, runtime=_runtime(rec or _Recorder(), backend=False)
    )


def test_a_second_request_while_a_swap_is_in_flight_is_refused() -> None:
    from raven.core.runtime import SwapCoordinator

    clock = [100.0]
    swaps = SwapCoordinator(min_interval_s=5.0, clock=lambda: clock[0])
    # Born claimed: generation 1 is still being wired until the first release().
    assert swaps.in_flight and swaps.begin() == "booting"
    swaps.release()
    assert swaps.begin() is None
    assert swaps.begin() == "swap_in_flight"
    swaps.stage(_candidate())
    # Still in flight through take() (the loop stopped, wiring is next)...
    assert swaps.take() is not None
    assert swaps.begin() == "swap_in_flight"
    # ...until release(), which the serving loop calls right before run().
    swaps.release()
    assert swaps.generation == 2
    clock[0] += 10.0
    assert swaps.begin() is None


def test_accepted_swaps_are_rate_limited() -> None:
    from raven.core.runtime import SwapCoordinator

    clock = [0.0]
    swaps = SwapCoordinator(min_interval_s=5.0, clock=lambda: clock[0])
    swaps.release()
    assert swaps.begin() is None
    swaps.abort()
    clock[0] = 2.0
    assert swaps.begin() == "too_soon"
    clock[0] = 6.0
    assert swaps.begin() is None


def test_a_failed_build_gives_the_slot_back_without_a_generation_bump() -> None:
    from raven.core.runtime import SwapCoordinator

    clock = [0.0]
    swaps = SwapCoordinator(min_interval_s=0.0, clock=lambda: clock[0])
    swaps.release()
    assert swaps.begin() is None
    swaps.abort()
    swaps.release()
    assert swaps.generation == 1
    assert swaps.take() is None


def test_a_superseded_candidate_is_discarded_not_disposed() -> None:
    from raven.core.runtime import SwapCoordinator

    swaps = SwapCoordinator(min_interval_s=0.0, clock=lambda: 0.0)
    swaps.release()
    first_rec = _Recorder()
    first = _candidate(first_rec)
    discarded: list[str] = []
    first.runtime.discard = lambda: discarded.append("first")  # type: ignore[method-assign]
    swaps.stage(first)
    swaps.stage(_candidate())
    assert discarded == ["first"]
    assert first_rec.calls == []  # dispose's stop sequence never ran on it


def test_trigger_tasks_are_held_until_done() -> None:
    from raven.core.runtime import SwapCoordinator

    swaps = SwapCoordinator(min_interval_s=0.0, clock=lambda: 0.0)

    async def _run() -> None:
        async def _noop() -> None:
            return None

        task = asyncio.create_task(_noop())
        swaps.track(task)
        assert task in swaps._tasks
        await task
        await asyncio.sleep(0)
        assert task not in swaps._tasks

    asyncio.run(_run())


def test_release_bumps_the_generation_exactly_once_per_swap() -> None:
    from raven.core.runtime import SwapCoordinator

    swaps = SwapCoordinator(min_interval_s=0.0, clock=lambda: 0.0)
    swaps.release()  # boot
    assert swaps.generation == 1
    assert swaps.begin() is None
    swaps.stage(_candidate())
    assert swaps.take() is not None
    swaps.release()
    swaps.release()  # a stray second release must not count a second swap
    assert swaps.generation == 2
    assert swaps.in_flight is False


def test_a_staged_candidate_survives_a_release_without_a_take() -> None:
    """release() without take() is the wiring path ending before the loop
    stopped for the swap; the candidate is still owed to the next take()."""
    from raven.core.runtime import SwapCoordinator

    swaps = SwapCoordinator(min_interval_s=0.0, clock=lambda: 0.0)
    swaps.release()
    assert swaps.begin() is None
    swaps.stage(_candidate())
    swaps.release()
    assert swaps.generation == 1
    assert swaps.take() is not None
