"""Tool registry for dynamic tool management."""

import asyncio
import logging
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from raven.agent.tools.base import Tool, ToolOutput, ToolResult
from raven.providers.base import RunMeta
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
        # The tools a session brought with it, keyed by session, and the
        # turn-local view of one session's set. Split because the two facts have
        # different lifetimes: the binding lasts as long as the session, the
        # visibility only as long as the turn running under it.
        self._session_tools: dict[str, dict[str, Tool]] = {}
        # Turn-local, not a field, so the guarantee survives two turns sharing
        # one registry. This surface builds an engine per session today, so the
        # registries are already apart -- but a promise that holds only because
        # of how the engines happen to be built is one a later change breaks
        # silently, and the promise is on the wire (see SESSION_MCP_CAPABILITY).
        self._overlay: ContextVar[dict[str, Tool] | None] = ContextVar("tool_registry_overlay", default=None)

    def bind_session_tools(self, session_key: str, tools: Mapping[str, Tool]) -> None:
        """Record the tools one session brought, without registering them.

        Registering would publish them for the life of the registry. Held aside
        instead, and made visible only inside :meth:`session_scope_for`. Replaces
        any earlier set for the same session.
        """
        self._session_tools[session_key] = dict(tools)

    def release_session_tools(self, session_key: str) -> None:
        """Forget one session's tools. Idempotent -- most sessions bring none."""
        self._session_tools.pop(session_key, None)

    @contextmanager
    def session_scope(self, tools: Mapping[str, Tool] | None) -> Iterator[None]:
        """Make ``tools`` visible for the current task, and only there.

        Entered where the turn runs, not where it was requested: the turn is
        submitted onto the scheduler and runs on its own task, which inherits no
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
        """One name resolved the way this turn sees it: its session's tools, then the registry's.

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

    def get_definitions(self, only_names: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """Tool definitions in OpenAI format, all of them or a named subset.

        `only_names` exists for tool sets large enough that exposing every schema
        every turn is itself a cost. A deck pipeline registers a dozen tools whose
        schemas run to thousands of tokens, and a turn that has not reached the
        authoring stage has no use for the authoring schema. Registration order is
        preserved and unknown names are ignored, so a caller naming a tool that is
        not installed gets a shorter list rather than an error -- the caller is
        describing a stage, not asserting an inventory.
        """
        visible = self._visible_tools()
        if only_names is None:
            return [tool.to_schema() for tool in visible.values()]
        wanted = set(only_names)
        return [tool.to_schema() for name, tool in visible.items() if name in wanted]

    @trace.instrument("tool.call", extract=semconv.tool_call)
    async def execute(
        self,
        name: str,
        params: dict[str, Any],
        *,
        run_meta: RunMeta | None = None,
    ) -> str:
        """Execute a tool by name with given parameters.

        ``run_meta`` carries what happened around the call rather than what it
        asks for -- today only whether it finished arriving, which changes what
        a validation failure means. Keyword-only so the five call sites stay
        self-describing and so a second field does not reorder anything.
        """
        _hint = "\n\n[Analyze the error above and try a different approach.]"

        visible = self._visible_tools()
        tool = visible.get(name)
        if not tool:
            return f"Error: Tool '{name}' not found. Available: {', '.join(visible)}"

        # Refused before dispatch, not after validation: a truncated call whose
        # required fields happen to have arrived still validates, and running it
        # executes an intent that was never fully transmitted. For write_file
        # that is not merely an incomplete write -- `mode` is optional, so a cut
        # before it arrives falls back to "overwrite" and silently replaces
        # everything an earlier append had written, then reports success.
        #
        # The cost of being wrong here is one retry: a turn can end with a
        # complete tool call and be cut in prose that follows it, which the
        # non-streaming path cannot tell apart (see providers/truncation.py).
        # A wasted turn is cheaper than a silent overwrite.
        truncation = run_meta.truncation if run_meta else None
        if truncation:
            hint = tool.truncation_hint
            return truncation.as_error(name) + (f" {hint}" if hint else "")
        if run_meta and run_meta.arguments_repaired and run_meta.last_of_turn:
            # Two facts, two readings, and nothing here to choose between them.
            # The arguments did not parse and nothing arrived after this call,
            # which is the shape a cut leaves -- and equally the shape of a
            # model writing bad JSON on its last call. Each reading gets its own
            # branch rather than one being asserted: a model told to split up a
            # call it merely misspelled goes looking for a size problem it does
            # not have. Longer than a verdict, and the length is the point.
            hint = tool.incomplete_hint
            limit_branch = (
                f"\n\nIf it was the output limit: {hint}"
                if hint
                else "\n\nIf it was the output limit: send this call again in a smaller form."
            )
            return (
                f"Error: [incomplete arguments] The arguments for '{name}' did not parse, and it "
                f"was the last call of the turn. Two things can cause that: the reply hit its "
                f"output token limit part-way through writing this call, or the arguments were "
                f"simply malformed. It was not run either way, and nothing here tells the two "
                f"apart." + limit_branch + "\n\nIf the arguments were malformed: send the same "
                "call again with well-formed arguments."
            )
        if run_meta and run_meta.arguments_repaired:
            # Calls arrived after this one, so a cut cannot explain it: the
            # model wrote bad JSON. Telling it to send the content in smaller
            # pieces would send it after a problem it does not have.
            return (
                f"Error: [invalid arguments] The arguments for '{name}' were not valid JSON, "
                f"so this call was not run. Send it again with well-formed arguments."
            )

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
            # A fault inside the tool, and two things were being lost here. The
            # traceback, which is the only thing that can say where it happened --
            # it went nowhere, so a crash could be seen from the outside and never
            # located. And the difference between "your arguments were wrong" and
            # "this tool is broken": the generic hint invited a retry, so one model
            # met `unhashable type: 'list'`, read it as a statement about its own
            # arguments, and spent six turns and 1.5M tokens rewriting a call that
            # could not have worked.
            logging.getLogger(__name__).exception("tool %s raised", name)
            return (
                f"Error executing {name}: {type(e).__name__}: {e}\n\n"
                "[This is a fault inside the tool, not a problem with the arguments you sent, "
                "so sending them again -- or sending different ones -- will not get past it. "
                "Reach the same end another way, and say plainly what you could not do.]"
            )

    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names.

        The registration view, not the turn's: the BM25 tool-search index is
        built once off this and shared by every turn on this registry, so a
        session's own tools have to be read on the search side (see
        ``session_tools_in_scope``) rather than indexed here.
        """
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return self._visible(name) is not None
