"""The background-service contribution: a resident host runs it, and owns it.

A product whose watching outlives any turn (an event poller pulling keyed
wakes forward, a queue drainer) contributes a service the way it contributes
a tool: a manifest entry, a factory taking ``PluginContext``, and -- when it
declares ``bind_runtime`` -- the late-bound ``RuntimeHandles`` grants. Two
disciplines are the paper, not a convention:

- **A service never mutates the host's assembly.** It is a dumb loop on the
  B side of the seam: it may consume its grants and its own state, and it
  reaches nothing else -- the generation doors law governs everything it
  could wish to change.
- **An error stops it loudly.** A service that raises is stopped and
  reported, never silently restarted: a watcher that is secretly dead is the
  exact lie the oncall product exists to prevent.

Only a resident host starts services (a one-shot turn never does), the host
owns the lifecycle -- started after assembly, cancelled at disposal with the
generation's other organs -- and ``stop`` must be idempotent.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PluginService(Protocol):
    """What a ``[[plugin.contributes.services]]`` factory returns."""

    async def start(self, handles: Any) -> None: ...

    async def stop(self) -> None: ...


__tier__ = "contract"
__all__ = ["PluginService"]
