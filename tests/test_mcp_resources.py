"""The three resource meta-tools: gating, rendering, and how they fail.

Global tools with a ``server`` argument, so they carry two responsibilities a
per-server wrapper would not have: deciding at call time whether the named server
still serves resources, and surviving one unreachable server when asked to cover
all of them.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from mcp import types
from pydantic import AnyUrl

from raven.contracts.tool import ToolResult
from raven.mcp.resources import (
    MAX_BLOB_BYTES,
    ListMcpResourcesTool,
    ListMcpResourceTemplatesTool,
    ReadMcpResourceTool,
    resource_tools,
)

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()


class _Session:
    """One server's session. Any attribute set to an exception is raised instead."""

    def __init__(self, *, resources=(), templates=(), contents=(), fail: Exception | None = None) -> None:
        self._resources = list(resources)
        self._templates = list(templates)
        self._contents = list(contents)
        self._fail = fail

    async def list_resources(self, params=None):
        if self._fail:
            raise self._fail
        return types.ListResourcesResult(resources=self._resources)

    async def list_resource_templates(self, params=None):
        if self._fail:
            raise self._fail
        return types.ListResourceTemplatesResult(resourceTemplates=self._templates)

    async def read_resource(self, uri):
        if self._fail:
            raise self._fail
        return types.ReadResourceResult(contents=self._contents)


class _PaginatedResourcesSession(_Session):
    """Session that simulates paginated list_resources/list_resource_templates responses."""

    def __init__(
        self,
        *,
        resource_pages: list[list[types.Resource]] | None = None,
        template_pages: list[list[types.ResourceTemplate]] | None = None,
        fail: Exception | None = None,
    ) -> None:
        self._resource_pages = resource_pages or [[]]
        self._template_pages = template_pages or [[]]
        self._fail = fail
        self._resource_page_index = 0
        self._template_page_index = 0

    async def list_resources(self, params=None):
        if self._fail:
            raise self._fail
        cursor = getattr(params, "cursor", None) if params else None
        if cursor is not None:
            try:
                self._resource_page_index = int(cursor)
            except ValueError:
                self._resource_page_index = 0

        if self._resource_page_index >= len(self._resource_pages):
            return types.ListResourcesResult(resources=[], nextCursor=None)

        current_page = self._resource_pages[self._resource_page_index]
        next_cursor = (
            str(self._resource_page_index + 1) if self._resource_page_index + 1 < len(self._resource_pages) else None
        )
        self._resource_page_index += 1
        return types.ListResourcesResult(resources=current_page, nextCursor=next_cursor)

    async def list_resource_templates(self, params=None):
        if self._fail:
            raise self._fail
        cursor = getattr(params, "cursor", None) if params else None
        if cursor is not None:
            try:
                self._template_page_index = int(cursor)
            except ValueError:
                self._template_page_index = 0

        if self._template_page_index >= len(self._template_pages):
            return types.ListResourceTemplatesResult(resourceTemplates=[], nextCursor=None)

        current_page = self._template_pages[self._template_page_index]
        next_cursor = (
            str(self._template_page_index + 1) if self._template_page_index + 1 < len(self._template_pages) else None
        )
        self._template_page_index += 1
        return types.ListResourceTemplatesResult(resourceTemplates=current_page, nextCursor=next_cursor)


class _Manager:
    """Stands in for the connection manager's two addressing methods."""

    def __init__(self, sessions: dict[str, _Session], *, offering: list[str] | None = None) -> None:
        self._sessions = sessions
        self._offering = sorted(sessions) if offering is None else offering

    def servers_offering(self, primitive: str) -> list[str]:
        return list(self._offering) if primitive == "resources" else []

    def session_of(self, server: str):
        return self._sessions.get(server)


def _res(uri: str, name: str, mime: str = "text/plain") -> types.Resource:
    return types.Resource(uri=AnyUrl(uri), name=name, mimeType=mime, description=f"about {name}")


