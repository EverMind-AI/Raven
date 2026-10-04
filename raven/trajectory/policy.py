"""Whether the trajectory view may be served by this process.

The policy separates *implementing* the trajectory RPC surface (a static
capability the server always announces) from *enabling* it for the current
process. Today the only source of truth is a launch flag; a settings entry or
a developer mode can later replace the getter without touching the readers,
and every replacement bumps ``revision`` so a client that cached the answer
knows to ask again.
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


__all__ = ["TrajectoryPolicy"]
