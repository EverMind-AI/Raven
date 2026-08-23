"""The two global tools through which the model reaches MCP prompts.

Global with a ``server`` argument, for the same reason as the resource tools:
two schemas whatever the server count, so growing the server set never pushes a
deploy over the tool-folding threshold.

A deliberate departure from the MCP spec, recorded because it is a real cost.
The spec positions prompts as *user*-selected templates, and recommends a slash
command as the carrier. Reaching them through tools makes them
model-selected instead. What that buys is zero change to the interaction layer:
the slash route needs a command registry that accepts outside contributions,
and ``commands.catalog`` is a pure reflection of the Typer app, while
``slash.exec`` returns display text that cannot become this turn's user input.

Because the product lands in a tool result rather than as user input, the
fencing every tool result gets applies unchanged -- and unlike the slash route,
fencing does not defeat the purpose here. On the slash route the expanded prompt
*is* the turn's instruction, so declaring it data would waste it; that is why
that route needs a per-server trust level and this one does not.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.agent.tools.base import Tool

if TYPE_CHECKING:
    from raven.mcp.manager import MCPConnectionManager

LIST_PROMPTS_NAME = "list_mcp_prompts"
GET_PROMPT_NAME = "get_mcp_prompt"

PROMPT_TOOL_NAMES = frozenset({LIST_PROMPTS_NAME, GET_PROMPT_NAME})

_CALL_TIMEOUT = 30.0


class _PromptTool(Tool):
    """Shared plumbing: reach a server by name, and refuse cleanly when it is gone."""

    def __init__(self, manager: "MCPConnectionManager") -> None:
        self._manager = manager

    def _session_or_refusal(self, server: Any) -> tuple[Any, str | None]:
        """The named server's session, or the sentence to return instead.

        The execution-time half of the capability gate: registration established
        that *some* server serves prompts, and this establishes that *this* one
        still does. Config is applied between turns, so a server can be removed
        or come back with different capabilities after the tool was registered.
        """
        offering = self._manager.servers_offering("prompts")
        if not isinstance(server, str) or not server:
            return None, f"Error: 'server' must name one of: {', '.join(offering) or '(none)'}."
        if server not in offering:
            return None, (
                f"Error: MCP server '{server}' does not serve prompts right now. "
                f"Serving prompts: {', '.join(offering) or '(none)'}."
            )
        session = self._manager.session_of(server)
        if session is None:
            return None, f"Error: MCP server '{server}' is not connected right now."
        return session, None

    def _server_property(self, *, required: bool) -> dict[str, Any]:
        offering = self._manager.servers_offering("prompts")
        listed = ", ".join(offering) if offering else "none currently"
        described = f"MCP server name. Serving prompts right now: {listed}."
        if not required:
            described += " Omit to cover every one of them."
        return {"type": "string", "description": described}


class ListMcpPromptsTool(_PromptTool):
    """Enumerate prompt templates and the arguments each one takes."""

    @property
    def name(self) -> str:
        return LIST_PROMPTS_NAME

    @property
    def description(self) -> str:
        return (
            "List the prompt templates an MCP server offers -- prepared "
            "instructions for tasks that server knows how to frame, each with the "
            f"arguments it takes. Fetch one with {GET_PROMPT_NAME}. Omit 'server' "
            "to list across every server that serves prompts."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"server": self._server_property(required=False)},
            "required": [],
        }

    async def execute(self, server: str | None = None, **_: Any) -> str:
        targets = [server] if server else self._manager.servers_offering("prompts")
        if not targets:
            return "Error: no MCP server is serving prompts right now."

        rows: list[dict[str, Any]] = []
        errors: list[str] = []
        for target in targets:
            session, refusal = self._session_or_refusal(target)
            if refusal is not None:
                errors.append(refusal)
                continue
            try:
                result = await _with_timeout(session.list_prompts())
            except Exception as exc:  # noqa: BLE001 -- one bad server must not hide the others
                logger.warning("MCP prompts: list on '{}' failed: {}", target, exc)
                errors.append(f"Error: listing prompts on '{target}' failed: {type(exc).__name__}.")
                continue
            for prompt in result.prompts:
                rows.append(
                    {
                        # On every row, because get_mcp_prompt needs it: a row the
                        # model cannot act on is worse than no row.
                        "server": target,
                        "name": prompt.name,
                        "description": prompt.description,
                        "arguments": [
                            {
                                "name": arg.name,
                                "description": arg.description,
                                "required": bool(arg.required),
                            }
                            for arg in (prompt.arguments or [])
                        ],
                    }
                )
        if not rows:
            return "\n".join(errors) if errors else "No prompts on the server(s) asked."
        body = json.dumps(rows, ensure_ascii=False, indent=2)
        return body if not errors else body + "\n\n" + "\n".join(errors)


class GetMcpPromptTool(_PromptTool):
    """Expand one prompt template with arguments."""

    @property
    def name(self) -> str:
        return GET_PROMPT_NAME

    @property
    def description(self) -> str:
        return (
            "Fetch one MCP prompt template, expanded with the arguments you "
            f"supply. Take 'server' and 'name' from a {LIST_PROMPTS_NAME} row, and "
            "pass that row's arguments. The result is the template's text for you "
            "to read and act on, not an instruction that has already been applied."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "server": self._server_property(required=True),
                "name": {
                    "type": "string",
                    "description": f"Prompt name, exactly as {LIST_PROMPTS_NAME} reported it.",
                },
                "arguments": {
                    "type": "object",
                    "description": (
                        "Values for the prompt's arguments, keyed by argument name. "
                        f"{LIST_PROMPTS_NAME} reports which are required."
                    ),
                },
            },
            "required": ["server", "name"],
        }

    async def execute(self, server: str, name: str, arguments: dict[str, Any] | None = None, **_: Any) -> str:
        session, refusal = self._session_or_refusal(server)
        if refusal is not None:
            return refusal
        if not isinstance(name, str) or not name:
            return f"Error: 'name' is required. Use {LIST_PROMPTS_NAME} to find one."
        if isinstance(arguments, str):
            # Models sometimes emit a nested object as a JSON string; parse it so
            # the call still goes through rather than failing on a shape nit.
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                return "Error: 'arguments' must be a JSON object."
        if arguments is not None and not isinstance(arguments, dict):
            return "Error: 'arguments' must be a JSON object keyed by argument name."

        # The SDK wants strings; a model that passes a number or a bool means the
        # obvious thing, and refusing it would cost a round-trip over a cast.
        cast = {
            str(k): v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) for k, v in (arguments or {}).items()
        }
        try:
            result = await _with_timeout(session.get_prompt(name, arguments=cast or None))
        except Exception as exc:  # noqa: BLE001 -- a server-side refusal is an answer, not a crash
            logger.warning("MCP prompts: get '{}' on '{}' failed: {}", name, server, exc)
            return f"Error: fetching prompt '{name}' from '{server}' failed: {type(exc).__name__}: {exc}"
        return _render_prompt(name, result)


async def _with_timeout(coro):
    import asyncio

    return await asyncio.wait_for(coro, timeout=_CALL_TIMEOUT)


def _render_prompt(name: str, result: Any) -> str:
    """One ``prompts/get`` result as model text, roles kept and labelled.

    An assistant-role message is kept, not dropped. ``prompts/get`` returns them
    for few-shot framing, and a template that demonstrates the answer shape loses
    its point without them. Keeping them is safe *because* this arrives as a tool
    result: it is text the model reads, clearly labelled, not a message replayed
    into the conversation as something the model itself said. The slash route
    cannot do this -- there the expansion becomes the turn's own messages, so an
    assistant turn fabricated by an outside server would be indistinguishable
    from the model's own history.
    """
    parts: list[str] = []
    if getattr(result, "description", None):
        parts.append(f"# {result.description}")

    for message in getattr(result, "messages", None) or []:
        role = getattr(message, "role", "user")
        text = _message_text(getattr(message, "content", None))
        parts.append(f"[{role}]\n{text}" if text else f"[{role}] (no renderable content)")

    if not parts:
        return f"(prompt '{name}' expanded to nothing)"
    return "\n\n".join(parts)


def _message_text(content: Any) -> str:
    """The text of one prompt message, without ever str()-ing a payload.

    A pydantic model's repr would dump an entire base64 image into the prompt as
    prose -- the same failure the tool-result renderer exists to avoid -- so
    anything that is not text is described instead.
    """
    from mcp import types

    if content is None:
        return ""
    items = content if isinstance(content, list) else [content]
    out: list[str] = []
    for item in items:
        if isinstance(item, types.TextContent):
            out.append(item.text)
            continue
        label = getattr(item, "type", type(item).__name__)
        mime = getattr(item, "mimeType", None)
        out.append(f"[non-text prompt content: {label}{f' ({mime})' if mime else ''}]")
    return "\n".join(t for t in out if t)


def prompt_tools(manager: "MCPConnectionManager") -> list[Tool]:
    """The two tools, for a caller that has already decided to register them."""
    return [ListMcpPromptsTool(manager), GetMcpPromptTool(manager)]


__all__ = [
    "GET_PROMPT_NAME",
    "LIST_PROMPTS_NAME",
    "PROMPT_TOOL_NAMES",
    "GetMcpPromptTool",
    "ListMcpPromptsTool",
    "prompt_tools",
]