class TestListing:
    async def test_rows_carry_the_server_that_owns_them(self):
        # read_mcp_resource needs it, so a row without it cannot be acted on.
        mgr = _Manager({"a": _Session(resources=[_res("file:///x", "x")])})
        rows = json.loads(await ListMcpResourcesTool(mgr).execute())
        assert rows[0]["server"] == "a"
        assert rows[0]["uri"] == "file:///x"

    async def test_omitting_the_server_covers_every_one_that_offers(self):
        mgr = _Manager(
            {
                "a": _Session(resources=[_res("file:///a", "a")]),
                "b": _Session(resources=[_res("file:///b", "b")]),
            }
        )
        rows = json.loads(await ListMcpResourcesTool(mgr).execute())
        assert sorted(r["server"] for r in rows) == ["a", "b"]

    async def test_one_broken_server_does_not_hide_the_others(self):
        mgr = _Manager(
            {
                "good": _Session(resources=[_res("file:///g", "g")]),
                "bad": _Session(fail=RuntimeError("transport gone")),
            }
        )
        out = await ListMcpResourcesTool(mgr).execute()
        body, _, errors = out.partition("\n\n")
        assert json.loads(body)[0]["server"] == "good"
        assert "bad" in errors

    async def test_templates_are_listed_apart_from_concrete_resources(self):
        tpl = types.ResourceTemplate(uriTemplate="file:///logs/{date}", name="logs")
        mgr = _Manager({"a": _Session(resources=[_res("file:///x", "x")], templates=[tpl])})
        concrete = json.loads(await ListMcpResourcesTool(mgr).execute())
        templates = json.loads(await ListMcpResourceTemplatesTool(mgr).execute())
        assert "uriTemplate" not in concrete[0]
        assert templates[0]["uriTemplate"] == "file:///logs/{date}"

    async def test_an_empty_server_says_so_rather_than_returning_nothing(self):
        mgr = _Manager({"a": _Session()})
        assert "No resources" in await ListMcpResourcesTool(mgr).execute()


class TestTheExecutionTimeGate:
    async def test_a_server_that_stopped_offering_is_refused_by_name(self):
        # Registration only established that *some* server serves resources; a
        # config apply between turns can remove this one.
        mgr = _Manager({"a": _Session()}, offering=[])
        out = await ReadMcpResourceTool(mgr).execute(server="a", uri="file:///x")
        assert "does not serve resources" in out

    async def test_the_refusal_names_what_is_available(self):
        mgr = _Manager({"a": _Session(), "b": _Session()})
        out = await ReadMcpResourceTool(mgr).execute(server="ghost", uri="file:///x")
        assert "a, b" in out

    async def test_offered_but_unreachable_is_its_own_answer(self):
        # Narrow race: both answers come from the same record.
        mgr = _Manager({}, offering=["a"])
        out = await ReadMcpResourceTool(mgr).execute(server="a", uri="file:///x")
        assert "not connected" in out

    async def test_listing_with_nothing_offering_says_so(self):
        mgr = _Manager({}, offering=[])
        assert "no MCP server is serving resources" in await ListMcpResourcesTool(mgr).execute()

    async def test_a_missing_uri_is_refused_before_the_call(self):
        mgr = _Manager({"a": _Session()})
        assert "'uri' is required" in await ReadMcpResourceTool(mgr).execute(server="a", uri="")


