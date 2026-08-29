"""The three global tools through which the model reaches MCP resources.

Global, with a ``server`` argument, rather than a wrapper pair per server. That
is what Claude Code, Codex and opencode all do, and the reason is schema cost:
per-server wrappers grow the tool array with the number of servers, which pushes
a deploy over the tool-folding threshold and costs the prompt-cache prefix. Three
tools cost the same whether one server serves resources or ten.

They are registered only when some connected server declared the ``resources``
capability, and every call re-checks the server it names. The gate is the
registration; the re-check covers the window between two config applies, in which
a server can go away or come back with different capabilities.

Resources are *data*, not instructions -- the model asks for them and gets file
contents back. They land in a tool result, so they inherit the same fencing every
other tool result gets, with no special handling here.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.agent.tools.base import Tool, ToolResult
from raven.utils.images import image_block, text_block

if TYPE_CHECKING:
    from raven.mcp.manager import MCPConnectionManager

LIST_RESOURCES_NAME = "list_mcp_resources"
LIST_RESOURCE_TEMPLATES_NAME = "list_mcp_resource_templates"
READ_RESOURCE_NAME = "read_mcp_resource"

RESOURCE_TOOL_NAMES = frozenset({LIST_RESOURCES_NAME, LIST_RESOURCE_TEMPLATES_NAME, READ_RESOURCE_NAME})

# A blob larger than this is not worth the tokens it would cost to carry, even
# base64-decoded and written to disk. Matches what opencode enforces.
MAX_BLOB_BYTES = 10 * 1024 * 1024

# Binary a provider can actually render, as opposed to binary we can only
# describe. A whitelist rather than a "starts with image/" test: a provider that
# rejects an unusual image type fails the whole request, not just this block.
RENDERABLE_MIME = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})

_CALL_TIMEOUT = 30.0


class _ResourceTool(Tool):
    """Shared plumbing: reach a server by name, and refuse cleanly when it is gone."""

    def __init__(self, manager: "MCPConnectionManager") -> None:
        self._manager = manager

    def _session_or_refusal(self, server: Any) -> tuple[Any, str | None]:
        """The named server's session, or the sentence to return instead.

        The execution-time half of the capability gate. Registration decided that
        *some* server serves resources; this decides whether *this* one does, and
        still does, which the registration cannot know: config is applied between
        turns and a server can be removed, disabled, or reconnected with
        different capabilities in between.

        The refusal names what is available, because the model chose this server
        from a list and a bare "no" leaves it guessing whether it mistyped.
        """
        offering = self._manager.servers_offering("resources")
        if not isinstance(server, str) or not server:
            return None, f"Error: 'server' must name one of: {', '.join(offering) or '(none)'}."
        if server not in offering:
            return None, (
                f"Error: MCP server '{server}' does not serve resources right now. "
                f"Serving resources: {', '.join(offering) or '(none)'}."
            )
        session = self._manager.session_of(server)
        if session is None:
            # Offered but unreachable: the two answers come from the same record,
            # so this is a narrow race rather than an ordinary state.
            return None, f"Error: MCP server '{server}' is not connected right now."
        return session, None

    def _server_property(self, *, required: bool) -> dict[str, Any]:
        offering = self._manager.servers_offering("resources")
        listed = ", ".join(offering) if offering else "none currently"
        described = f"MCP server name. Serving resources right now: {listed}."
        if not required:
            described += " Omit to cover every one of them."
        return {"type": "string", "description": described}


class ListMcpResourcesTool(_ResourceTool):
    """Enumerate concrete resources, optionally across every server at once."""

    @property
    def name(self) -> str:
        return LIST_RESOURCES_NAME

    @property
    def description(self) -> str:
        return (
            "List the resources an MCP server exposes -- files, records and other "
            "readable content, each identified by a URI. Omit 'server' to list "
            "across every server that serves resources. Read one with "
            f"{READ_RESOURCE_NAME}. Parameterised resources are listed separately "
            f"by {LIST_RESOURCE_TEMPLATES_NAME}."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"server": self._server_property(required=False)},
            "required": [],
        }

    async def execute(self, server: str | None = None, **_: Any) -> str:
        targets = [server] if server else self._manager.servers_offering("resources")
        if not targets:
            return "Error: no MCP server is serving resources right now."

        rows: list[dict[str, Any]] = []
        errors: list[str] = []
        for target in targets:
            session, refusal = self._session_or_refusal(target)
            if refusal is not None:
                errors.append(refusal)
                continue
            try:
                result = await _with_timeout(session.list_resources())
            except Exception as exc:  # noqa: BLE001 -- one bad server must not hide the others
                logger.warning("MCP resources: list on '{}' failed: {}", target, exc)
                errors.append(f"Error: listing '{target}' failed: {type(exc).__name__}.")
                continue
            for res in result.resources:
                rows.append(
                    {
                        # Carried on every row, not just when several servers were
                        # asked: read_mcp_resource needs it, and a row that cannot
                        # be acted on is worse than no row.
                        "server": target,
                        "uri": str(res.uri),
                        "name": res.name,
                        "description": res.description,
                        "mimeType": res.mimeType,
                    }
                )
        return _rendered(rows, errors, empty="No resources on the server(s) asked.")


class ListMcpResourceTemplatesTool(_ResourceTool):
    """Enumerate parameterised resources, kept apart from concrete ones."""

    @property
    def name(self) -> str:
        return LIST_RESOURCE_TEMPLATES_NAME

    @property
    def description(self) -> str:
        return (
            "List an MCP server's resource *templates* -- parameterised URIs that "
            "take arguments, as opposed to the concrete resources listed by "
            f"{LIST_RESOURCES_NAME}. Fill a template's placeholders yourself and "
            f"pass the result to {READ_RESOURCE_NAME}. Omit 'server' to list "
            "across every server that serves resources."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"server": self._server_property(required=False)},
            "required": [],
        }

    async def execute(self, server: str | None = None, **_: Any) -> str:
        targets = [server] if server else self._manager.servers_offering("resources")
        if not targets:
            return "Error: no MCP server is serving resources right now."

        rows: list[dict[str, Any]] = []
        errors: list[str] = []
        for target in targets:
            session, refusal = self._session_or_refusal(target)
            if refusal is not None:
                errors.append(refusal)
                continue
            try:
                result = await _with_timeout(session.list_resource_templates())
            except Exception as exc:  # noqa: BLE001 -- see ListMcpResourcesTool
                logger.warning("MCP resources: template list on '{}' failed: {}", target, exc)
                errors.append(f"Error: listing templates on '{target}' failed: {type(exc).__name__}.")
                continue
            for tpl in result.resourceTemplates:
                rows.append(
                    {
                        "server": target,
                        "uriTemplate": tpl.uriTemplate,
                        "name": tpl.name,
                        "description": tpl.description,
                        "mimeType": tpl.mimeType,
                    }
                )
        return _rendered(rows, errors, empty="No resource templates on the server(s) asked.")


class ReadMcpResourceTool(_ResourceTool):
    """Fetch one resource's contents by URI."""

    def __init__(self, manager: "MCPConnectionManager", *, workspace: Any = None) -> None:
        super().__init__(manager)
        self._workspace = workspace

    @property
    def name(self) -> str:
        return READ_RESOURCE_NAME

    @property
    def description(self) -> str:
        return (
            "Read one MCP resource by URI. Take 'server' and 'uri' from a "
            f"{LIST_RESOURCES_NAME} row, or fill in a template from "
            f"{LIST_RESOURCE_TEMPLATES_NAME}. Text comes back inline; binary is "
            "written to a file and its path is returned."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "server": self._server_property(required=True),
                "uri": {
                    "type": "string",
                    "description": f"Resource URI, exactly as {LIST_RESOURCES_NAME} reported it.",
                },
            },
            "required": ["server", "uri"],
        }

    async def execute(self, server: str, uri: str, **_: Any) -> str | ToolResult:
        session, refusal = self._session_or_refusal(server)
        if refusal is not None:
            return refusal
        if not isinstance(uri, str) or not uri:
            return f"Error: 'uri' is required. Use {LIST_RESOURCES_NAME} to find one."
        try:
            result = await _with_timeout(session.read_resource(uri))
        except Exception as exc:  # noqa: BLE001 -- a server-side refusal is an answer, not a crash
            logger.warning("MCP resources: read of '{}' on '{}' failed: {}", uri, server, exc)
            return f"Error: reading '{uri}' from '{server}' failed: {type(exc).__name__}: {exc}"
        return _render_contents(server, uri, result.contents, workspace=self._workspace)


