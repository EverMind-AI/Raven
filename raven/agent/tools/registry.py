"""Tool registry for dynamic tool management."""

import asyncio
import copy
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.agent import workdir
from raven.agent.tools.params import cast_params, validate_params
from raven.contracts.llm_provider import RunMeta, TruncationInfo
from raven.contracts.tool import RAW_ARGUMENTS_KEY, Continuation, Tool, ToolOutput, ToolResult
from raven.observability import semconv
from raven.tracing import trace

if TYPE_CHECKING:
    from raven.mcp.naming import MCPToolRef


class ToolAdmissionError(TypeError):
    """A tool refused at the registry's door: an authored member is missing or
    mis-shaped. Raised at registration, where the author sees it — not deep
    inside the turn that first calls the tool."""


@dataclass(frozen=True)
class ToolSpec:
    """The admitted, frozen shape of one registered tool.

    Dispensed once at the door (:func:`admit_tool`) and the only thing the
    registry's own machinery reads afterwards — a consumer that kept reading
    members off the live object let the de-facto contract widen silently
    (measured: 4 authored members on the paper, 13 consumed). Behaviour
    (``execute`` / ``blocking_for`` / ``metadata_owner`` / ``cast_params``)
    stays on ``tool``, the body; data rides here, frozen at admission. Same
    declare→check→dispense pattern as manifest and config-slice admission.
    """

    name: str
    schema: dict[str, Any]
    # An AUTHORED ``to_schema`` override is a signed declaration of a dynamic
    # shape (load_playbook regenerates its enum per render); the registry then
    # serves the live call instead of the snapshot. Undeclared mutation of
    # ``parameters`` still cannot leak — that path stays frozen.
    schema_dynamic: bool
    channels: frozenset[str] | None
    timeout_seconds: float | None
    truncation_hint: str | None
    incomplete_hint: str | None
    # The optional availability declaration (raven/contracts/plugin_surface.py):
    # a tool that bills against a credential the deployment may not have
    # configured authors ``configured()``, and the withheld axis asks it per
    # assembly. Dispensed here so the consumer reads the admitted shape, not a
    # member discovered off the live object; None when the tool declares none.
    configured: Callable[[], bool] | None
    tool: Tool


def admit_tool(tool: Tool) -> ToolSpec:
    """Check the four authored members, normalize the optional ones, dispense
    the frozen spec.

    The advertised schema is derived HERE, from the authored members, and
    deep-copied: the base class's ``to_schema`` is sugar, not the ticket (a
    duck with the four members boards without it), and a shallow snapshot
    would alias the tool's live ``parameters`` dict — a later mutation of the
    object must not leak into what the model is shown. The one sanctioned
    escape is an authored ``to_schema`` override: writing one declares the
    schema dynamic, and the registry serves it live."""
    name = getattr(tool, "name", None)
    if not isinstance(name, str) or not name:
        raise ToolAdmissionError(f"tool {tool!r} declares no usable name")
    description = getattr(tool, "description", None)
    if not isinstance(description, str):
        raise ToolAdmissionError(f"tool {name!r}: description must be a string")
    parameters = getattr(tool, "parameters", None)
    if not isinstance(parameters, dict):
        raise ToolAdmissionError(f"tool {name!r}: parameters must be a JSON-schema mapping")
    if not callable(getattr(tool, "execute", None)):
        raise ToolAdmissionError(f"tool {name!r}: execute is not callable")
    channels = getattr(tool, "channels", None)
    timeout = getattr(tool, "timeout_seconds", None)
    if timeout is not None and not isinstance(timeout, (int, float)):
        raise ToolAdmissionError(f"tool {name!r}: timeout_seconds must be a number or None")
    truncation = getattr(tool, "truncation_hint", None)
    incomplete = getattr(tool, "incomplete_hint", None)
    configured = getattr(tool, "configured", None)
    if configured is not None and not callable(configured):
        # ``configured = False`` reads as an author declaring the tool
        # unavailable; admitting it would offer the tool anyway, silently.
        raise ToolAdmissionError(f"tool {name!r}: configured must be a callable answering bool, or absent")
    schema = {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": copy.deepcopy(parameters),
        },
    }
    own_to_schema = getattr(type(tool), "to_schema", None)
    dynamic = own_to_schema is not None and own_to_schema is not Tool.to_schema
    return ToolSpec(
        name=name,
        schema=schema,
        schema_dynamic=dynamic,
        channels=frozenset(channels) if channels is not None else None,
        timeout_seconds=float(timeout) if timeout is not None else None,
        truncation_hint=truncation if isinstance(truncation, str) else None,
        incomplete_hint=incomplete if isinstance(incomplete, str) else None,
        configured=configured,
        tool=tool,
    )


