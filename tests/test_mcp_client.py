"""MCP tool wrapper result verdicts (ok flag on failures)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from raven.agent.tools.base import ToolResult
from raven.mcp.client import MCPToolWrapper


class _Session:
    def __init__(self, outcome: object | Exception) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, arguments: dict) -> object:
        self.calls.append((name, arguments))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _wrapper(session: _Session) -> MCPToolWrapper:
    tool_def = SimpleNamespace(
        name="read",
        description="read something",
        inputSchema={"type": "object", "properties": {}},
    )
    return MCPToolWrapper(session, "server-x", tool_def, tool_timeout=30, taken=frozenset())


def _result(*, is_error: bool, text: str) -> SimpleNamespace:
    from mcp.types import TextContent

    return SimpleNamespace(isError=is_error, content=[TextContent(type="text", text=text)])


async def test_an_is_error_result_carries_ok_false() -> None:
    session = _Session(_result(is_error=True, text="permission denied"))

    result = await _wrapper(session).execute(arguments={})

    assert isinstance(result, ToolResult)
    assert result.model_text == "permission denied"
    assert result.ok is False, "isError is the producer's verdict, not a text prefix"
    assert session.calls == [("read", {"arguments": {}})]


async def test_a_clean_result_stays_a_bare_string() -> None:
    session = _Session(_result(is_error=False, text="all good"))

    result = await _wrapper(session).execute(arguments={})

    assert result == "all good"
    assert isinstance(result, str)


async def test_a_timeout_reports_failure() -> None:
    session = _Session(asyncio.TimeoutError())

    result = await _wrapper(session).execute(arguments={})

    assert isinstance(result, ToolResult) and result.ok is False


async def test_a_generic_failure_reports_failure() -> None:
    session = _Session(RuntimeError("boom"))

    result = await _wrapper(session).execute(arguments={})

    assert isinstance(result, ToolResult) and result.ok is False