# ── rendering ──────────────────────────────────────────────────────


async def _with_timeout(coro):
    import asyncio

    return await asyncio.wait_for(coro, timeout=_CALL_TIMEOUT)


def _rendered(rows: list[dict[str, Any]], errors: list[str], *, empty: str) -> str:
    """JSON rows plus any per-server errors, so a partial answer is still usable.

    Errors are appended rather than raised: asking every server at once is the
    default, and one unreachable server must not cost the model the rows the
    others returned.
    """
    if not rows:
        return "\n".join(errors) if errors else empty
    body = json.dumps(rows, ensure_ascii=False, indent=2)
    return body if not errors else body + "\n\n" + "\n".join(errors)


def _render_contents(server: str, uri: str, contents: list[Any], *, workspace: Any) -> str | ToolResult:
    """One ``read_resource`` result as model text, plus blocks a provider can show.

    Text arrives inline. Binary does not: base64 in a tool result costs tokens
    for something the model cannot read, so it is decoded to a file and only the
    path comes back -- except for the image types a provider can actually render,
    which additionally ride along as blocks.
    """
    from mcp import types

    texts: list[str] = []
    blocks: list[Any] = []
    for item in contents:
        if isinstance(item, types.TextResourceContents):
            texts.append(item.text)
            continue
        if isinstance(item, types.BlobResourceContents):
            texts.append(_render_blob(server, uri, item, blocks, workspace=workspace))
            continue
        # An SDK that grew a third content type: say so rather than dropping it
        # silently, which is how a resource comes back empty for no visible
        # reason.
        texts.append(f"[unrenderable resource content: {type(item).__name__}]")

    text = "\n\n".join(t for t in texts if t) or "(the resource returned no content)"
    if not blocks:
        return text
    # Blocks only when something in them is an image. The text of a text-only
    # resource must keep travelling as a plain string: every non-multimodal
    # consumer of a tool result reads the text, and a blocks list that carries
    # nothing a provider can render buys nothing and costs a shape change.
    # ``blocks_from_mcp_content`` holds the same line for tool results.
    return ToolResult(model_text=text, blocks=[text_block(text), *blocks])