def _truncation_error(truncation: TruncationInfo) -> str:
    """What is known, and what is only inferred, kept apart.

    Known: the turn stopped at the output limit, because the upstream said so.
    Inferred: that this call was the cut one, which follows from generation
    being sequential but not from anything the upstream said -- a turn can
    finish a call and then hit the limit in the prose after it. Saying "this
    call was cut" as a fact sends a model to split up a call that was whole.

    The refusal is not conditional on that inference. A call that may be
    incomplete is not dispatched either way; being wrong costs one retry. What
    to do about it is the tool's, via ``Tool.truncation_hint``.
    """
    at = f" at the {truncation.at_tokens}-token output limit" if truncation.at_tokens else " at the output limit"
    return (
        f"Error: [truncated] This turn stopped{at}, and this call was the last thing "
        f"being written, so it may have been cut short. It was not run. Send it again."
    )


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


def _charter_withheld(tools: "Mapping[str, Tool]") -> frozenset[str]:
    """What this turn's charter, if any, takes off the table.

    Imported inside the call rather than at module level: a charter belongs to
    the dispatch layer, and the registry is reached from places that must not
    pull that in. The import is a cached lookup after the first turn.
    """
    try:
        from raven.agent.subagent.charter import current_charter, narrowed_tools
    except Exception:  # noqa: BLE001 - no charter support is not a reason to lose tools
        return frozenset()
    # Asked before the names are materialised. This runs on every
    # ``withheld_names`` read, which is every ``execute`` and every schema
    # render, so a turn with no charter must not pay a tuple of every tool name
    # for an answer that is always the empty set.
    if current_charter() is None:
        return frozenset()
    return narrowed_tools(tuple(tools))


def _charter_refusals(name: str, params: dict[str, Any]) -> list[str]:
    """Why this turn's charter refuses this call, or an empty list.

    Lazily imported for the reason ``_charter_withheld`` is, and quiet on any
    failure: a charter that cannot be judged must not be able to stop a turn
    the install would otherwise have run.
    """
    try:
        from raven.agent.subagent.charter import judge, prior_calls

        return judge(name, params, prior_calls())
    except Exception:  # noqa: BLE001 - a brief that cannot be read is not a refusal
        return []


def _record_call(name: str, params: dict[str, Any]) -> None:
    """Remember a call that ran and did not fail.

    After dispatch, never before: a rule that asks for a prior ``read_file``
    is asking whether the file was read, and a read that errored read nothing.
    """
    try:
        from raven.agent.subagent.charter import record_call

        record_call(name, params)
    except Exception:  # noqa: BLE001 - bookkeeping must not cost the call its result
        pass


def call_failed(result: Any) -> bool:
    """Whether one tool call went wrong, by the registry's own convention.

    The tool's verdict when it gave one, and the failure text as the backstop
    when it did not: ``execute`` returns a :class:`ToolOutput` on the paths that
    run a tool, and a bare string on the ones that refuse before dispatch
    (unparseable arguments, an invalid parameter set, a timeout), where there is
    no object to carry ``ok`` and the leading ``Error`` is the whole of the
    signal.

    One function because the rule had begun to be copied: reading only ``ok``
    calls a refused-before-dispatch call a success, and reading only the text
    calls a tool that answers with the word Error a failure.
    """
    if not bool(getattr(result, "ok", True)):
        return True
    text = getattr(result, "model_text", None)
    return str(text if text is not None else result).startswith("Error")


