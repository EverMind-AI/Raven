"""Tool registry for dynamic tool management."""

import asyncio
from typing import Any

from raven.agent.tools.base import Tool
from raven.tracing import semconv, trace

# Appended to every tool-error return path below. Model-visible on every arm,
# including DR.
#
# Module level, not a local in ``execute``, and the reason is a gate rather than
# style: ``tests/test_prompt_detector_disjointness.py`` enforces that no detector
# trigger phrase appears in model-visible prompt text, and it finds that text by
# scanning module-level ``str`` constants (``vars(module)``). A function-local is
# invisible to that scan, so as a local this string sat outside the guard while
# containing "a different approach" - the exact idiom dr@1.9 had to remove from
# ``spin_breaker._RESTART_MARKERS`` because the prompt taught the model to say it.
# Adding the module to the test's list would not have helped while the string was
# a local: the fix has to be the hoist, or the gate is a gate over nothing.
_TOOL_ERROR_HINT = "\n\n[Analyze the error above and try a different approach.]"


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

    def names(self) -> list[str]:
        """Registered tool names, registration-ordered."""
        return list(self._tools)

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get all tool definitions in OpenAI format."""
        return [tool.to_schema() for tool in self._tools.values()]

    @trace.instrument("tool.call", extract=semconv.tool_call)
    async def execute(self, name: str, params: dict[str, Any]) -> str:
        """Execute a tool by name with given parameters."""
        _hint = _TOOL_ERROR_HINT

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
            return f"Error executing {name}: {str(e)}" + _hint

    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