def _render_blob(server: str, uri: str, item: Any, blocks: list[Any], *, workspace: Any) -> str:
    import base64

    try:
        raw = base64.b64decode(item.blob, validate=True)
    except Exception:  # noqa: BLE001 -- a malformed payload is the server's fault, not a crash here
        return f"[resource {uri} sent a payload that is not valid base64]"

    if len(raw) > MAX_BLOB_BYTES:
        return (
            f"[resource {uri} is {len(raw)} bytes, over the {MAX_BLOB_BYTES}-byte limit; not read. "
            "Ask the server for a smaller representation if it offers one.]"
        )

    mime = item.mimeType or "application/octet-stream"
    path = _write_blob(server, uri, raw, mime, workspace=workspace)
    note = f"[resource {uri} ({mime}, {len(raw)} bytes)"
    note += f" written to {path}]" if path else " could not be written to disk]"

    if mime in RENDERABLE_MIME:
        blocks.append(image_block(_data_uri(raw, mime)))
    return note


def _data_uri(raw: bytes, mime: str) -> str:
    import base64

    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def _write_blob(server: str, uri: str, raw: bytes, mime: str, *, workspace: Any) -> str | None:
    """Land a binary resource under the workspace and return its path.

    Returns None when there is nowhere to write -- a caller built without a
    workspace still gets the description and any renderable block, which beats
    failing the read over a place to put a file.
    """
    import hashlib
    import mimetypes
    from pathlib import Path

    if workspace is None:
        return None
    ext = mimetypes.guess_extension(mime) or ".bin"
    # Named by the URI's digest, not by the URI: a resource URI is free-form and
    # may carry separators, query strings, or characters a filesystem refuses.
    digest = hashlib.sha1(f"{server}\0{uri}".encode()).hexdigest()[:12]
    try:
        out_dir = Path(workspace) / "mcp_resources"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{digest}{ext}"
        path.write_bytes(raw)
        return str(path)
    except OSError as exc:
        logger.warning("MCP resources: could not write '{}' from '{}': {}", uri, server, exc)
        return None


def resource_tools(manager: "MCPConnectionManager", *, workspace: Any = None) -> list[Tool]:
    """The three tools, for a caller that has already decided to register them."""
    return [
        ListMcpResourcesTool(manager),
        ListMcpResourceTemplatesTool(manager),
        ReadMcpResourceTool(manager, workspace=workspace),
    ]


__all__ = [
    "LIST_RESOURCES_NAME",
    "LIST_RESOURCE_TEMPLATES_NAME",
    "MAX_BLOB_BYTES",
    "READ_RESOURCE_NAME",
    "RENDERABLE_MIME",
    "RESOURCE_TOOL_NAMES",
    "ListMcpResourceTemplatesTool",
    "ListMcpResourcesTool",
    "ReadMcpResourceTool",
    "resource_tools",
]