# When a running tool first says so, and how often after that. The first line is
# late enough that ordinary calls never print one, and early enough that a reader
# tailing the log during a slow call does not have to wait a full minute to learn
# it is a slow call rather than a dead one.
HEARTBEAT_FIRST_S = 30.0
HEARTBEAT_EVERY_S = 60.0


async def _heartbeat(name: str, ceiling: float) -> None:
    """Say that a tool is still running, for as long as it still is.

    A tool between its start line and its result line was indistinguishable from
    a process that had stopped. Measured on a real run: `ppt_prepare` held the
    turn for 600 seconds, twice, and the log for that whole window holds three
    lines -- the call, the timeout, and the next iteration. Nothing said which of
    the tool's own steps it was in, or that it was in any of them, so the first
    question asked of the record ("was it working?") had no answer in it.

    A line rather than an event: this is the registry, which has no outlet, and
    the question it answers is the one asked of a log after the fact. What a
    reader watching the page sees while a tool runs is a separate seam.

    Cancelled by the caller when the call returns, so the last line is always one
    the tool outlived.
    """
    started = time.monotonic()
    delay = HEARTBEAT_FIRST_S
    while True:
        await asyncio.sleep(delay)
        delay = HEARTBEAT_EVERY_S
        logger.info(
            "tools: {} still running after {:.0f}s of its {:.0f}s ceiling",
            name,
            time.monotonic() - started,
            ceiling,
        )


