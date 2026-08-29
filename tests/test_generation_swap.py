"""Generation model: RavenRuntime.dispose retires one generation in order.

The gateway swaps generations at a turn boundary (BUILD N+1 first, then
SWAP, then DISPOSE N -- see ``_serve_generations`` in
``raven/cli/gateway_commands.py``). DISPOSE is the step with an ordering
contract: sub-agents cancel before the MCP servers close, the loop stops
before the backend drains, and the backend stops last. These tests pin that
order against stubs, because a real gateway cannot be driven under unit
test (see the note in ``test_cli_gateway_commands.py``).
"""

from __future__ import annotations

import asyncio

from raven.core.runtime import RavenRuntime


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []


class _StubSubagents:
    def __init__(self, rec: _Recorder) -> None:
        self._rec = rec

    async def cancel_all(self) -> None:
        self._rec.calls.append("subagents.cancel_all")


class _StubLoop:
    def __init__(self, rec: _Recorder) -> None:
        self._rec = rec
        self.subagents = _StubSubagents(rec)

    async def close_mcp(self) -> None:
        self._rec.calls.append("close_mcp")

    def stop(self) -> None:
        self._rec.calls.append("stop")

    async def drain_backend_stores(self) -> None:
        self._rec.calls.append("drain_backend_stores")


class _StubBackend:
    def __init__(self, rec: _Recorder) -> None:
        self._rec = rec

    async def stop(self) -> None:
        self._rec.calls.append("backend.stop")


def _runtime(rec: _Recorder, *, backend: bool) -> RavenRuntime:
    return RavenRuntime(
        loop=_StubLoop(rec),
        plugin_registry=None,
        backend=_StubBackend(rec) if backend else None,
        strategies=None,
        deliverables=None,
    )


def test_dispose_order_with_backend() -> None:
    rec = _Recorder()
    asyncio.run(_runtime(rec, backend=True).dispose())
    assert rec.calls == [
        "subagents.cancel_all",
        "close_mcp",
        "stop",
        "drain_backend_stores",
        "backend.stop",
    ]


def test_dispose_without_backend_skips_the_drain() -> None:
    rec = _Recorder()
    asyncio.run(_runtime(rec, backend=False).dispose())
    assert rec.calls == ["subagents.cancel_all", "close_mcp", "stop"]
