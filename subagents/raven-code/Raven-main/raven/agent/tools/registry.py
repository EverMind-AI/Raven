"""Tool registry for dynamic tool management."""

import asyncio
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

    def canonical_name(self, name: str) -> str:
        """The registered name a call to ``name`` actually executes.

        Callers that classify by tool name (untrusted-output fencing, the
        test-evidence gate) must use this, not the model's raw spelling —
        ``execute`` repairs mangled names, so classifying by the raw name
        lets a case-mangled call bypass name-keyed policies.
        """
        if name in self._tools:
            return name
        return self._repair_tool_name(name) or name

    @trace.instrument("tool.call", extract=semconv.tool_call)
    async def execute(self, name: str, params: dict[str, Any]) -> str:
        """Execute a tool by name with given parameters."""
        # Generic change-approach hint, appended only where the error text
        # cannot carry its own next step (timeouts, unexpected exceptions).
        # Tool-authored errors and validation errors carry targeted guidance;
        # stacking a generic suffix on top of those is noise.
        _hint = "\n\n[Analyze the error above and try a different approach.]"

        note = ""
        tool = self._tools.get(name)
        if not tool:
            repaired = self._repair_tool_name(name)
            if repaired is None:
                return f"Error: Tool '{name}' not found. Available: {', '.join(self.tool_names)}"
            # Models emit case/format-mangled names (Read_File, execRead).
            # Executing the obvious match costs nothing; failing the call
            # costs a turn.
            tool = self._tools[repaired]
            note = f"[note: tool name {name!r} resolved to {repaired!r}]\n"
            name = repaired

        try:
            # Attempt to cast parameters to match schema types
            params = tool.cast_params(params)

            # Validate parameters
            errors = tool.validate_params(params)
            if errors:
                suggestion = self._suggest_tool_for_params(name, params)
                return note + f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors) + suggestion

            ceiling = tool.timeout_seconds or self.DEFAULT_TOOL_TIMEOUT_S
            if tool.blocking_interaction:
                # Intentionally waits on a human — must not be timer-killed.
                result = await tool.execute(**params)
            else:
                result = await asyncio.wait_for(tool.execute(**params), timeout=ceiling)

            # Unwrap ToolResult here, at the boundary: `execute` promises model
            # text to every caller, and only the agent loop wants the display
            # string (which rides along on ToolOutput). Tool-authored errors
            # carry their own targeted guidance, so no generic hint is added.
            if isinstance(result, ToolResult):
                model_text, display_text = result.model_text, result.display_text
            else:
                model_text, display_text = str(result), None
            return ToolOutput(note + model_text, display_text)
        except asyncio.TimeoutError:
            return note + f"Error: Tool '{name}' timed out after {ceiling:.0f}s." + _hint
        except Exception as e:
            return note + f"Error executing {name}: {str(e)}" + _hint

    @staticmethod
    def _normalize_tool_name(name: str) -> str:
        return name.casefold().replace("_", "").replace("-", "")

    def _repair_tool_name(self, name: str) -> str | None:
        """Map a case/format-mangled tool name onto the registered one.

        Only unambiguous normalization matches (casefold, dropped ``_``/``-``)
        are repaired; anything fuzzier risks executing a tool the model did
        not intend.
        """
        wanted = self._normalize_tool_name(name)
        matches = [n for n in self._tools if self._normalize_tool_name(n) == wanted]
        if len(matches) == 1:
            return matches[0]
        return None

    def _suggest_tool_for_params(self, name: str, params: dict[str, Any]) -> str:
        """Point a mis-routed call at the tool its parameters actually fit.

        Models confuse similarly named tools (exec_read called with read_file's
        path/offset/limit was observed 6 times in one eval task). When the
        provided parameter names satisfy another tool's schema exactly — all
        keys known, all required keys present — say so instead of leaving the
        model to rediscover the split by trial and error.
        """
        provided = set(params)
        if not provided:
            return ""
        for other_name, other in self._tools.items():
            if other_name == name:
                continue
            schema = other.parameters or {}
            props = set(schema.get("properties", {}))
            required = set(schema.get("required", []))
            if provided <= props and required <= provided:
                return f" These parameters match tool '{other_name}' — did you mean to call that instead?"
        return ""

    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
