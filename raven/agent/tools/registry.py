"""Tool registry for dynamic tool management."""

import asyncio
from contextvars import ContextVar
from typing import Any

from raven.agent.tools.base import Tool, ToolOutput, ToolResult
from raven.tracing import semconv, trace


class ToolRegistry:
    """
    Registry for agent tools.

    Allows dynamic registration and execution of tools.
    """

    # Backstop ceiling for tools that don't set their own ``timeout_seconds``.
    # Generous on purpose: it exists to break an infinite hang (a tool with no
    # internal timeout that never returns), not to enforce a tight per-tool SLA.
    DEFAULT_TOOL_TIMEOUT_S = 300.0

    def __init__(self):
        self._tools: dict[str, Tool] = {}
        # Turn-local, not an attribute: one gateway registry serves concurrent
        # turns from different channels, and a plain field would let whichever
        # turn set it last decide what the others are shown.
        self._channel: ContextVar[str | None] = ContextVar("tool_registry_channel", default=None)

    def set_channel(self, channel: str | None) -> None:
        """Record the channel this turn is answering on (turn-local).

        Set alongside the per-tool contexts, before the schema is assembled, so
        ``offers_on_this_channel`` can withhold a channel-bound tool. Left unset
        the registry advertises everything, which is what a surface with a
        single channel (the sub-agent and curator registries) wants.
        """
        self._channel.set(channel)

    def offers_on_this_channel(self, tool: Tool) -> bool:
        """Whether this turn's channel may see ``tool`` at all.

        The one predicate behind both surfaces a tool can be reached through --
        the schema and tool-search -- so a channel-bound tool cannot be hidden
        from one and found through the other.
        """
        if tool.channels is None:
            return True
        channel = self._channel.get()
        return channel is None or channel in tool.channels

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools

    def names(self) -> list[str]:
        """Every registered tool name.

        Used to attribute registrations to the attempt that made them: an MCP
        connect registers through this registry, so diffing before and after is
        the only way to know what a *cancelled* attempt managed to add.
        """
        return list(self._tools)

    def is_blocking(self, name: str, params: dict[str, Any] | None = None) -> bool:
        """Whether this call is a blocking interaction (execute() is not timer-wrapped).

        The tool's own ``blocking_for`` is the single fact source, and it takes the
        call's params because a forwarding tool (``tool_call``) inherits the verdict
        of whatever it forwards to. Exposed so a turn stream can tell a consumer
        that this call has no deadline and may go silent for as long as it runs,
        without the consumer keeping its own list of tool names.
        """
        tool = self._tools.get(name)
        return bool(tool is not None and tool.blocking_for(params or {}))

    def take_metadata(self, name: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Consume the structured payload this call left for the turn stream.

        Takes the call's params for the same reason ``is_blocking`` does: a
        forwarding tool (``tool_call``) owns none of the metadata it returns, so
        the payload must be collected from whatever it forwarded to.
        """
        tool = self._tools.get(name)
        if tool is None:
            return None
        return tool.metadata_owner(params or {}).take_metadata()

    def get_definitions(self) -> list[dict[str, Any]]:
        """Tool definitions in OpenAI format, minus those this channel cannot use."""
        return [tool.to_schema() for tool in self._tools.values() if self.offers_on_this_channel(tool)]

    @trace.instrument("tool.call", extract=semconv.tool_call)
    async def execute(self, name: str, params: dict[str, Any]) -> str:
        """Execute a tool by name with given parameters."""
        _hint = "\n\n[Analyze the error above and try a different approach.]"

        tool = self._tools.get(name)
        if not tool:
            return f"Error: Tool '{name}' not found. Available: {', '.join(self.tool_names)}"

        try:
            # Attempt to cast parameters to match schema types
            params = tool.cast_params(params)

            # Validate parameters
            errors = tool.validate_params(params)
            if errors:
                return f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors) + _hint

            ceiling = tool.timeout_seconds or self.DEFAULT_TOOL_TIMEOUT_S
            if tool.blocking_for(params):
                # Intentionally waits on a human — must not be timer-killed.
                result = await tool.execute(**params)
            else:
                result = await asyncio.wait_for(tool.execute(**params), timeout=ceiling)

            # Unwrap ToolResult here, at the boundary: `execute` promises model
            # text to every caller, and only the agent loop wants the display
            # string and multimodal blocks (which ride along on ToolOutput).
            if isinstance(result, ToolResult):
                model_text, display_text = result.model_text, result.display_text
                retryable, abort_action = result.retryable, result.abort_action
                blocks = result.blocks
            else:
                model_text, display_text = str(result), None
                retryable, abort_action = True, False
                blocks = None

            if model_text.startswith("Error"):
                # ``Error:`` describes presentation, not retry semantics.
                # Policy-aware tools return explicit control metadata so the
                # registry does not accidentally turn a security decision into
                # the generic invitation to find an equivalent implementation.
                #
                # An error also replaces the result, so any blocks it came with
                # are no longer what the model should be looking at.
                suffix = _hint if retryable else ""
                return ToolOutput(
                    model_text + suffix,
                    display_text,
                    retryable=retryable,
                    abort_action=abort_action,
                )
            return ToolOutput(
                model_text,
                display_text,
                retryable=retryable,
                abort_action=abort_action,
                blocks=blocks,
            )
        except asyncio.TimeoutError:
            return f"Error: Tool '{name}' timed out after {ceiling:.0f}s." + _hint
        except Exception as e:
            return f"Error executing {name}: {str(e)}" + _hint

    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
