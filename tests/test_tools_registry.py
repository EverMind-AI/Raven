"""Tests for the ToolRegistry execute boundary (raven/agent/tools/registry.py).

``Tool.execute`` may return ``str`` or :class:`ToolResult`, but every consumer
of the registry other than the agent loop -- the sentinel action executor, the
subagent manager, the context curator, tracing -- treats the return value as
model text and puts it straight into a message, a reply or an artifact. So the
registry unwraps here and hands back a ``str`` with the display string attached.
"""

from __future__ import annotations

import json

import pytest

from raven.agent.tools.base import Tool, ToolOutput, ToolResult
from raven.agent.tools.registry import ToolRegistry


class _Split(Tool):
    def __init__(self, model_text: str, display_text: str | None) -> None:
        self._model_text = model_text
        self._display_text = display_text

    @property
    def name(self) -> str:
        return "split"

    @property
    def description(self) -> str:
        return "returns ToolResult"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(model_text=self._model_text, display_text=self._display_text)


class _Plain(Tool):
    @property
    def name(self) -> str:
        return "plain"

    @property
    def description(self) -> str:
        return "returns a bare str"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        return "plain output"


def _registry(*tools: Tool) -> ToolRegistry:
    reg = ToolRegistry()
    for tool in tools:
        reg.register(tool)
    return reg


@pytest.mark.asyncio
async def test_tool_result_is_unwrapped_to_model_text_at_the_boundary():
    reg = _registry(_Split("model sentence", "question -> answer"))

    result = await reg.execute("split", {})

    # A str, equal to the model text -- not a dataclass, and no repr anywhere.
    assert isinstance(result, str)
    assert result == "model sentence"
    assert "ToolResult(" not in f"{result}"
    assert json.loads(json.dumps({"result": result}))["result"] == "model sentence"
    # The display string rides along for the agent loop.
    assert result.display_text == "question -> answer"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_plain_string_tools_carry_no_display_text():
    reg = _registry(_Plain())

    result = await reg.execute("plain", {})

    assert result == "plain output"
    assert getattr(result, "display_text", None) is None


@pytest.mark.asyncio
async def test_error_prefixed_tool_result_keeps_hint_and_display():
    reg = _registry(_Split("Error: broker unavailable", "asked -> nothing"))

    result = await reg.execute("split", {})

    assert result.startswith("Error: broker unavailable")
    assert "try a different approach" in result
    assert result.display_text == "asked -> nothing"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_missing_tool_still_returns_a_plain_string():
    reg = _registry(_Plain())

    result = await reg.execute("nope", {})

    assert isinstance(result, str)
    assert "not found" in result


def test_tool_output_is_a_str_subclass():
    out = ToolOutput("text", "display")

    assert isinstance(out, str)
    assert out == "text"
    assert out.display_text == "display"
    # Attribute is always present, so callers can getattr without a default dance.
    assert ToolOutput("text").display_text is None
