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
    def __init__(
        self,
        model_text: str,
        display_text: str | None,
        *,
        retryable: bool = True,
        abort_action: bool = False,
    ) -> None:
        self._model_text = model_text
        self._display_text = display_text
        self._retryable = retryable
        self._abort_action = abort_action

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
        return ToolResult(
            model_text=self._model_text,
            display_text=self._display_text,
            retryable=self._retryable,
            abort_action=self._abort_action,
        )


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
async def test_non_retryable_error_omits_hint_and_preserves_abort_signal():
    reg = _registry(
        _Split(
            "Error: denied by safety policy",
            None,
            retryable=False,
            abort_action=True,
        )
    )

    result = await reg.execute("split", {})

    assert "try a different approach" not in result
    assert result.retryable is False  # type: ignore[attr-defined]
    assert result.abort_action is True  # type: ignore[attr-defined]


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


class _NeedsPath(Tool):
    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return "needs path and content"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        }

    async def execute(self, **kwargs) -> str:
        return "wrote it"


class TestUnparsableArguments:
    """A call whose arguments were not JSON must be told so.

    The loop parks the raw text under ``_raw_arguments`` when ``json.loads``
    fails. Reported through schema validation that reads as "missing required
    path" -- and a caller told it forgot a field it did send re-sends the same
    malformed JSON forever. One observed session burned eighteen tool calls in
    this loop and the model ended up reasoning about ``_raw_arguments``, an
    internal key it only ever saw because we invented it.
    """

    @pytest.mark.asyncio
    async def test_the_error_names_the_parse_failure_not_a_missing_field(self):
        reg = _registry(_NeedsPath())

        result = await reg.execute("write_file", {"_raw_arguments": '{"path": "a.py", "content": "x'})

        assert "not valid JSON" in result
        assert "missing required" not in result
        # The raw text comes back so the caller can see what it actually sent.
        assert '{"path": "a.py"' in result
        # And the internal key never appears in what the caller is asked to fix.
        assert "_raw_arguments" not in result

    @pytest.mark.asyncio
    async def test_a_genuinely_missing_field_still_reports_as_missing(self):
        reg = _registry(_NeedsPath())

        result = await reg.execute("write_file", {"path": "a.py"})

        assert "missing required content" in result

    @pytest.mark.asyncio
    async def test_a_long_argument_blob_is_capped(self):
        reg = _registry(_NeedsPath())

        result = await reg.execute("write_file", {"_raw_arguments": "x" * 5000})

        assert result.endswith("...")
        assert len(result) < 1000
