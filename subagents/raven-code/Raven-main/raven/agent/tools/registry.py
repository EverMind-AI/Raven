"""Tool registry for dynamic tool management."""

import asyncio
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
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
        self._aliases: dict[str, str] = {}
        # Optional first-write gate (see raven.agent.workspace_gate). Sits at
        # this single choke point, after alias/mangled-name resolution, for the
        # same reason canonical_name exists: a policy keyed on the raw spelling
        # can be bypassed by a case-mangled call.
        self._write_gate = None
        # The tools a session brought with it, keyed by session, and the
        # turn-local view of one session's set. Split because the two facts have
        # different lifetimes: the binding lasts as long as the session, the
        # visibility only as long as the turn running under it.
        self._session_tools: dict[str, dict[str, Tool]] = {}
        # Turn-local, not a field: one registry serves turns from two sessions
        # concurrently, and a field would let whichever turn entered last decide
        # what the other one can see.
        self._overlay: ContextVar[dict[str, Tool] | None] = ContextVar("tool_registry_overlay", default=None)

    def set_write_gate(self, gate) -> None:
        """Install a callable `(canonical_name, params) -> str | None`;
        a non-None return is handed to the model instead of executing."""
        self._write_gate = gate

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

        The single lookup behind ``get`` / ``has`` / ``execute`` /
        ``canonical_name``, so a session tool cannot be advertised through one
        and missing from another.
        """
        overlay = self._overlay.get()
        if overlay is not None and name in overlay:
            return overlay[name]
        return self._tools.get(self._aliases.get(name, name))

    def _visible_tools(self) -> dict[str, Tool]:
        """Every tool this turn can reach, session tools last so a clash resolves to them."""
        overlay = self._overlay.get()
        if not overlay:
            return self._tools
        return {**self._tools, **overlay}

    def register(self, tool: Tool) -> None:
        """Register a tool (and any legacy-name aliases it declares)."""
        self._tools[tool.name] = tool
        # getattr: plugin/stub tools may duck-type Tool without the attribute.
        for alias in getattr(tool, "aliases", ()):
            self._aliases[alias] = tool.name

    def unregister(self, name: str) -> None:
        """Unregister a tool by name (aliases resolve to the tool they name)."""
        canonical = self._aliases.get(name, name)
        self._tools.pop(canonical, None)
        self._aliases = {a: n for a, n in self._aliases.items() if n != canonical}

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._visible(name)

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return self._visible(name) is not None

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get all tool definitions in OpenAI format."""
        return [tool.to_schema() for tool in self._visible_tools().values()]

    def canonical_name(self, name: str) -> str:
        """The registered name a call to ``name`` actually executes.

        Callers that classify by tool name (untrusted-output fencing, the
        test-evidence gate) must use this, not the model's raw spelling —
        ``execute`` repairs mangled names, so classifying by the raw name
        lets a case-mangled call bypass name-keyed policies.
        """
        if name in self._visible_tools():
            return name
        if name in self._aliases:
            return self._aliases[name]
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
        # Declared aliases are intentional compatibility names — resolve them
        # silently, unlike the mangled-name repair below which announces itself.
        name = self._aliases.get(name, name)
        visible = self._visible_tools()
        tool = visible.get(name)
        if not tool:
            repaired = self._repair_tool_name(name)
            if repaired is None:
                return f"Error: Tool '{name}' not found. Available: {', '.join(visible)}"
            # Models emit case/format-mangled names (Read_File, execRead).
            # Executing the obvious match costs nothing; failing the call
            # costs a turn.
            tool = visible[repaired]
            note = f"[note: tool name {name!r} resolved to {repaired!r}]\n"
            name = repaired

        resolve_aliases = getattr(tool, "resolve_param_aliases", None)
        if resolve_aliases is not None:
            params, alias_note = resolve_aliases(params)
            note += alias_note

        if self._write_gate is not None:
            gate_params = params
            session = str((params or {}).get("session") or "")
            sessions = getattr(tool, "sessions", None)
            session_working_dir = getattr(sessions, "working_dir", None)
            if session and callable(session_working_dir):
                opened_in = session_working_dir(session)
                if opened_in:
                    gate_params = {**params, "_raven_session_workdir": opened_in}
            blocked = await self._write_gate(name, gate_params)
            if blocked is not None:
                return note + blocked

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
        candidates = [*self._visible_tools(), *self._aliases]
        matches = {self._aliases.get(n, n) for n in candidates if self._normalize_tool_name(n) == wanted}
        if len(matches) == 1:
            return matches.pop()
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
        for other_name, other in self._visible_tools().items():
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
        """Get list of registered tool names.

        The registration view, not the turn's: the BM25 tool-search index is
        built once off this and shared by every concurrent turn, so a session's
        own tools have to be read on the search side (see
        ``session_tools_in_scope``) rather than indexed here.
        """
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return self.has(name)
