"""Tool registry for dynamic tool management."""

import asyncio
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
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
        # The tools a session brought with it, keyed by session, and the
        # turn-local view of one session's set. Split because the two facts have
        # different lifetimes: the binding lasts as long as the session, the
        # visibility only as long as the turn running under it.
        self._session_tools: dict[str, dict[str, Tool]] = {}
        # Turn-local, not a field: one registry serves turns from two sessions
        # concurrently, and a field would let whichever turn entered last decide
        # what the other one can see.
        self._overlay: ContextVar[dict[str, Tool] | None] = ContextVar("tool_registry_overlay", default=None)

    def bind_session_tools(self, session_key: str, tools: Mapping[str, Tool]) -> None:
        """Record the tools one session brought, without registering them.

        Registering would publish them process-wide, and one registry serves
        every session on a connection. Held aside instead, and made visible only
        inside :meth:`session_scope_for`. Replaces any earlier set for the same
        session.
        """
        self._session_tools[session_key] = dict(tools)

    def release_session_tools(self, session_key: str) -> None:
        """Forget one session's tools. Idempotent -- most sessions bring none."""
        self._session_tools.pop(session_key, None)

    @contextmanager
    def session_scope(self, tools: Mapping[str, Tool] | None) -> Iterator[None]:
        """Make ``tools`` visible for the current task, and only there.

        Entered where the turn runs, not where it was requested: the turn is
        submitted onto the spine and runs on its own task, which inherits no
        context from the request handler.

        Passing nothing is not a no-op -- it clears the overlay for this task. A
        turn whose session brought no servers must not see another session's.
        """
        token = self._overlay.set(dict(tools) if tools else None)
        try:
            yield
        finally:
            self._overlay.reset(token)

    @contextmanager
    def session_scope_for(self, session_key: str) -> Iterator[None]:
        """:meth:`session_scope` over whatever this session bound, if anything."""
        with self.session_scope(self._session_tools.get(session_key)):
            yield

    def session_tools_in_scope(self) -> dict[str, Tool]:
        """The session tools this turn can see; empty outside any scope."""
        return dict(self._overlay.get() or {})

    def _visible(self, name: str) -> Tool | None:
        """One name resolved the way this turn sees it: its session's tools, then the process's.

        The single lookup behind ``get`` / ``has`` / ``execute``, so a session
        tool cannot be advertised through one and missing from another.
        """
        overlay = self._overlay.get()
        if overlay is not None and name in overlay:
            return overlay[name]
        return self._tools.get(name)

    def _visible_tools(self) -> dict[str, Tool]:
        """Every tool this turn can reach, session tools last so a clash resolves to them."""
        overlay = self._overlay.get()
        if not overlay:
            return self._tools
        return {**self._tools, **overlay}

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._visible(name)

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return self._visible(name) is not None

    def names(self) -> list[str]:
        """Registered tool names, registration-ordered.

        The registration view, not the turn's -- what an MCP connect diffs to
        learn what it added. What this turn can reach is ``get_definitions``.
        """
        return list(self._tools)

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get all tool definitions in OpenAI format."""
        return [tool.to_schema() for tool in self._visible_tools().values()]

    @trace.instrument("tool.call", extract=semconv.tool_call)
    async def execute(self, name: str, params: dict[str, Any]) -> str:
        """Execute a tool by name with given parameters."""
        _hint = _TOOL_ERROR_HINT

        visible = self._visible_tools()
        tool = visible.get(name)
        if not tool:
            return f"Error: Tool '{name}' not found. Available: {', '.join(visible)}"

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
        """Get list of registered tool names.

        The registration view, like :meth:`names`: the BM25 tool-search index is
        built once off this and shared by every concurrent turn, so a session's
        own tools have to be read on the search side (see
        ``session_tools_in_scope``) rather than indexed here.
        """
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return self._visible(name) is not None
