"""Where a tool sits in the array, for the few that have to sit somewhere.

The array is read top to bottom like the rest of the prompt, so its order is not
cosmetic for a tool whose text another one points at: ``spawn``'s description ends
with the agent roster, and ``run_subagent_dag`` names that roster instead of
repeating it. Registration order cannot keep the two at the end -- an MCP server
connects, and a session brings its own tools, after the host has wired its own --
so the registry places them when it assembles the array.
"""

from __future__ import annotations

from typing import Any

from raven.agent.tools.registry import ToolRegistry
from raven.contracts.tool import Tool


class _Stub(Tool):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "stub"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        return "ran"


def _registry(*names: str) -> ToolRegistry:
    reg = ToolRegistry()
    for name in names:
        reg.register(_Stub(name))
    return reg


def _order(reg: ToolRegistry) -> list[str]:
    return [d["function"]["name"] for d in reg.get_definitions()]


def test_the_named_tools_close_the_array_in_the_order_given() -> None:
    reg = _registry("spawn", "read_file", "run_subagent_dag", "exec")
    reg.list_last("run_subagent_dag", "spawn")

    assert _order(reg) == ["read_file", "exec", "run_subagent_dag", "spawn"]


def test_a_tool_registered_after_them_still_comes_before_them() -> None:
    reg = _registry("read_file", "run_subagent_dag", "spawn")
    reg.list_last("run_subagent_dag", "spawn")
    reg.register(_Stub("mcp_docs_search"))

    assert _order(reg) == ["read_file", "mcp_docs_search", "run_subagent_dag", "spawn"]


def test_a_session_tool_comes_before_them_too() -> None:
    reg = _registry("read_file", "run_subagent_dag", "spawn")
    reg.list_last("run_subagent_dag", "spawn")

    with reg.session_scope({"mcp_bridged_probe": _Stub("mcp_bridged_probe")}):
        assert _order(reg) == ["read_file", "mcp_bridged_probe", "run_subagent_dag", "spawn"]


def test_one_that_is_switched_off_or_never_registered_leaves_no_gap() -> None:
    reg = _registry("read_file", "run_subagent_dag", "spawn")
    reg.list_last("run_subagent_dag", "spawn", "never_registered")
    reg.set_withheld_source(lambda: frozenset({"spawn"}))

    assert _order(reg) == ["read_file", "run_subagent_dag"]


def test_without_a_declaration_the_array_keeps_registration_order() -> None:
    assert _order(_registry("spawn", "read_file", "run_subagent_dag")) == ["spawn", "read_file", "run_subagent_dag"]
