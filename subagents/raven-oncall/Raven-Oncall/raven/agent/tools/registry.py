"""Tool registry for dynamic tool management."""

import asyncio
from typing import Any

from loguru import logger

from raven.agent.tools.base import Tool
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

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get all tool definitions in OpenAI format."""
        return [tool.to_schema() for tool in self._tools.values()]

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
            if tool.blocking_interaction:
                # Intentionally waits on a human — must not be timer-killed.
                result = await tool.execute(**params)
            else:
                result = await asyncio.wait_for(tool.execute(**params), timeout=ceiling)

            if isinstance(result, str) and result.startswith("Error"):
                return result + _hint
            return result
        except asyncio.TimeoutError:
            return f"Error: Tool '{name}' timed out after {ceiling:.0f}s." + _hint
        except Exception as e:
            # A fault in the tool, not in the call. Two things follow, and neither
            # was happening.
            #
            # It has to reach a person, with a traceback. Nothing here logged, so
            # the only trace of a bug was one line of str(e) in the transcript:
            # measured 2026-08-17, ops_submit raised KeyError('host') and the whole
            # log held no traceback -- the cause was found by reading the source.
            #
            # And the reader must not be told to route around it. The generic hint
            # ("try a different approach") is right for a refusal and wrong here:
            # the same measurement had the arm pass a host by hand, then edit the
            # campaign's own declaration to add the field, tripping the apparatus
            # gate three times -- while the jobs it thought had failed were already
            # running, because the exception was raised after they were staged and
            # written to the ledger. Nothing it could do with arguments or state
            # would have helped, and nothing said so.
            logger.exception("Tool {!r} raised {}", name, type(e).__name__)
            return (
                f"Error executing {name}: {type(e).__name__} {str(e)!r}. This is a fault "
                "inside the tool itself, so no change to your arguments or to any state "
                "will fix it -- do not work around it. Some of this call may already have "
                "taken effect: check before deciding anything. Report it to the owner and "
                "move on to what does not depend on it."
            )

    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
