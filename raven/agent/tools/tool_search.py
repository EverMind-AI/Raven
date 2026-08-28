"""Progressive tool disclosure for large catalogs.

When built-ins + plugins + MCP push the tool count past a threshold, injecting
every schema into each request burns context that scales with tool count. This
module withholds most schemas and exposes two meta-tools instead:

  - ``tool_search`` — BM25 keyword search over the hidden catalog; each hit
                      carries name + description + parameter schema, enough to
                      call the tool without a second lookup.
  - ``tool_call``   — invoke a cataloged tool by name; forwards through the
                      registry, which validates arguments and returns a
                      correctable error when they don't fit the schema.

The tool list sent to the model never changes turn-to-turn (always the core
set + these two meta-tools), so the prompt cache stays stable across the
whole session — tools sit ahead of system+messages in the cached prefix, so a
changing tool list would invalidate everything after it. The cost is that
cataloged tools are invoked through ``tool_call`` rather than native
function-calling.

Two visibility tiers per turn (see :class:`ToolSearchStrategy`):
  - always-visible: a core set + the meta-tools (full schema every turn);
  - cataloged:      everything else — searchable, schema withheld until asked.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, NamedTuple

from raven.agent.tools.base import Tool
from raven.agent.tools.registry import absent_tool_error
from raven.agent.tools.tool_index import ToolIndex
from raven.token_wise.base import TokenStrategy

if TYPE_CHECKING:
    from raven.agent.tools.registry import ToolRegistry

# Core tools kept exposed every turn — the agent would be crippled having to
# search for these. Beyond the file/search/exec primitives, ``message``,
# ``ask_user`` and ``spawn`` are interaction/orchestration primitives the agent
# must reach on any turn (reply, unblock via a question, delegate a subagent) —
# hiding them risks the model not thinking to search for them at all.
#
# Deliberately NOT here: ``run_subagent_dag`` (a fan-out of minute-scale
# sub-agent runs is a deliberate act, not a per-turn primitive; it should be
# reached through ``tool_search`` like any other optional capability) and
# ``read_skill``. Under compaction that costs a ``tool_search`` hop before a
# ``inject: description`` skill's body can be loaded — accepted, since the
# alternative is keeping every progressive-disclosure entry point resident.
# Config ``tools.tool_search.always_visible`` adds either one back per deploy.
DEFAULT_ALWAYS_VISIBLE: tuple[str, ...] = (
    "read_file",
    "write_file",
    "edit_file",
    "list_dir",
    "grep",
    "find",
    "exec",
    "message",
    "ask_user",
    "spawn",
)

TOOL_CALL_NAME: str = "tool_call"
# The meta-tools: always registered when the feature is on, never cataloged.
META_TOOL_NAMES: frozenset[str] = frozenset({"tool_search", TOOL_CALL_NAME})


class _Target(NamedTuple):
    """What one ``tool_call`` name resolves to. Exactly one field is set."""

    tool: "Tool | None"
    refusal: str | None


class ToolSearchController:
    """Shared state between the meta-tools and the strategy.

    Holds the live registry (the catalog source of truth) and the BM25 index.
    The visible set is constant (core + meta-tools), which keeps the per-turn
    tool list — and thus the prompt cache — stable.
    """

    def __init__(
        self,
        registry: "ToolRegistry",
        *,
        always_visible: set[str],
        search_result_limit: int = 10,
        compaction_threshold: int = 50,
    ) -> None:
        self._registry = registry
        # The configured spellings, kept as configured. Resolved to registered
        # names on every read instead of once here -- see visible_names.
        self._configured_visible = set(always_visible)
        self.search_result_limit = search_result_limit
        # The single home for the fold threshold: the strategy's per-turn
        # filter and :meth:`tool_call_available` both read it from here, so an
        # advertisement can never promise a route the fold is not shipping.
        self.compaction_threshold = compaction_threshold
        self._index = ToolIndex()

    def _catalog_tools(self) -> list[Tool]:
        """All registered tools except the meta-tools (never self-searchable).

        Deliberately not channel-filtered: the BM25 index is keyed on this set
        and shared by every concurrent turn, so making it channel-dependent
        would have web and IM turns swapping the index out from under each
        other. The channel filter belongs on the reads instead -- ``search``
        and ``resolve_target``.
        """
        out = []
        for name in self._registry.tool_names:
            # Schema-hidden tools are advertised by their owning tool's result
            # text, not by search; indexing them would give them a second,
            # uninvited discovery path (tool_call still resolves them).
            if name in META_TOOL_NAMES or name in self._registry.schema_hidden_names():
                continue
            tool = self._registry.get(name)
            if tool is not None:
                out.append(tool)
        return out

    def refresh(self) -> None:
        """Sync the BM25 index with the current registry (no-op if unchanged)."""
        self._index.ensure(self._catalog_tools())

    def visible_names(self) -> set[str]:
        """Configured names resolved against the registry, per call.

        Not resolved once in ``__init__``, for two reasons that both point the
        same way. MCP servers attach while raven runs, so a name configured here
        may not be registered yet when this object is built. And a namespaced
        tool's registered name need not equal the configured one: sanitising and
        the length cap rewrite it, so a config entry written against the old
        spelling would silently stop keeping its tool resident -- one extra
        search hop for the model, and no error anywhere to say why.

        A configured name that resolves to nothing is kept as written. Such an
        entry usually names a tool this deploy does not have, which is ordinary,
        and dropping it would only hide the typo case rather than report it.
        """
        out = set(META_TOOL_NAMES)
        for entry in self._configured_visible:
            out.update(self._registry.resolve_configured(entry) or [entry])
        return out

    def search(self, query: str, limit: int | None = None) -> list[dict[str, Any]]:
        """Hits carry name + description + parameter schema, so the model can go
        straight to tool_call without a separate describe round-trip.

        Rank, then filter, then truncate -- in that order. Truncating first lets
        a tool this channel cannot use consume a result slot, so a usable
        lower-ranked hit is dropped and the model is told nothing matched.
        Reading that many names is free: the index scores and sorts the whole
        catalog either way, and only the final slice is bounded. The bound is
        the registry's current size, not the index's -- it stays >= the indexed
        catalog because ``refresh()`` runs at the top of every
        ``before_llm_call``, ahead of any search. Unregistering a tool after the
        last refresh is the one state that inverts that and truncates the
        ranking ahead of the filter.
        """
        cap = limit or self.search_result_limit
        ranked = self._index.search(query, len(self._registry.tool_names) or cap)
        # Asked once for the whole scan, not once per candidate: the source
        # behind it is a file read, and the loop below walks the entire ranked
        # catalog. One answer for the scan is also the more correct one -- a set
        # re-read mid-loop could offer a tool and withhold its neighbour from
        # the same list.
        withheld = self._registry.withheld_names()
        hits = []
        for name in ranked:
            tool = self._registry.get(name)
            if tool is None or not self._registry.offers(tool, withheld):
                continue
            hits.append(
                {
                    "name": name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                }
            )
            if len(hits) >= cap:
                break
        return hits

    def resolve_target(self, name: Any) -> _Target:
        """What ``tool_call`` would forward this name to, and why not when it would not.

        One resolution with two readers. Splitting it meant walking the same
        three checks twice -- the meta-tool guard, the registry lookup, the
        channel filter -- so exactly one of ``tool`` and ``refusal`` is set.

        The reason is part of the return rather than the caller's to reconstruct,
        because the two ways a name fails want different answers:

        - not a string, or a meta-tool: refused here rather than resolved, or
          ``tool_call`` would recurse into itself.
        - absent, or withheld from this channel: one answer for both, and the
          same sentence the direct path uses. Withheld is answered as absent
          deliberately -- "it exists but not for you" is itself the way around
          the filter that keeping it out of the schema was meant to close.
          Appending where the catalog is is safe for that branch because
          ``search`` filters by channel too. It reads as a statement of where
          the truth lives, not an instruction to search again: a model that
          searched, called what it found, and lost the tool in between would
          otherwise be sent round the same loop.
        """
        if not isinstance(name, str):
            return _Target(None, "Error: 'name' must be a string naming a tool.")
        if name in META_TOOL_NAMES:
            return _Target(None, f"Error: '{name}' cannot be invoked via tool_call.")
        tool = self._registry.get(name)
        if tool is None or not self._registry.offers(tool):
            return _Target(None, absent_tool_error(name, tail=" -- tool_search lists what is currently loaded"))
        return _Target(tool, None)

    def tool_call_available(self) -> bool:
        """Whether ``tool_call`` is in the model's per-turn tool list right now.

        The predicate behind the DAG acceptance text's advertisement of the
        schema-hidden control tools: it must agree with what
        :class:`ToolSearchStrategy` actually ships, or the text promises a
        route that does not exist. Both read the threshold from here, and both
        test the same two things -- the meta-tool is registered and not
        withheld, and the catalog sits above the fold.
        """
        if TOOL_CALL_NAME not in self._registry.tool_names:
            return False
        if TOOL_CALL_NAME in self._registry.withheld_names():
            return False
        catalog = sum(1 for t in self._registry.get_definitions() if t["function"]["name"] not in META_TOOL_NAMES)
        return catalog > self.compaction_threshold

    def target_is_blocking(self, name: Any) -> bool:
        """Whether the tool ``tool_call`` would forward to is a blocking interaction.

        A meta-tool target is refused by :meth:`call`, so it reports non-blocking
        here too — which also stops the registry lookup recursing back into this
        controller.
        """
        if not isinstance(name, str) or name in META_TOOL_NAMES:
            return False
        return self._registry.is_blocking(name)

    async def call(self, name: str, arguments: dict[str, Any] | None) -> str:
        """Invoke a cataloged tool: forward to the registry (validates args).

        Models sometimes emit the nested ``arguments`` as a JSON string rather
        than an object; parse that case so the call still goes through.
        """
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                return "Error: 'arguments' must be a JSON object."
        target = self.resolve_target(name)
        if target.refusal is not None:
            return target.refusal
        return await self._registry.execute(name, arguments or {})


class ToolSearchTool(Tool):
    """Keyword search over tools whose schemas are not currently loaded."""

    def __init__(self, controller: ToolSearchController) -> None:
        self._ctrl = controller

    @property
    def name(self) -> str:
        return "tool_search"

    @property
    def description(self) -> str:
        return (
            "Search the catalog of additional tools that are available but not "
            "currently loaded. Returns matching tools with their description and "
            "parameter schema, ready to invoke with tool_call. Query with task "
            "keywords, e.g. 'create github issue' or '生成图片'."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Task keywords describing the capability you need.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of results.",
                    "minimum": 1,
                    "maximum": 10,
                },
            },
            "required": ["query"],
        }

    async def execute(self, query: str, limit: int | None = None) -> str:
        hits = self._ctrl.search(query, limit)
        if not hits:
            return f"No tools matched '{query}'. Try broader or different keywords."
        return json.dumps(hits, ensure_ascii=False)


class ToolCallTool(Tool):
    """Invoke a cataloged tool by name. Arguments are validated by the registry."""

    def __init__(self, controller: ToolSearchController) -> None:
        self._ctrl = controller

    @property
    def name(self) -> str:
        return TOOL_CALL_NAME

    @property
    def description(self) -> str:
        return (
            "Invoke a tool found via tool_search by name, passing its arguments. "
            "If the arguments don't fit the tool's schema the registry returns a "
            "validation error describing the fix; adjust and call again."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Exact tool name from a tool_search result.",
                },
                "arguments": {
                    "type": "object",
                    "description": "Arguments object for the target tool.",
                },
            },
            "required": ["name"],
        }

    def blocking_for(self, params: dict[str, Any]) -> bool:
        return self._ctrl.target_is_blocking(params.get("name"))

    def metadata_owner(self, params: dict[str, Any]) -> Tool:
        return self._ctrl.resolve_target(params.get("name")).tool or self

    async def execute(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        return await self._ctrl.call(name, arguments)


class ToolSearchStrategy(TokenStrategy):
    """``before_llm_call`` hook that compacts the tool list for large catalogs.

    At or below ``compaction_threshold`` tools it passes through unchanged (and
    drops the meta-tools, so small setups are byte-for-byte as before). Above
    it, only the always-visible core + meta-tools keep their schema in the
    request; the rest stay reachable via ``tool_search`` / ``tool_call``.
    """

    def __init__(self, controller: ToolSearchController, *, compaction_threshold: int | None = None) -> None:
        self._ctrl = controller
        # The controller's threshold unless a caller overrides it, so the fold
        # and the controller's availability predicate share one source.
        self._compaction_threshold = (
            compaction_threshold if compaction_threshold is not None else controller.compaction_threshold
        )

    @property
    def name(self) -> str:
        return "tool_search"

    async def before_llm_call(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None, str]:
        if not tools:
            return messages, tools, model
        self._ctrl.refresh()
        catalog_size = sum(1 for t in tools if t["function"]["name"] not in META_TOOL_NAMES)
        if catalog_size <= self._compaction_threshold:
            out = [t for t in tools if t["function"]["name"] not in META_TOOL_NAMES]
            return messages, out, model
        present = {t["function"]["name"] for t in tools}
        if not META_TOOL_NAMES <= present:
            # Meta-tools unavailable (e.g. removed via disabled_tools): expose
            # everything rather than strand the cataloged tools behind a search
            # the model cannot invoke.
            return messages, tools, model
        visible = self._ctrl.visible_names()
        out = [t for t in tools if t["function"]["name"] in visible]
        return messages, out, model
