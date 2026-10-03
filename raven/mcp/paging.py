"""One place for walking a paginated MCP list verb.

The pinned Python SDK sends exactly one request per ``ClientSession.list_*``
call and leaves the walking to the caller -- the TypeScript SDK aggregates all
four list verbs itself, the Python one does not. A server that pages a list
would otherwise hand the model its first page with nothing saying the list is
incomplete, the silent truncation of #301 and #855. Every list verb therefore
walks its pages through here, stopping on an absent or a repeated cursor, the
rule #825 introduced for ``tools/list``.

The public surface is one typed walker per verb, so the items a caller loops
over carry their real SDK type rather than the ``Any`` a session yields at
these call sites; the page shape (which attribute carries the items, where the
cursor lives) stays behind the private engine.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp import ClientSession, types


async def _walk(list_page: Callable[..., Awaitable[Any]], attr: str) -> list[Any]:
    """Every item a list verb serves, read page by page.

    The cursor travels through ``params=PaginatedRequestParams`` because the
    ``cursor=`` overload is deprecated in the pinned mcp 1.30.0 and the ty gate
    fails on the warning. The caller keeps its timeout around the whole walk,
    so a server that never stops paging fails visibly instead of stalling the
    turn.
    """
    from mcp import types

    items: list[Any] = []
    cursor = None
    seen: set[str] = set()
    while True:
        page = await list_page(params=types.PaginatedRequestParams(cursor=cursor))
        items.extend(getattr(page, attr))
        cursor = page.nextCursor
        # A server that keeps returning the same cursor would page forever.
        if not cursor or cursor in seen:
            return items
        seen.add(cursor)


async def walk_tools(session: ClientSession) -> list[types.Tool]:
    return await _walk(session.list_tools, "tools")


async def walk_resources(session: ClientSession) -> list[types.Resource]:
    return await _walk(session.list_resources, "resources")


async def walk_resource_templates(session: ClientSession) -> list[types.ResourceTemplate]:
    return await _walk(session.list_resource_templates, "resourceTemplates")


async def walk_prompts(session: ClientSession) -> list[types.Prompt]:
    return await _walk(session.list_prompts, "prompts")