class TestReading:
    async def test_text_comes_back_inline(self):
        contents = [types.TextResourceContents(uri=AnyUrl("file:///x"), mimeType="text/plain", text="hello")]
        mgr = _Manager({"a": _Session(contents=contents)})
        out = await ReadMcpResourceTool(mgr).execute(server="a", uri="file:///x")
        assert out == "hello"
        assert not isinstance(out, ToolResult), "text-only must stay a plain string"

    async def test_a_renderable_image_rides_along_as_a_block(self):
        contents = [types.BlobResourceContents(uri=AnyUrl("file:///p.png"), mimeType="image/png", blob=PNG)]
        mgr = _Manager({"a": _Session(contents=contents)})
        out = await ReadMcpResourceTool(mgr, workspace=None).execute(server="a", uri="file:///p.png")
        assert isinstance(out, ToolResult)
        assert "image/png" in out.model_text

    async def test_binary_is_written_out_and_only_its_path_returned(self, tmp_path: Path):
        raw = b"\x00\x01\x02not text"
        contents = [
            types.BlobResourceContents(
                uri=AnyUrl("file:///blob.bin"),
                mimeType="application/octet-stream",
                blob=base64.b64encode(raw).decode(),
            )
        ]
        mgr = _Manager({"a": _Session(contents=contents)})
        out = await ReadMcpResourceTool(mgr, workspace=tmp_path).execute(server="a", uri="file:///blob.bin")

        written = list((tmp_path / "mcp_resources").iterdir())
        assert len(written) == 1
        assert written[0].read_bytes() == raw
        assert str(written[0]) in out
        # The payload itself must not be in the text: base64 in a tool result
        # costs tokens for something the model cannot read.
        assert base64.b64encode(raw).decode() not in out

    async def test_an_oversized_blob_is_described_not_read(self):
        raw = b"x" * (MAX_BLOB_BYTES + 1)
        contents = [
            types.BlobResourceContents(
                uri=AnyUrl("file:///big.bin"),
                mimeType="application/octet-stream",
                blob=base64.b64encode(raw).decode(),
            )
        ]
        mgr = _Manager({"a": _Session(contents=contents)})
        out = await ReadMcpResourceTool(mgr).execute(server="a", uri="file:///big.bin")
        assert "over the" in out and "not read" in out

    async def test_no_workspace_still_answers(self):
        # A caller built without a workspace gets the description rather than a
        # failed read over a place to put a file.
        contents = [
            types.BlobResourceContents(
                uri=AnyUrl("file:///b.bin"), mimeType="application/octet-stream", blob=base64.b64encode(b"z").decode()
            )
        ]
        mgr = _Manager({"a": _Session(contents=contents)})
        out = await ReadMcpResourceTool(mgr, workspace=None).execute(server="a", uri="file:///b.bin")
        assert "could not be written" in out

    async def test_an_empty_result_says_so(self):
        mgr = _Manager({"a": _Session(contents=[])})
        out = await ReadMcpResourceTool(mgr).execute(server="a", uri="file:///x")
        assert "no content" in out

    async def test_a_server_side_failure_is_an_answer_not_a_crash(self):
        mgr = _Manager({"a": _Session(fail=PermissionError("denied"))})
        out = await ReadMcpResourceTool(mgr).execute(server="a", uri="file:///x")
        assert "failed" in out and "PermissionError" in out


class TestTheSchema:
    def test_the_server_argument_lists_who_offers(self):
        # The model picks a server from this description, so it has to name them.
        mgr = _Manager({"openseo": _Session(), "gh": _Session()})
        described = ListMcpResourcesTool(mgr).parameters["properties"]["server"]["description"]
        assert "gh, openseo" in described

    def test_only_read_requires_its_arguments(self):
        mgr = _Manager({"a": _Session()})
        by_name = {t.name: t for t in resource_tools(mgr)}
        assert by_name["list_mcp_resources"].parameters["required"] == []
        assert by_name["read_mcp_resource"].parameters["required"] == ["server", "uri"]

    def test_each_tool_points_at_the_next_step(self):
        mgr = _Manager({"a": _Session()})
        by_name = {t.name: t for t in resource_tools(mgr)}
        assert "read_mcp_resource" in by_name["list_mcp_resources"].description
        assert "list_mcp_resource_templates" in by_name["list_mcp_resources"].description
        assert "list_mcp_resources" in by_name["read_mcp_resource"].description


