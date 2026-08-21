"""Tool registry for dynamic tool management."""

import asyncio
from collections.abc import Callable
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.agent.tools.base import Tool, ToolOutput, ToolResult
from raven.providers.base import RunMeta
from raven.tracing import semconv, trace

if TYPE_CHECKING:
    from raven.mcp.naming import MCPToolRef

# Where the agent loop parks a tool call's arguments when they do not parse as
# JSON. Named here, next to the only code that must recognise it, so the two
# ends cannot drift into reporting a parse failure as a missing field.
RAW_ARGUMENTS_KEY = "_raw_arguments"


def absent_tool_error(name: str, *, tail: str = "") -> str:
    """What either surface says about a name that did not resolve.

    Both readings, because neither surface can tell them apart: the model
    invented the name, or an MCP server was unloaded mid-turn after this turn's
    prompt promised its tools. "Not found" alone reads as "you got the name
    wrong", which is wrong half the time.

    One function because the two surfaces must not word it differently, and a
    test holding two string literals together is the wrong place for that. The
    ``tail`` is how the folded surface adds where its catalog is; the unfolded
    one has none to point at -- the model is already holding every schema.
    """
    return f"Error: tool '{name}' is not available. It may have been unloaded, or the name may be wrong{tail}."


def _received_tail(params: dict[str, Any]) -> str:
    """The text the model actually emitted, for a call whose arguments did not parse.

    Worth quoting back rather than only naming the fault. Reporting an unparsed
    call through schema validation says "you forgot `path`", which is a lie the
    caller acts on: it re-sends the same malformed JSON with the same field in
    it, and loops -- eighteen calls in one observed session, the model
    eventually theorising about ``_raw_arguments``, a key that exists only
    because we put it there. Showing the text is what ends that.

    Empty when the arguments parsed, so it can be appended unconditionally.
    """
    raw = str(params.get(RAW_ARGUMENTS_KEY) or "")
    if not raw:
        return ""
    return f" Received: {raw[:400]}" + ("..." if len(raw) > 400 else "")


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
        # The one record of where a namespaced tool came from, keyed by the name
        # it is registered under. Every question about an MCP tool -- which
        # server owns it, what it is called there, which registered names a
        # config entry refers to -- is answered from here and nowhere else. It
        # has to be one place: the name is a one-way function of the pair
        # (sanitised, capped, possibly hash-suffixed), so anything that tries to
        # recover the parts from the string is guessing, and three consumers
        # each guessed differently before this existed.
        self._origins: dict[str, MCPToolRef] = {}
        # Turn-local, not an attribute: one gateway registry serves concurrent
        # turns from different channels, and a plain field would let whichever
        # turn set it last decide what the others are shown.
        self._channel: ContextVar[str | None] = ContextVar("tool_registry_channel", default=None)
        # Asked, not stored: the operator's off switches are a *preference*, and
        # a preference read once at startup is one the operator cannot change.
        # See ``set_withheld_source``.
        self._withheld: Callable[[], frozenset[str]] | None = None

    def set_withheld_source(self, source: "Callable[[], frozenset[str]] | None") -> None:
        """Install the answer to "which tools has the operator switched off".

        A callable rather than a set, because the point is that it can change
        while the process runs. It is asked once per assembly (see
        :meth:`get_definitions`), so it is free to be a cached read of a file
        whose mtime it watches.

        This replaces unregistering the tool. Unregistering expressed the
        preference by destroying the thing it was a preference about, so turning a
        tool back on was not merely unimplemented but unimplementable -- nothing
        remembered what to put back. Withholding is reversible by construction,
        and it is the same shape the channel restriction already has.
        """
        self._withheld = source

    def withheld_names(self) -> frozenset[str]:
        """The current off switches, or an empty set when nobody installed a source."""
        if self._withheld is None:
            return frozenset()
        try:
            return self._withheld()
        except Exception:  # noqa: BLE001 - a bad read must not cost the turn its tools
            logger.warning("tools: could not read the disabled-tool list; offering everything")
            return frozenset()

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

        The channel half of :meth:`offers`. Kept separate because the two answer
        different questions -- one is what the surface can carry, the other is
        what the operator asked for -- and only the second one moves while the
        process runs.
        """
        if tool.channels is None:
            return True
        channel = self._channel.get()
        return channel is None or channel in tool.channels

    def offers_by_name(self, name: str) -> bool:
        """Whether ``name`` is a tool the model can actually reach right now.

        The question every caller reaching for ``get(name) is not None`` meant.
        Registration was a fair proxy for availability until the off switch
        stopped unregistering: a withheld tool stays in ``_tools``, which is what
        makes the switch reversible, so ``get`` now answers "this build has such a
        tool" rather than "you may use it".

        It matters wherever the answer becomes prompt text: advertising a tool the
        operator switched off, which ``execute`` then refuses, is the one thing
        the switch is supposed to make impossible.
        """
        tool = self._tools.get(name)
        return tool is not None and self.offers(tool)

    def offers(self, tool: Tool, withheld: "frozenset[str] | None" = None) -> bool:
        """Whether ``tool`` is reachable at all right now.

        The one predicate behind both surfaces a tool can be reached through --
        the schema and tool-search -- so a tool cannot be hidden from one and
        found through the other. That invariant is why the off switch belongs
        here and not at either call site.

        ``withheld`` is passed in when a caller is testing many tools at once, so
        the source is asked once per assembly rather than once per tool.
        """
        if not self.offers_on_this_channel(tool):
            return False
        names = self.withheld_names() if withheld is None else withheld
        return tool.name not in names

    def register(self, tool: Tool, *, origin: "MCPToolRef | None" = None) -> None:
        """Register a tool, recording where it came from if it has an origin.

        ``origin`` is how a namespaced tool declares its parts. Passed at
        registration rather than read back off the tool afterwards, so the
        record cannot disagree with the registration that created it.
        """
        self._tools[tool.name] = tool
        if origin is not None:
            self._origins[tool.name] = origin

    def unregister(self, name: str) -> None:
        """Unregister a tool by name -- the only way a tool leaves.

        No record is kept of what was here. What that costs a turn already in
        flight, and why the miss is worded the way it is, is in :meth:`execute`.
        """
        self._tools.pop(name, None)
        self._origins.pop(name, None)

    def origin_of(self, name: str) -> "MCPToolRef | None":
        """Where this registered tool came from, or None if it has no origin."""
        return self._origins.get(name)

    def names_from(self, server: str) -> list[str]:
        """Every registered name contributed by one MCP server.

        The manager teardown path reads this rather than keeping its own set of
        names: a second copy is a second answer, and the blacklist can
        unregister a name between the connect and the teardown.
        """
        return [name for name, ref in self._origins.items() if ref.server == server]

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools

    def resolve_configured(self, configured: str) -> list[str]:
        """Every registered name a config entry refers to (possibly empty).

        Exact match wins outright. The registry is keyed by name, so a config
        entry naming a tool that exists names *that* tool -- fanning out from
        there would disable something the user did not write. That is not
        hypothetical: two different servers can produce the same historical
        spelling (``mcp_openseo_search_v2`` is the raw form of both
        ``('openseo', 'search_v2')`` and ``('openseo_search', 'v2')``), so a
        fan-out reached across servers into a tool named nothing like the entry.

        The fallback exists for the opposite case -- an entry naming a spelling
        no tool carries today, because sanitising or the length cap changed it.
        Such a name cannot be parsed back into a pair, so each origin generates
        its own spellings forward and the entry is tested against them. Several
        origins can legitimately answer: ``a.b`` and ``a/b`` both clean to
        ``a_b``, and an entry naming the collapsed spelling means both.
        """
        from raven.mcp.naming import spellings

        if configured in self._tools:
            return [configured]
        return [name for name, ref in self._origins.items() if configured in spellings(ref.server, ref.tool)]

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
        """Tool definitions in OpenAI format, minus those not on offer right now.

        Assembled per LLM call, which is what makes an off switch take effect on
        the next turn rather than the next restart: the list is computed here, so
        the only thing a preference has to do is be readable by the time this
        runs.
        """
        withheld = self.withheld_names()
        return [tool.to_schema() for tool in self._tools.values() if self.offers(tool, withheld)]

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
        # Appended to an error the model may recover from by doing something
        # else. Deliberately not on the branch below: a tool that is not there
        # is not one of those, and "try a different approach" argues with an
        # answer whose point is that there is nothing to try.
        _hint = "\n\n[Analyze the error above and try a different approach.]"

        # A withheld tool is still in ``_tools`` by construction -- that is what
        # makes the switch reversible -- so the off switch has to be answered here
        # too, or it is an omission from the array rather than a block. Two ways a
        # name arrives anyway: a model calling from habit rather than from the
        # array (an eval harness leaving only ``execute`` still gets asked for
        # ``read_file``), and a switch flipped mid-conversation, where the array is
        # fresh and the history is not.
        #
        # The channel half of ``offers`` cannot move in here: its ContextVar is
        # unset on internally-initiated calls, so testing it would refuse them all.
        tool = self._tools.get(name)
        if not tool or name in self.withheld_names():
            # No catalog listing on the end of it. Unfolded, every schema is
            # already in this request and a list only repeats it; folded, a
            # cataloged tool is reached through ``tool_call``, which appends the
            # pointer this surface cannot. Measured at 1470 tokens for a
            # 210-tool deploy, in the tool result, kept in history for the run.
            return absent_tool_error(name)

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
            # The tail belongs here too, and this is the path it matters most on:
            # ``flag_truncation`` reads ``arguments_repaired`` to reach its verdict
            # and adds ``truncation`` to that same run_meta, so a streamed reply cut
            # mid-arguments arrives carrying all three -- the flag, the verdict, and
            # the parked text.
            hint = tool.truncation_hint
            return truncation.as_error(name) + (f" {hint}" if hint else "") + _received_tail(params)
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
                "call again with well-formed arguments." + _received_tail(params)
            )
        if run_meta and run_meta.arguments_repaired:
            # Calls arrived after this one, so a cut cannot explain it: the
            # model wrote bad JSON. Telling it to send the content in smaller
            # pieces would send it after a problem it does not have.
            return (
                f"Error: [invalid arguments] The arguments for '{name}' were not valid JSON, "
                f"so this call was not run. Send it again with well-formed arguments." + _received_tail(params)
            )

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
                diff = result.diff
            else:
                model_text, display_text = str(result), None
                retryable, abort_action = True, False
                blocks = None
                diff = None

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
                diff=diff,
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
