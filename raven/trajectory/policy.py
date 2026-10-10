"""Whether the trajectory view may be served by this process.

The trajectory RPC surface is announced at the handshake (``trajectory-v1``)
only while this policy is enabled, and every handler consults it again before
it answers. Today the only source of truth is a launch flag, armed once before
the first client connects; a settings entry or a developer mode can later
replace the getter without touching the readers, and every replacement bumps
``revision``. A source that can change while clients are connected must also
make them handshake again, since the announcement is read only then.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class TrajectoryPolicy:
    enabled_getter: Callable[[], bool] = field(default=lambda: False)
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
"""The process-wide policy, armed once by the hosting entrance at launch.

Same shape as :data:`raven.rpc.serve_control.SERVE`: the command that starts
the process says whether the view is enabled, and every reader -- the RPC
handlers, the health endpoint -- consults this one object.
"""


def arm(enabled: bool) -> None:
    POLICY.replace_source(lambda: enabled)


def current() -> TrajectoryPolicy:
    return POLICY


def _reset_for_tests() -> None:
    POLICY.enabled_getter = lambda: False
    POLICY.revision = 0


__all__ = ["POLICY", "TrajectoryPolicy", "arm", "current"]