class TestPagination:
    """Tests for paginated list_resources and list_resource_templates handling."""

    def _resource_page(self, count: int, start: int = 0) -> list[types.Resource]:
        """Generate a page of resources."""
        return [
            types.Resource(
                uri=AnyUrl(f"file:///resource_{i}"),
                name=f"resource_{i}",
                mimeType="text/plain",
                description=f"Resource {i}",
            )
            for i in range(start, start + count)
        ]

    def _template_page(self, count: int, start: int = 0) -> list[types.ResourceTemplate]:
        """Generate a page of resource templates."""
        return [
            types.ResourceTemplate(
                uriTemplate=f"file:///template_{i}/{{param}}",
                name=f"template_{i}",
                mimeType="text/plain",
                description=f"Template {i}",
            )
            for i in range(start, start + count)
        ]

    async def test_single_page_resources_returns_all(self):
        """A single page returns all resources without pagination."""
        page = self._resource_page(5)
        mgr = _Manager({"a": _PaginatedResourcesSession(resource_pages=[page])})
        rows = json.loads(await ListMcpResourcesTool(mgr).execute())
        assert len(rows) == 5
        assert rows[0]["uri"] == "file:///resource_0"
        assert rows[4]["uri"] == "file:///resource_4"

    async def test_multiple_pages_resources_are_aggregated(self):
        """Multiple resource pages are aggregated into a single result."""
        page1 = self._resource_page(3, 0)
        page2 = self._resource_page(3, 3)
        page3 = self._resource_page(2, 6)

        mgr = _Manager({"a": _PaginatedResourcesSession(resource_pages=[page1, page2, page3])})
        rows = json.loads(await ListMcpResourcesTool(mgr).execute())

        assert len(rows) == 8, f"Expected 8 resources, got {len(rows)}"
        assert rows[0]["uri"] == "file:///resource_0"
        assert rows[7]["uri"] == "file:///resource_7"

    async def test_single_page_templates_returns_all(self):
        """A single page returns all templates without pagination."""
        page = self._template_page(4)
        mgr = _Manager({"a": _PaginatedResourcesSession(template_pages=[page])})
        rows = json.loads(await ListMcpResourceTemplatesTool(mgr).execute())
        assert len(rows) == 4
        assert rows[0]["uriTemplate"] == "file:///template_0/{param}"
        assert rows[3]["uriTemplate"] == "file:///template_3/{param}"

    async def test_multiple_pages_templates_are_aggregated(self):
        """Multiple template pages are aggregated into a single result."""
        page1 = self._template_page(2, 0)
        page2 = self._template_page(3, 2)

        mgr = _Manager({"a": _PaginatedResourcesSession(template_pages=[page1, page2])})
        rows = json.loads(await ListMcpResourceTemplatesTool(mgr).execute())

        assert len(rows) == 5, f"Expected 5 templates, got {len(rows)}"
        assert rows[0]["uriTemplate"] == "file:///template_0/{param}"
        assert rows[4]["uriTemplate"] == "file:///template_4/{param}"

    async def test_empty_page_ends_pagination(self):
        """An empty page ends the pagination walk."""
        page1 = self._resource_page(2)
        page2 = []  # Empty page

        mgr = _Manager({"a": _PaginatedResourcesSession(resource_pages=[page1, page2])})
        rows = json.loads(await ListMcpResourcesTool(mgr).execute())

        assert len(rows) == 2, f"Expected 2 resources, got {len(rows)}"

    async def test_repeated_cursor_ends_pagination(self):
        """A repeated cursor ends the pagination walk (server bug guard)."""

        # Custom session that repeats the cursor
        class RepeatingCursorSession(_PaginatedResourcesSession):
            async def list_resources(self, params=None):
                if self._fail:
                    raise self._fail
                cursor = getattr(params, "cursor", None) if params else None
                if cursor is not None:
                    try:
                        self._resource_page_index = int(cursor)
                    except ValueError:
                        self._resource_page_index = 0

                if self._resource_page_index >= len(self._resource_pages):
                    return types.ListResourcesResult(resources=[], nextCursor=None)

                current_page = self._resource_pages[self._resource_page_index]
                # Bug: always return cursor "0" instead of incrementing
                next_cursor = "0" if self._resource_page_index == 0 else None
                self._resource_page_index += 1
                return types.ListResourcesResult(resources=current_page, nextCursor=next_cursor)

        page1 = self._resource_page(2)
        page2 = self._resource_page(2, 2)

        mgr = _Manager({"a": RepeatingCursorSession(resource_pages=[page1, page2])})
        rows = json.loads(await ListMcpResourcesTool(mgr).execute())

        # Should only get first page because cursor "0" repeats
        assert len(rows) == 2, f"Expected 2 resources (first page only), got {len(rows)}"
