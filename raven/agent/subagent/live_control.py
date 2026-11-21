"""Control a single delegated execution for the lifetime of its backend call."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from raven.agent.subagent.activity import RunActivity


@dataclass
class LiveDelegation:
    """A control target pinned to one backend call, including across an ACP hold."""

    call_id: str
    agent: str
    instance: str
    interrupt: Callable[[], None]
    report_now: Callable[[], Awaitable[str]] | None = None
    active: bool = True
    interrupt_requested: bool = False
    report_requested: bool = False
    report_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def request(self, mode: str) -> str:
        if not self.active:
            return "not_running"
        if mode == "interrupt":
            if not self.interrupt_requested:
                self.interrupt()
                self.interrupt_requested = True
            return "interrupt_requested"
        async with self.report_lock:
            if not self.active or self.interrupt_requested:
                return "not_running"
            if self.report_requested:
                return "report_requested"
            reporter = self.report_now
            if reporter is None:
                return "unsupported"
            status = await reporter()
            if status == "report_requested":
                self.report_requested = True
            return status

    def arguments(self) -> dict[str, str]:
        return {"agent": self.agent, "instance": self.instance, "call_id": self.call_id}

    def notice(self, elapsed: int, *, controls: bool, run_id: str | None = None, node_id: str | None = None) -> str:
        label = f"Subagent {self.agent!r}, instance {self.instance!r}, call {self.call_id!r}"
        if run_id:
            label += f", DAG {run_id!r}, node {node_id!r}"
        text = f"{label} has been running for {elapsed} seconds and is still active."
        if controls:
            args = self.arguments()
            text += (
                '\nYou can keep waiting or call tool_call with name "cancel_subagent" and arguments '
                + json.dumps(args)
                + " to interrupt this execution."
            )
            if self.report_now is not None:
                text += "\nTo request a final reply, use arguments " + json.dumps({**args, "mode": "report_now"}) + "."
        return text


@contextmanager
def controlling(
    run: RunActivity,
    control: LiveDelegation,
    *,
    interval: float = 0,
    announce: Callable[[int], Awaitable[None]] | None = None,
) -> Iterator[None]:
    """Publish a control and watch elapsed time while its owner is running."""
    run.control = control
    watcher = None
    if announce and interval > 0:
        began = asyncio.get_running_loop().time()
        watcher = asyncio.create_task(_watch(control, began, interval, announce))
    try:
        yield
    finally:
        control.active = False
        run.control = None
        if watcher is not None:
            watcher.cancel()


async def _watch(
    control: LiveDelegation, began: float, interval: float, announce: Callable[[int], Awaitable[None]]
) -> None:
    clock = asyncio.get_running_loop().time
    notice_at = began + interval
    while control.active:
        await asyncio.sleep(max(0, notice_at - clock()))
        if control.active and not control.interrupt_requested:
            try:
                await announce(int(clock() - began))
            except Exception:  # noqa: BLE001 - advisory notice cannot fail delegation
                logger.opt(exception=True).warning("Could not notify host about delegation {}", control.call_id)
        notice_at = clock() + interval
