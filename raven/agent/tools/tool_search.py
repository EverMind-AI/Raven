"""Progressive tool disclosure for large catalogs.

When built-ins + plugins + MCP push the tool count past a threshold, injecting
every schema into each request burns context that scales with tool count. This
module withholds most schemas and exposes three meta-tools instead:

  - ``tool_search``   — BM25 keyword search over the hidden catalog; compact
                        hits (name + description, no parameters).
  - ``tool_describe`` — one tool's full parameter schema (so the model knows
                        the argument shape before calling it).
  - ``tool_call``     — invoke a cataloged tool by name; forwards through the
                        registry, which still validates arguments.

The tool list sent to the model never changes turn-to-turn (always the core
set + these three meta-tools), so the prompt cache stays stable across the
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
from typing import TYPE_CHECKING, Any

from raven.agent.tools.base import Tool
from raven.agent.tools.tool_index import ToolIndex
from raven.token_wise.base import TokenStrategy

if TYPE_CHECKING:
    from raven.agent.tools.registry import ToolRegistry

# Core tools kept exposed every turn — the agent would be crippled having to
# search for these. Config ``tools.tool_search.always_visible`` extends this.
DEFAULT_ALWAYS_VISIBLE: tuple[str, ...] = (
    "read_file",
    "write_file",
    "edit_file",
    "list_dir",
    "grep",
    "find",
    "exec",
    "message",
)

TOOL_CALL_NAME: str = "tool_call"
# The meta-tools: always registered when the feature is on, never cataloged.
META_TOOL_NAMES: frozenset[str] = frozenset({"tool_search", "tool_describe", TOOL_CALL_NAME})


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
    ) -> None:
        self._registry = registry
        self.always_visible = set(always_visible) | META_TOOL_NAMES
        self.search_result_limit = search_result_limit
        self._index = ToolIndex()

    def _catalog_tools(self) -> list[Tool]:
        """All registered tools except the meta-tools (never self-searchable)."""
        out = []
        for name in self._registry.tool_names:
            if name in META_TOOL_NAMES:
                continue
            tool = self._registry.get(name)
            if tool is not None:
                out.append(tool)
        return out

    def refresh(self) -> None:
        """Sync the BM25 index with the current registry (no-op if unchanged)."""
        self._index.ensure(self._catalog_tools())

    def visible_names(self) -> set[str]:
        return self.always_visible

    def search(self, query: str, limit: int | None = None) -> list[dict[str, Any]]:
        """Compact hits: name + description, never parameters."""
        names = self._index.search(query, limit or self.search_result_limit)
        hits = []
        for name in names:
            tool = self._registry.get(name)
            if tool is None:
                continue
            hits.append({"name": name, "description": tool.description})
        return hits

    def describe(self, name: str) -> dict[str, Any] | None:
        """Return the tool's full function schema (for argument shape)."""
        tool = self._registry.get(name)
        if tool is None or name in META_TOOL_NAMES:
            return None
        return tool.to_schema()["function"]

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
        if name in META_TOOL_NAMES:
            return f"Error: '{name}' cannot be invoked via tool_call."
        if not self._registry.has(name):
            return f"Error: tool '{name}' not found. Use tool_search to find it."
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
            "currently loaded. Returns matching tool names with a short "
            "description (no parameters). Use tool_describe to see a tool's "
            "arguments, then tool_call to invoke it. Query with task keywords, "
            "e.g. 'create github issue' or '生成图片'."
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
                    "maximum": 25,
                },
            },
            "required": ["query"],
        }

    async def execute(self, query: str, limit: int | None = None) -> str:
        hits = self._ctrl.search(query, limit)
        if not hits:
            return f"No tools matched '{query}'. Try broader or different keywords."
        return json.dumps(hits, ensure_ascii=False)


class ToolDescribeTool(Tool):
    """Return a cataloged tool's full parameter schema."""

    def __init__(self, controller: ToolSearchController) -> None:
        self._ctrl = controller

    @property
    def name(self) -> str:
        return "tool_describe"

    @property
    def description(self) -> str:
        return (
            "Show the full parameter schema of a tool found via tool_search, so "
            "you know its arguments. Invoke the tool afterwards with tool_call."
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
            },
            "required": ["name"],
        }

    async def execute(self, name: str) -> str:
        schema = self._ctrl.describe(name)
        if schema is None:
            return f"Tool '{name}' not found. Use tool_search to find available tools."
        return json.dumps(schema, ensure_ascii=False)


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
            "Use tool_describe first to learn the argument shape."
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

    async def execute(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        return await self._ctrl.call(name, arguments)


class ToolSearchStrategy(TokenStrategy):
    """``before_llm_call`` hook that compacts the tool list for large catalogs.

    At or below ``compaction_threshold`` tools it passes through unchanged (and
    drops the meta-tools, so small setups are byte-for-byte as before). Above
    it, only the always-visible core + meta-tools keep their schema in the
    request; the rest stay reachable via ``tool_search`` / ``tool_call``.
    """

    def __init__(self, controller: ToolSearchController, *, compaction_threshold: int = 50) -> None:
        self._ctrl = controller
        self._compaction_threshold = compaction_threshold

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
