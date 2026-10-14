"""Whether the trajectory view may be served by this process.

The trajectory RPC surface is announced at the handshake (``trajectory-v1``)
only while this policy is enabled, and every handler consults it again before
it answers. It is on by default. A settings entry or a developer mode can
replace the getter without touching the readers, and every replacement bumps
``revision``. A source that can change while clients are connected must also
make them handshake again, since the announcement is read only then.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class TrajectoryPolicy:
    enabled_getter: Callable[[], bool] = field(default=lambda: True)
    revision: int = 0

    def enabled(self) -> bool:
        return bool(self.enabled_getter())

    def replace_source(self, getter: Callable[[], bool]) -> None:
        self.enabled_getter = getter
        self.revision += 1

    @classmethod
    def fixed(cls, enabled: bool) -> "TrajectoryPolicy":
        return cls(enabled_getter=lambda: enabled)


POLICY = TrajectoryPolicy()
"""The process-wide policy; every reader -- the RPC handlers, the health
endpoint, the handshake -- consults this one object."""


def arm(enabled: bool) -> None:
    POLICY.replace_source(lambda: enabled)


def current() -> TrajectoryPolicy:
    return POLICY


def _reset_for_tests() -> None:
    POLICY.enabled_getter = lambda: True
    POLICY.revision = 0


__all__ = ["POLICY", "TrajectoryPolicy", "arm", "current"]
