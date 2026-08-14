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
from raven.providers.base import RunMeta, TruncationInfo


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


# ---------------------------------------------------------------------------
# Truncated arguments are reported as truncation, not as a missing field
# ---------------------------------------------------------------------------


class _NeedsPath(Tool):
    """A tool with one required parameter, plus a record of what it received."""

    def __init__(self) -> None:
        self.received: dict | None = None

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return "writes a file"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        }

    async def execute(self, **kwargs) -> str:
        self.received = kwargs
        return "written"


@pytest.mark.asyncio
async def test_truncated_arguments_reported_as_truncation() -> None:
    """A cut-off call names the real cause instead of the missing field.

    The schema is right that something is missing; it is wrong about why.
    Telling a model that wrote a path it forgot the path sends it looking for a
    mistake it did not make, and its most reasonable next move is to resend the
    same oversized payload.
    """
    reg = ToolRegistry()
    reg.register(_NeedsPath())

    out = await reg.execute(
        "write_file",
        {"_raw_arguments": '{"content": "def foo('},
        run_meta=RunMeta(truncation=TruncationInfo(at_tokens=4096)),
    )

    assert "[truncated]" in out
    assert "4096 tokens" in out
    assert "missing required" not in out
    # The generic hint would still point at "try a different approach", which is
    # the advice that produced the retry loop.
    assert "different approach" not in out


@pytest.mark.asyncio
async def test_truncated_but_parseable_arguments_also_reported_as_truncation() -> None:
    """Covers the shape where the transport closed the JSON for us.

    The blob parses cleanly and simply lacks whatever the model had not reached
    yet, so validation fails on a genuinely absent field -- indistinguishable
    from a model error unless the truncation flag is honoured.
    """
    reg = ToolRegistry()
    reg.register(_NeedsPath())

    out = await reg.execute(
        "write_file",
        {"content": "def foo(): ..."},
        run_meta=RunMeta(truncation=TruncationInfo(at_tokens=8192)),
    )

    assert "[truncated]" in out
    assert "missing required" not in out


@pytest.mark.asyncio
async def test_untruncated_invalid_arguments_keep_the_schema_message() -> None:
    """A genuine model mistake must still read as one."""
    reg = ToolRegistry()
    reg.register(_NeedsPath())

    out = await reg.execute("write_file", {"content": "hello"})

    assert "Invalid parameters" in out
    assert "[truncated]" not in out
    assert "different approach" in out


@pytest.mark.asyncio
async def test_truncation_note_never_reaches_the_tool() -> None:
    """The note travels beside the arguments, never inside them.

    Note what this pins down as a side effect: when the arguments validate,
    the tool runs even though the turn was truncated. That is the remaining
    gap -- a call whose fields are all present but whose last string value was
    cut mid-word passes validation and executes, writing a half-file and
    reporting success. Closing it means refusing the call before dispatch,
    which is a control-flow change and deliberately out of scope here.
    """
    tool = _NeedsPath()
    reg = ToolRegistry()
    reg.register(tool)

    await reg.execute(
        "write_file",
        {"path": "a.py", "content": "x"},
        run_meta=RunMeta(truncation=TruncationInfo(at_tokens=4096)),
    )

    assert tool.received == {"path": "a.py", "content": "x"}


# ---------------------------------------------------------------------------
# The advice on a truncated call comes from the tool, not from one template
# ---------------------------------------------------------------------------


class _Appendable(_NeedsPath):
    """A tool with a smaller form to fall back to."""

    @property
    def truncation_hint(self) -> str:
        return "Send a smaller first chunk with mode=overwrite, then append the rest with mode=append."


@pytest.mark.asyncio
async def test_a_tools_own_recovery_advice_reaches_the_model() -> None:
    """Observed live: the generic "send it in smaller pieces" produced thirty-two
    iterations of the same call with the content field left out. The model had
    worked out that it should split the file, and still did not use the append
    mode -- it is declared in the tool schema, which a model in a retry loop
    never re-reads. The error message is the one text it does read every turn.
    """
    reg = ToolRegistry()
    reg.register(_Appendable())

    out = await reg.execute(
        "write_file",
        {"path": "snake.py"},
        run_meta=RunMeta(truncation=TruncationInfo(at_tokens=1200)),
    )

    assert "[truncated]" in out
    assert "mode=append" in out


@pytest.mark.asyncio
async def test_a_tool_without_advice_still_gets_the_neutral_message() -> None:
    """And nothing more: a generic "split it up" was wrong for tools like exec,
    whose argument is one command and has no smaller form.
    """
    reg = ToolRegistry()
    reg.register(_NeedsPath())

    out = await reg.execute(
        "write_file",
        {"path": "snake.py"},
        run_meta=RunMeta(truncation=TruncationInfo(at_tokens=1200)),
    )

    assert "[truncated]" in out
    assert "was not saved" in out
    # No instruction at all: the two facts stand, and nothing tells this tool
    # to do something it may have no way of doing.
    assert "smaller pieces" not in out
    assert out.rstrip().endswith("Everything past that point was not saved.")


def test_shipped_tools_that_can_be_split_say_how() -> None:
    """write_file and exec are the two the incident actually cycled through."""
    from raven.agent.tools.filesystem import WriteFileTool
    from raven.agent.tools.shell import ExecTool

    assert "mode=append" in (WriteFileTool(".").truncation_hint or "")
    exec_hint = ExecTool().truncation_hint or ""
    assert "shorter command" in exec_hint


@pytest.mark.asyncio
async def test_the_message_does_not_tell_the_model_to_withhold_the_content() -> None:
    """It used to say "do not resend the same content", and that was wrong.

    Nothing past the cut is saved: the upstream drops the unfinished key, so
    the call that reaches the tool carries only the fields that completed, and
    the conversation history stores that same stub. The content genuinely has
    to come again. Observed live: the model followed the old sentence exactly
    -- forty turns of `write_file({"path": ...})` with the content withheld.
    """
    reg = ToolRegistry()
    reg.register(_NeedsPath())

    out = await reg.execute(
        "write_file",
        {"path": "snake.py"},
        run_meta=RunMeta(truncation=TruncationInfo(at_tokens=1200)),
    )

    assert "was not saved" in out
    assert "not resend" not in out.lower()
