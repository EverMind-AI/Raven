"""Tool registry for dynamic tool management."""

import asyncio
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from loguru import logger

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

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get all tool definitions in OpenAI format."""
        return [tool.to_schema() for tool in self._visible_tools().values()]

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

        # An argument the tool does not have is normally harmless -- it lands in
        # **kwargs and is ignored. ``machine`` is not harmless: it says WHERE, and a
        # tool that ignores it answers about the wrong machine. Measured 2026-08-19:
        # read_file was given machine="conn_cpu_32c" for a path that also exists
        # here, and returned this computer's copy of the file with nothing to say
        # it had. The path in that run happened not to exist locally, so the loop
        # saw "file not found" and corrected itself; a shared mount or a common
        # path like /opt would have handed it the wrong contents silently.
        if params.get("machine") and "machine" not in (
            tool.parameters.get("properties") or {}
        ):
            return (
                f"Error: {name} runs on this computer and has no 'machine' -- it would have "
                f"ignored the one you gave and answered about the wrong machine. To reach "
                f"{params['machine']!r}, use exec with a machine: "
                f"exec(command=\"...\", machine={params['machine']!r}). Nothing was done."
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
                ends_turn = result.ends_turn
            else:
                model_text, display_text = str(result), None
                retryable, abort_action = True, False
                blocks = None
                ends_turn = None

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
                    ends_turn=ends_turn,
                )
            return ToolOutput(
                model_text,
                display_text,
                retryable=retryable,
                abort_action=abort_action,
                blocks=blocks,
                ends_turn=ends_turn,
            )
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
        return self._visible(name) is not None
