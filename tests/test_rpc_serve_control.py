"""Tests for the handle a running page host gives ``system.upgrade``."""

from __future__ import annotations

import asyncio

from raven.rpc.serve_control import ServeControl


def test_a_hosted_page_is_not_running_until_the_gateway_hands_over_its_stop() -> None:
    """The page is mounted before the gateway has built its graceful stop, so
    in that gap there is nothing that could end the process."""
    ctl = ServeControl()
    ctl.arm_hosted(18792, "tok", "cookie")

    assert ctl.hosted_by_gateway
    assert not ctl.running
    assert ctl.request_shutdown() is False


def test_the_hand_over_makes_the_gateway_stoppable_through_its_own_stop() -> None:
    stopped: list[bool] = []
    ctl = ServeControl()
    ctl.arm_hosted(18792, "tok", "cookie")
    ctl.hand_over(lambda: stopped.append(True), lambda: None, 4242)

    assert ctl.running
    assert ctl.supervisor_pid == 4242
    assert ctl.request_shutdown() is True
    assert stopped == [True]


def test_busy_is_the_gateway_answer_and_nothing_when_there_is_no_gateway() -> None:
    ctl = ServeControl()
    assert ctl.busy() is None

    ctl.arm_hosted(18792, "tok", "cookie")
    ctl.hand_over(lambda: None, lambda: {"subagents": 2, "questions": 0}, None)

    assert ctl.busy() == {"subagents": 2, "questions": 0}


def test_disarm_forgets_the_gateway_handles_too() -> None:
    """A swap tears the page down and mounts it again. A stop left over from
    the torn-down generation would end the process on behalf of a page that
    no longer exists."""
    ctl = ServeControl()
    ctl.arm_hosted(18792, "tok", "cookie")
    ctl.hand_over(lambda: None, lambda: {"subagents": 1, "questions": 0}, 4242)

    ctl.disarm()

    assert not ctl.running
    assert not ctl.hosted_by_gateway
    assert ctl.busy() is None
    assert ctl.supervisor_pid is None


def test_standalone_serve_still_stops_through_its_event() -> None:
    stop = asyncio.Event()
    ctl = ServeControl()
    ctl.arm(18792, "tok", "cookie", stop)

    assert ctl.running
    assert ctl.request_shutdown() is True
    assert stop.is_set()