class ToolRegistry:
    """
    Registry for agent tools.

    Allows dynamic registration and execution of tools.
    """

    # Backstop ceiling for tools that don't set their own ``timeout_seconds``.
    # Generous on purpose: it exists to break an infinite hang (a tool with no
    # internal timeout that never returns), not to enforce a tight per-tool SLA.
    DEFAULT_TOOL_TIMEOUT_S = 300.0

    def __init__(self, *, tool_gates: "Sequence[Any]" = (), permission_gate: "Any | None" = None):
        self._tools: dict[str, Tool] = {}
        # The platform's own gate (raven.permissions), distinct from the plugin
        # gates below: it can wait on a human mid-adjudication and answers with
        # a full ToolResult, both of which the plugin paper deliberately does
        # not offer. Fixed at construction like the gates -- an unattended
        # registry is built with a gate that refuses to ask.
        self._permission_gate = permission_gate
        # Cast at assembly, fixed for the generation (paper:
        # contracts/tool_gate.py): no setter, no latch -- changing gates is a
        # generation swap. Ordered by (name, contributed_by) so adjudication
        # is deterministic whatever order the builder yielded them in.
        self._tool_gates: tuple[Any, ...] = tuple(
            sorted(tool_gates, key=lambda g: (str(getattr(g, "name", "")), str(getattr(g, "contributed_by", ""))))
        )
        # The admitted, frozen specs, keyed like ``_tools``. The registry's own
        # machinery reads data from here and calls behaviour on the body; the
        # pair is written together in register() and nowhere else.
        self._specs: dict[str, ToolSpec] = {}
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
        # ``_withheld``, ``_schema_hidden`` and the turn freeze below are
        # distinct axes: the first is an operator off switch (a withheld tool is
        # unreachable everywhere), the second a design property (a hidden tool
        # stays callable -- ``tool_call`` resolves by registry, never by schema
        # -- it is just not advertised), and the freeze is per turn (a mid-turn
        # arrival is unreachable through every surface until the next turn --
        # ``offers`` consults it for schema and tool-search, and ``execute``
        # consults it for dispatch, so the three cannot disagree).
        self._withheld: Callable[[], frozenset[str]] | None = None
        self._schema_hidden: set[str] = set()
        # The tools a session brought with it, keyed by session, plus the
        # turn-local view of one session's set. Two halves because the two facts
        # have different lifetimes: the binding lives as long as the session, the
        # visibility only as long as the turn that runs under it.
        #
        # A ContextVar and not a field, for the reason ``_channel`` gives: turns
        # from two sessions run concurrently on one registry, and a field would
        # let whichever turn entered last decide what the other one can see.
        self._session_tools: dict[str, dict[str, Tool]] = {}
        self._overlay: ContextVar[dict[str, Tool] | None] = ContextVar("tool_registry_overlay", default=None)
        # The tools this turn may be shown, captured at turn entry as the
        # (name, instance) pairs themselves. Instances and not names: a
        # same-name re-registration is a different tool wearing a familiar
        # label, and admitting it by name would swap the served schema between
        # two model calls of one turn. A ContextVar for the reason ``_channel``
        # gives; ``None`` (no turn scope) advertises everything, which is what
        # registries that never enter one -- a sub-agent's, the curator's -- get.
        self._turn_names: ContextVar[dict[str, Tool] | None] = ContextVar("tool_registry_turn_names", default=None)
        # The off switches as they stood at turn entry. Unioned into
        # ``withheld_names`` so a switch that turns a tool ON mid-turn lands on
        # the next turn -- the tool array is the prompt-cache prefix, and a
        # live un-withholding would move it between two model calls exactly the
        # way a late registration would. Turning a tool OFF stays live: the
        # union can only add entries, never mask one the current read carries.
        self._turn_withheld: ContextVar[frozenset[str] | None] = ContextVar("tool_registry_turn_withheld", default=None)

    @property
    def tool_gates(self) -> tuple[Any, ...]:
        """The gates cast over this registry at construction, in adjudication
        order. Read-only on purpose: the paper's first discipline."""
        return self._tool_gates

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
        """The current off switches, or an empty set when nobody installed a source.

        Inside a turn scope, whatever was withheld at the turn's entry stays
        withheld for the whole turn (see ``_turn_withheld``); switches that
        tighten mid-turn still land on this very read.
        """
        frozen = self._turn_withheld.get() or frozenset()
        # A dispatch's charter narrows this turn the same way an off switch
        # does, and joins by union for the same reason: every source here may
        # take a tool away and none may hand one back. That is what makes a
        # charter safe to accept from another process -- the worst a bad one
        # can do is leave this turn with fewer tools than it would have had.
        charter = _charter_withheld(self._tools)
        if self._withheld is None:
            return frozen | charter
        try:
            return self._withheld() | frozen | charter
        except Exception:  # noqa: BLE001 - a bad read must not cost the turn its tools
            logger.warning("tools: could not read the disabled-tool list; offering everything")
            return frozen | charter

    def hide_from_schema(self, *names: str) -> None:
        """Keep tools registered and callable, but out of the provider's tool schema.

        For tools whose only advertisement is another tool's result text. Unlike
        the off switch this is not operator-reversible: the tool itself is what
        decides it is not for the schema.
        """
        self._schema_hidden.update(names)

    def schema_hidden_names(self) -> frozenset[str]:
        """Every registered name withheld from the schema (still callable)."""
        return frozenset(self._schema_hidden)

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
        tool = self._visible(name)
        return tool is not None and self.offers(tool)

    def offers(self, tool: Tool, withheld: "frozenset[str] | None" = None) -> bool:
        """Whether ``tool`` is reachable at all right now.

        The one predicate behind both surfaces a tool can be reached through --
        the schema and tool-search -- so a tool cannot be hidden from one and
        found through the other. That invariant is why the off switch belongs
        here and not at either call site -- and why the turn freeze does too: a
        mid-turn arrival kept out of the schema but findable through tool-search
        would be the same disagreement.

        ``withheld`` is passed in when a caller is testing many tools at once, so
        the source is asked once per assembly rather than once per tool.
        """
        if not self.offers_on_this_channel(tool):
            return False
        if not self._visible_to_this_turn(tool.name):
            return False
        names = self.withheld_names() if withheld is None else withheld
        return tool.name not in names

    def _visible_to_this_turn(self, name: str) -> bool:
        """Whether the turn freeze admits this name; always true outside a scope.

        Admission is by identity, not by name: the entry pair must still be the
        registered pair. A same-name re-registration therefore reads as the
        removal it starts with -- the name drops out for the rest of the turn,
        which the asymmetry allows -- and the replacement is an addition that
        lands on the next turn like any other, instead of changing an admitted
        entry's schema between two model calls.

        Consulted by ``offers`` (schema, tool-search) and by ``execute``
        (dispatch): a name the turn cannot see is one the turn cannot run,
        or a call composed against the entry instance's schema would be
        dispatched to whatever now wears the name.

        Session-overlay tools are admitted by name: they enter with the turn
        that carries them, after the freeze captured the base registry.
        """
        frozen = self._turn_names.get()
        if frozen is None:
            return True
        entry = frozen.get(name)
        if entry is not None and self._tools.get(name) is entry:
            return True
        overlay = self._overlay.get()
        return bool(overlay and name in overlay)

    def register(self, tool: Tool, *, origin: "MCPToolRef | None" = None) -> None:
        """Register a tool, recording where it came from if it has an origin.

        ``origin`` is how a namespaced tool declares its parts. Passed at
        registration rather than read back off the tool afterwards, so the
        record cannot disagree with the registration that created it.
        """
        spec = admit_tool(tool)
        self._tools[spec.name] = tool
        self._specs[spec.name] = spec
        if origin is not None:
            self._origins[spec.name] = origin

    def unregister(self, name: str) -> None:
        """Unregister a tool by name -- the only way a tool leaves.

        No record is kept of what was here. What that costs a turn already in
        flight, and why the miss is worded the way it is, is in :meth:`execute`.
        """
        self._tools.pop(name, None)
        self._specs.pop(name, None)
        self._origins.pop(name, None)

    def origin_of(self, name: str) -> "MCPToolRef | None":
        """Where this registered tool came from, or None if it has no origin."""
        return self._origins.get(name)

    def spec_of(self, name: str) -> ToolSpec | None:
        """The admitted shape of a registered tool, or None for a name the door
        never saw (a session overlay tool, or nothing by that name)."""
        return self._specs.get(name)

    def names_from(self, server: str) -> list[str]:
        """Every registered name contributed by one MCP server.

        The manager teardown path reads this rather than keeping its own set of
        names: a second copy is a second answer, and the blacklist can
        unregister a name between the connect and the teardown.
        """
        return [name for name, ref in self._origins.items() if ref.server == server]

    def bind_session_tools(self, session_key: str, tools: "Mapping[str, Tool]") -> None:
        """Record the tools one session brought with it, without registering them.

        Registering would publish them process-wide: one registry serves every
        session on a connection, so a tool registered for session A stays
        reachable from session B's turn and from the next session on the same
        process. These are held aside instead, and made visible only inside
        :meth:`session_scope_for`.

        Replaces any earlier set for the same session -- a session that brings
        servers twice (``session/new`` then ``session/load``) means the second
        set, not both.
        """
        self._session_tools[session_key] = dict(tools)

    def release_session_tools(self, session_key: str) -> None:
        """Forget one session's tools. Idempotent -- a session that brought none is normal."""
        self._session_tools.pop(session_key, None)

    @contextmanager
    def session_scope(self, tools: "Mapping[str, Tool] | None") -> Iterator[None]:
        """Make ``tools`` visible for the duration of the current task, and only there.

        Entered where the turn runs, not where it was requested: the turn is
        submitted onto the spine and executes on its own task, which does not
        inherit a context set by the request handler.

        Passing nothing (or an empty mapping) is not a no-op -- it clears the
        overlay for this task. A turn whose session brought no servers must not
        see another session's, so "no tools" has to be stated rather than left to
        whatever the surrounding context happened to hold.
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

    @contextmanager
    def turn_scope(self) -> Iterator[None]:
        """Freeze which registered tools this turn's schema may carry.

        A tool that registers while the turn runs -- an MCP handshake finishing
        in the background is the ordinary case since ``prewarm_mcp`` -- joins the
        next turn instead of appearing in this one's tool array between two model
        calls. The array is the first segment of the prompt-cache prefix, so a
        mid-turn arrival rebuilds the whole cached prompt; the arrival loses
        nothing by waiting, because ``_mcp_tool_notices`` already tells the model
        the server is still connecting.

        The freeze is against ADDITIONS to the array, whatever their mechanism:
        a registration that lands mid-turn, and equally an off switch that turns
        a tool back ON mid-turn (the withheld set as of entry is unioned into
        every ``withheld_names`` read for the turn). Removals pass through both
        axes -- a tool that left the registry has no schema to serve, and a
        switch that tightens mid-turn must bind the very next call.
        A same-name re-registration is both at once -- a removal followed by an
        addition wearing the old label -- and each half keeps its own timing:
        the entry drops out of this turn (captured pairs are checked by
        identity, see ``_visible_to_this_turn``), and the replacement joins the
        next one. Anything else would swap an admitted entry's schema in place,
        moving the array as surely as a new name would. The one lane that may
        still re-render mid-turn is a tool that DECLARED a dynamic schema
        (``schema_dynamic``): its instance is stable and its variability is its
        contract, so the freeze pins the pair and leaves the rendering to it.
        Session-overlay tools are exempt: they enter with the turn that carries
        them.
        """
        token = self._turn_names.set(dict(self._tools))
        wtoken = self._turn_withheld.set(self.withheld_names())
        try:
            yield
        finally:
            self._turn_withheld.reset(wtoken)
            self._turn_names.reset(token)

    def session_tools_in_scope(self) -> dict[str, Tool]:
        """The session tools this turn can see; empty outside any scope.

        For the one reader that cannot be served by ``_visible_tools``:
        tool-search ranks a BM25 index that is built once and shared by every
        concurrent turn, so a turn's own tools have to be added on the read side
        instead of indexed. ``tool_names`` / ``names`` stay the registration
        view -- MCP teardown diffs them to learn what a connect added.
        """
        return dict(self._overlay.get() or {})

    def _visible(self, name: str) -> Tool | None:
        """One name resolved the way this turn sees it: its session's tools, then the process's.

        The single lookup behind ``get`` / ``has`` / ``execute`` / ``is_blocking``
        / ``take_metadata``, so a session tool cannot be advertised through one
        and missing from another -- the shape that would show up as a tool the
        model is offered and then told does not exist.
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

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._visible(name)

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return self._visible(name) is not None

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

        if configured in self._visible_tools():
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
        tool = self._visible(name)
        return bool(tool is not None and tool.blocking_for(params or {}))

    def take_metadata(self, name: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Consume the structured payload this call left for the turn stream.

        Takes the call's params for the same reason ``is_blocking`` does: a
        forwarding tool (``tool_call``) owns none of the metadata it returns, so
        the payload must be collected from whatever it forwarded to.
        """
        tool = self._visible(name)
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
        # Served from the admitted snapshot, not the live object: what the
        # model is shown is what the door checked, whatever the object has
        # grown or mutated since. A tool that AUTHORED its own to_schema has
        # declared a dynamic shape (load_playbook's per-render enum) and is
        # served live -- declared dynamism, not silent widening. A session
        # overlay tool never passed the door and keeps its live schema.
        out: list[dict[str, Any]] = []
        for name, tool in self._visible_tools().items():
            if not self.offers(tool, withheld) or name in self._schema_hidden:
                continue
            spec = self._specs.get(name)
            if spec is None or spec.schema_dynamic:
                out.append(tool.to_schema())
            else:
                out.append(spec.schema)
        return out

    def hidden_definition(self, name: str) -> dict[str, Any] | None:
        """One schema-hidden tool's definition, in the shape a visible tool gets.

        A hidden tool reaches the model only through another tool's result text,
        so that text has to carry the definition -- and carrying a hand-written
        copy of it is what drifted: the model wrote ``action`` for ``decision``
        and lost a call to it three runs running. Served from here so the
        advertisement cannot say anything the door did not check.

        ``None`` when the name is not registered, or when the tool is *not*
        hidden: a visible tool is already in the array, and advertising it a
        second time is the duplication this exists to end.
        """
        if name not in self._schema_hidden:
            return None
        tool = self.get(name)
        if tool is None:
            return None
        spec = self._specs.get(name)
        # Same snapshot-or-live rule as ``get_definitions``: an AUTHORED
        # ``to_schema`` is the tool signing for a shape that is not fixed at
        # admission, and ``resolve_dag_node``'s is -- its node shape carries a
        # ``subagent`` enum built from the hot-appliable agent table.
        if spec is None or spec.schema_dynamic:
            return tool.to_schema()
        return copy.deepcopy(spec.schema)

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
        # The turn freeze CAN and must: a call composed against the entry
        # instance's schema would otherwise dispatch to a same-name replacement
        # that landed mid-turn, running arguments shaped for a tool that is no
        # longer registered. The freeze admits on an unset scope (a registry
        # that never enters one, an internally-initiated call), so unlike the
        # channel gate it refuses nothing it should not.
        tool = self._visible(name)
        # A registered tool answers data questions from its admitted spec; a
        # session overlay tool never passed the door and answers live.
        spec = self._specs.get(name) if tool is not None and self._tools.get(name) is tool else None
        if not tool or name in self.withheld_names() or not self._visible_to_this_turn(name):
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
            hint = (spec or tool).truncation_hint
            return _truncation_error(truncation) + (f" {hint}" if hint else "") + _received_tail(params)
        if run_meta and run_meta.arguments_repaired and run_meta.last_of_turn:
            # Two facts, two readings, and nothing here to choose between them.
            # The arguments did not parse and nothing arrived after this call,
            # which is the shape a cut leaves -- and equally the shape of a
            # model writing bad JSON on its last call. Each reading gets its own
            # branch rather than one being asserted: a model told to split up a
            # call it merely misspelled goes looking for a size problem it does
            # not have. Longer than a verdict, and the length is the point.
            hint = (spec or tool).incomplete_hint
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
            params = cast_params(tool.parameters, tool.cast_params(params))

            errors = validate_params(tool.parameters, params) + tool.validate_params(params)
            # This turn's charter, judged where a schema error is judged and for
            # the same reason: the refusal reaches the model as this call's
            # result, so the next attempt can be right. Ahead of the permission
            # gate, which is about what a person allows rather than what this
            # dispatch was briefed to do.
            errors += _charter_refusals(name, params)
            if errors:
                return f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors) + _hint

            # Post-validation, pre-dispatch, and ahead of the plugin gates:
            # platform authority precedes plugin policy. The refusal (or the
            # human's deny) replaces the call; approval waits deliberately
            # happen before the timeout ceiling below, so a human deciding is
            # never timer-killed.
            if self._permission_gate is not None:
                refusal = await self._permission_gate.enforce(name, params)
                if refusal is not None:
                    return ToolOutput(
                        refusal.model_text,
                        refusal.display_text,
                        retryable=refusal.retryable,
                        blocks_call=refusal.blocks_call,
                        continuation=refusal.continuation,
                        ok=refusal.ok,
                    )

            # Post-validation, pre-dispatch: the point the paper names. Guarded
            # so a registry with no gates runs today's path byte for byte.
            if self._tool_gates:
                verdict = await self._adjudicate(name, params)
                if verdict is not None:
                    return verdict

            ceiling = (spec or tool).timeout_seconds or self.DEFAULT_TOOL_TIMEOUT_S
            if tool.blocking_for(params):
                # Intentionally waits on a human — must not be timer-killed.
                result = await tool.execute(**params)
            else:
                # The heartbeat is a bystander: it neither holds the result nor
                # touches the deadline, so a change here cannot alter what the
                # tool returns or when it is killed.
                beat = asyncio.ensure_future(_heartbeat(name, ceiling))
                try:
                    result = await asyncio.wait_for(tool.execute(**params), timeout=ceiling)
                finally:
                    beat.cancel()

            # Unwrap ToolResult here, at the boundary: `execute` promises model
            # text to every caller, and only the agent loop wants the display
            # string and multimodal blocks (which ride along on ToolOutput).
            if isinstance(result, ToolResult):
                model_text, display_text = result.model_text, result.display_text
                retryable, blocks_call = result.retryable, result.blocks_call
                continuation = result.continuation
                ok = result.ok
                blocks = result.blocks
                diff = result.diff
                file_change = result.file_change
            else:
                model_text, display_text = str(result), None
                retryable, blocks_call = True, False
                continuation = Continuation.CONTINUE
                ok = bool(getattr(result, "ok", True))
                blocks = None
                diff = None
                file_change = None
            # Remembered once the verdict is in, and only when it is good. A
            # rule that asks for a prior ``read_file`` is asking whether the
            # file was read; a read that errored read nothing, and letting it
            # satisfy the rule would hand the next call exactly the permission
            # the rule exists to withhold. Judged by ``call_failed`` rather than
            # by ``ok`` alone, for the reason that function was written: a tool
            # answering with a bare error string carries no ``ok`` to read.
            #
            # The result itself, not its text: ``call_failed`` reads ``ok``
            # first and falls back to the text only when there is no ``ok`` to
            # read, so handing it the unwrapped string is what would lose the
            # verdict -- a ``run_shell`` that exited non-zero says so through
            # ``ok`` and its output rarely begins with the word Error.
            if not call_failed(result):
                _record_call(name, params)

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
                    blocks_call=blocks_call,
                    continuation=continuation,
                    ok=False,
                )
            return ToolOutput(
                model_text,
                display_text,
                retryable=retryable,
                blocks_call=blocks_call,
                continuation=continuation,
                ok=ok,
                blocks=blocks,
                diff=diff,
                file_change=file_change,
            )
        except asyncio.TimeoutError:
            return f"Error: Tool '{name}' timed out after {ceiling:.0f}s." + _hint
        except Exception as e:
            return f"Error executing {name}: {str(e)}" + _hint

    async def _adjudicate(self, name: str, params: dict[str, Any]) -> str | None:
        """Run the cast gates over one validated call; the first non-None
        verdict replaces it.

        A verdict is returned verbatim -- no retry hint appended: the
        adjudication text is the complete result. A gate that raises refuses
        the call it was adjudicating (fail-closed, naming the gate), because
        failing open would make a gate's bugs silent permission grants. The
        turn's working directory is passed explicitly so a gate never fishes
        it out of params; None outside a bound turn (a subagent backend, an
        internally-initiated call), and the gate decides for itself.
        """
        session_workdir = workdir.current()
        for gate in self._tool_gates:
            gate_name = getattr(gate, "name", None) or type(gate).__name__
            try:
                verdict = await gate.adjudicate(name, params, session_workdir=session_workdir)
            except Exception as e:
                return f"Error: tool call '{name}' was refused by gate {gate_name}: {e}"
            if verdict is not None:
                return verdict
        return None

    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
