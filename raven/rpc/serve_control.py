"""What a running page host exposes to handlers that must restart it.

``system.upgrade`` needs four things this layer cannot know on its own: which
port to come back on, which shared secret and which browser session cookie to
keep (an open browser holds the cookie, and a relauncher holds the token --
carrying only one of them would sign somebody out), and a way to end the
serve loop *after* its reply has been flushed.

The class lives on the READING side on purpose: rpc owns the shape of what it
needs, and the hosting entrance (`raven serve`, the gateway's page) arms and
disarms the singleton at boot -- an entrance may import this layer, never the
other way around.
"""

from __future__ import annotations

import asyncio
from typing import Callable, Optional


class ServeControl:
    def __init__(self) -> None:
        self.port: Optional[int] = None
        self.token: Optional[str] = None
        self.cookie: Optional[str] = None
        self._stop: Optional[asyncio.Event] = None
        self._gateway_stop: Optional[Callable[[], None]] = None
        self._gateway_busy: Optional[Callable[[], Optional[dict]]] = None
        self.supervisor_pid: Optional[int] = None
        self.hosted_by_gateway = False

    def arm(self, port: int, token: str, cookie: str, stop: asyncio.Event) -> None:
        self.port, self.token, self.cookie, self._stop = port, token, cookie, stop

    def arm_hosted(self, port: int, token: str, cookie: str) -> None:
        """The gateway hosts the page: record the endpoint facts.

        No stop handle yet. The page is mounted before the gateway has built
        the graceful stop it answers the control plane with, so that handle
        arrives later through ``hand_over``. Until it does, a hosted page is
        not ``running`` and ``system.upgrade`` refuses rather than guess.
        """
        self.port, self.token, self.cookie = port, token, cookie
        self.hosted_by_gateway = True

    def hand_over(
        self,
        stop: Callable[[], None],
        busy: Callable[[], Optional[dict]],
        supervisor_pid: Optional[int],
    ) -> None:
        """The gateway's own graceful stop, and its own answer to "is work running?".

        Not the page mount's stop event: that one ends the announcer and
        nothing else, and the process has to exit for anything to replace it.
        ``stop`` is the path `gateway.shutdown` takes -- the whole teardown
        chain, IM channels included, ending in a zero exit that the `raven web`
        supervisor reads as "stand down" rather than as a crash to undo.
        ``busy`` is the check the gateway already refuses a config swap on.

        ``supervisor_pid`` is the `raven web` supervisor that will stand down
        with this process, or None when nothing would bring it back (started by
        hand, or under `raven web --foreground`). The gateway answers it because
        only the entrance layer can read the supervisor's state file.
        """
        self._gateway_stop, self._gateway_busy = stop, busy
        self.supervisor_pid = supervisor_pid

    def disarm(self) -> None:
        self.port = self.token = self.cookie = self._stop = None
        self._gateway_stop = self._gateway_busy = None
        self.supervisor_pid = None
        self.hosted_by_gateway = False

    @property
    def running(self) -> bool:
        return self._stop is not None or self._gateway_stop is not None

    def busy(self) -> Optional[dict]:
        """What is still running that a restart would cut off, or None."""
        return self._gateway_busy() if self._gateway_busy is not None else None

    def request_shutdown(self) -> bool:
        if self._stop is not None:
            self._stop.set()
            return True
        if self._gateway_stop is not None:
            self._gateway_stop()
            return True
        return False


SERVE = ServeControl()

__all__ = ["SERVE", "ServeControl"]
