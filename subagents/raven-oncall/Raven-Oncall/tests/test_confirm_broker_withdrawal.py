"""A confirm that stopped being pending is withdrawn from the screen.

Same defect and same fix as the ask-user prompt: the box is drawn from
``confirm.request`` and was taken down only by an answer or by the end-of-turn
overlay reset, and a wake turn reaches neither -- the hard limit fires long
before the turn ends, and a cron turn runs in a conversation no TUI session
subscribes to, so its end is never delivered at all.

The failure direction is what makes it worth a frame: the box reads as "it is
waiting for me" while the caller has already taken the default and moved on.
"""

from __future__ import annotations

import asyncio

from raven.tui_rpc.confirm_broker import ConfirmBroker


def _frames() -> tuple[list[dict], object]:
    frames: list[dict] = []

    async def send_frame(frame: dict) -> None:
        frames.append(frame)

    return frames, send_frame


async def _wait(frames: list[dict], n: int = 1, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while len(frames) < n:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"expected {n} frame(s), saw {len(frames)}")
        await asyncio.sleep(0.005)


async def test_a_timed_out_confirm_is_withdrawn(monkeypatch) -> None:
    monkeypatch.setattr("raven.tui_rpc.confirm_broker._CONFIRM_HARD_LIMIT_S", 0.05)
    frames, send_frame = _frames()
    broker = ConfirmBroker(send_frame)

    assert await broker.await_confirm("delete it?", default=False) is False
    assert [f["method"] for f in frames] == ["confirm.request", "confirm.cancel"]
    assert frames[1]["params"]["reason"] == "timeout"


async def test_the_withdrawal_names_the_confirm_it_takes_down(monkeypatch) -> None:
    """Without the id a late withdrawal would remove a newer prompt, leaving a
    caller blocked on a question with nothing on screen to answer."""
    monkeypatch.setattr("raven.tui_rpc.confirm_broker._CONFIRM_HARD_LIMIT_S", 0.05)
    frames, send_frame = _frames()
    broker = ConfirmBroker(send_frame)

    await broker.await_confirm("delete it?", default=True)

    assert frames[1]["params"]["request_id"] == frames[0]["params"]["request_id"]


async def test_an_answered_confirm_is_not_withdrawn() -> None:
    frames, send_frame = _frames()
    broker = ConfirmBroker(send_frame)

    task = asyncio.create_task(broker.await_confirm("delete it?", default=False))
    await _wait(frames)
    broker.resolve(frames[0]["params"]["request_id"], True)

    assert await task is True
    assert [f["method"] for f in frames] == ["confirm.request"]


async def test_a_failed_withdrawal_still_returns_the_default(monkeypatch) -> None:
    """The caller is a worker thread that must always get a bool back."""
    monkeypatch.setattr("raven.tui_rpc.confirm_broker._CONFIRM_HARD_LIMIT_S", 0.05)

    async def send_frame(frame: dict) -> None:
        if frame["method"] == "confirm.cancel":
            raise ConnectionResetError("frontend gone")

    assert await ConfirmBroker(send_frame).await_confirm("?", default=True) is True
