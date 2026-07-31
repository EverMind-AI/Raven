"""Cross-tool did-you-mean suggestions on invalid parameters.

A model that confuses similarly named tools (exec_read called with read_file's
path/offset/limit) must get pointed at the tool its parameters actually fit,
instead of rediscovering the split by trial and error.
"""

import pytest

from raven.agent.tools.base import Tool
from raven.agent.tools.registry import ToolRegistry


class _StubTool(Tool):
    def __init__(self, name: str, properties: dict, required: list[str]):
        self._name = name
        self._properties = properties
        self._required = required

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"stub {self._name}"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": self._properties, "required": self._required}

    async def execute(self, **kwargs) -> str:
        return "ok"


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        _StubTool(
            "read_file",
            {
                "path": {"type": "string"},
                "offset": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            ["path"],
        )
    )
    reg.register(
        _StubTool(
            "exec_read",
            {
                "session": {"type": "string"},
                "timeout": {"type": "integer"},
                "close": {"type": "boolean"},
            },
            ["session"],
        )
    )
    return reg


@pytest.mark.asyncio
async def test_misrouted_params_suggest_the_matching_tool():
    reg = _registry()
    result = await reg.execute("exec_read", {"path": "/etc/hosts", "offset": 85, "limit": 10})
    assert "missing required session" in result
    assert "did you mean" in result
    assert "read_file" in result


@pytest.mark.asyncio
async def test_plain_invalid_params_get_no_false_suggestion():
    reg = _registry()
    result = await reg.execute("exec_read", {"bogus": "x"})
    assert "Invalid parameters" in result
    assert "did you mean" not in result


@pytest.mark.asyncio
async def test_valid_params_execute_without_suggestion():
    reg = _registry()
    assert await reg.execute("exec_read", {"session": "s1"}) == "ok"


@pytest.mark.asyncio
async def test_mangled_tool_name_is_repaired_and_executed():
    reg = _registry()
    result = await reg.execute("Read_File", {"path": "/etc/hosts"})
    assert "resolved to 'read_file'" in result
    assert result.endswith("ok")


@pytest.mark.asyncio
async def test_unknown_tool_name_still_lists_available():
    reg = _registry()
    result = await reg.execute("fetch_url", {})
    assert "not found" in result
    assert "read_file" in result


@pytest.mark.asyncio
async def test_validation_error_carries_no_generic_hint():
    """Targeted guidance (did-you-mean) replaces the generic change-approach
    suffix; stacking both buries the actionable part."""
    reg = _registry()
    result = await reg.execute("exec_read", {"path": "/etc/hosts"})
    assert "did you mean" in result
    assert "Analyze the error above" not in result
