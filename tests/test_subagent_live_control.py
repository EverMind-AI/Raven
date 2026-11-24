"""The long-run notice: how a controlled delegation tells its host it is still working."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from raven.agent.subagent.live_control import LiveDelegation, controlling


def _control() -> LiveDelegation:
    return LiveDelegation("call-1", "agent", "instance", interrupt=lambda: None)


async def _until(predicate, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition was never met")
        await asyncio.sleep(0.005)


async def test_a_finished_control_refuses_a_request() -> None:
    """The control outlives the call only as an object: a request that lands
    after the run ended has nothing to stop or report, and saying so is what
    keeps the caller from believing it reached the execution."""
    control = _control()
    control.active = False
    assert await control.request("interrupt") == "not_running"

    interrupted = _control()
    interrupted.interrupt_requested = True
    assert await interrupted.request("report_now") == "not_running"


async def test_the_notice_offers_report_now_only_when_the_backend_supports_it() -> None:
    """The notice lists the controls a caller can use; offering report_now to a
    lane with no reporter is a call the tool then refuses."""

    async def report() -> str:
        return "report_requested"

    plain = _control()
    assert "report_now" not in plain.notice(90, controls=True)

    with_report = LiveDelegation("call-2", "agent", "instance", interrupt=lambda: None, report_now=report)
    notice = with_report.notice(90, controls=True)
    assert "cancel_subagent" in notice
    assert '"mode": "report_now"' in notice


async def test_the_notice_stops_once_an_interrupt_was_requested() -> None:
    """A person who asked to stop a run does not need to be told it is still
    running: the notice advises them to keep waiting, and the wait is over."""
    run = SimpleNamespace()
    control = _control()
    notices: list[int] = []

    async def announce(elapsed: int) -> None:
        notices.append(elapsed)

    with controlling(run, control, interval=0.01, announce=announce):
        await _until(lambda: notices)
        control.interrupt_requested = True
        settled = len(notices)
        await asyncio.sleep(0.08)
        assert len(notices) == settled

    assert run.control is None


async def test_a_failing_notice_does_not_stop_the_watch() -> None:
    """The notice is advisory: the host going away must not end the watching,
    and the next interval tries again."""
    run = SimpleNamespace()
    control = _control()
    attempts = 0

    async def announce(elapsed: int) -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("host went away")

    with controlling(run, control, interval=0.01, announce=announce):
        await _until(lambda: attempts >= 2)

    assert attempts >= 2
