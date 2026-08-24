"""Pagination tests for MCP tools/list discovery (issue #301)."""

from __future__ import annotations

from types import SimpleNamespace

from raven.agent.tools.mcp import _collect_tools


def _page(tool_names: list[str], next_cursor: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        tools=[SimpleNamespace(name=n) for n in tool_names],
        nextCursor=next_cursor,
    )


class _FakeSession:
    """Replays a scripted list_tools response keyed by the cursor argument."""

    def __init__(self, pages: dict):
        self._pages = pages
        self.calls: list = []

    async def list_tools(self, cursor: str | None = None) -> SimpleNamespace:
        self.calls.append(cursor)
        return self._pages[cursor]


async def test_single_page_returns_tools_without_follow_up() -> None:
    session = _FakeSession({None: _page(["a", "b"])})

    tools = await _collect_tools(session)

    assert [t.name for t in tools] == ["a", "b"]
    assert session.calls == [None]


async def test_multi_page_follows_cursor_chain() -> None:
    session = _FakeSession(
        {
            None: _page(["a"], "c1"),
            "c1": _page(["b"], "c2"),
            "c2": _page(["c"]),
        }
    )

    tools = await _collect_tools(session)

    assert [t.name for t in tools] == ["a", "b", "c"]
    assert session.calls == [None, "c1", "c2"]


async def test_repeated_cursor_stops_instead_of_looping() -> None:
    session = _FakeSession(
        {
            None: _page(["a"], "c1"),
            "c1": _page(["b"], "c1"),
        }
    )

    tools = await _collect_tools(session)

    assert [t.name for t in tools] == ["a", "b"]
    assert session.calls == [None, "c1"]
