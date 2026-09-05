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
from typing import Optional


class ServeControl:
    def __init__(self) -> None:
        self.port: Optional[int] = None
        self.token: Optional[str] = None
        self.cookie: Optional[str] = None
        self._stop: Optional[asyncio.Event] = None
        self.hosted_by_gateway = False

    def arm(self, port: int, token: str, cookie: str, stop: asyncio.Event) -> None:
        self.port, self.token, self.cookie, self._stop = port, token, cookie, stop

    def arm_hosted(self, port: int, token: str, cookie: str) -> None:
        """The gateway hosts the page: record the endpoint facts, no stop event.

        ``system.upgrade``'s restart flow replaces a `raven serve` process with
        another `raven serve`; ending the gateway's loop that way would drop the
        IM channels and bring back the wrong process. So a hosted page carries
        no shutdown handle, and upgrade refuses with its own reason instead
        (see ``raven.rpc.methods.system.system_upgrade``).
        """
        self.port, self.token, self.cookie = port, token, cookie
        self.hosted_by_gateway = True

    def disarm(self) -> None:
        self.port = self.token = self.cookie = self._stop = None
        self.hosted_by_gateway = False

    @property
    def running(self) -> bool:
        return self._stop is not None

    def request_shutdown(self) -> bool:
        if self._stop is None:
            return False
        self._stop.set()
        return True


SERVE = ServeControl()

__all__ = ["SERVE", "ServeControl"]
