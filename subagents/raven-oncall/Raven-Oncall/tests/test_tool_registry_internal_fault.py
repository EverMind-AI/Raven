"""A fault in a tool is not something the caller can fix.

Measured 2026-08-17: ops_submit raised KeyError('host') after it had staged the
jobs and written them to the ledger. The registry rendered that as
"Error executing ops_submit: 'host'" followed by the generic hint to try a
different approach, and logged nothing at all.

Both halves went wrong. No traceback reached the log, so the cause was found by
reading the source rather than the record. And the arm, told to try another
approach, passed a host by hand and then edited the campaign's own declaration to
add the field, tripping the apparatus gate three times -- while the work it
believed had failed was already running.

A refusal is something to route around. A bug is not.
"""

from __future__ import annotations

import pytest

from raven.agent.tools.base import Tool
from raven.agent.tools.registry import ToolRegistry


class _Boom(Tool):
    @property
    def name(self) -> str:
        return "boom"

    @property
    def description(self) -> str:
        return "raises"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs):
        raise KeyError("host")


@pytest.fixture
def registry():
    r = ToolRegistry()
    r.register(_Boom())
    return r


async def test_the_exception_type_reaches_the_caller(registry):
    """'host' alone reads like a tool asking for a parameter, which is what the
    arm took it for."""
    out = await registry.execute("boom", {})
    assert "KeyError" in out and "boom" in out


async def test_it_says_the_fault_is_the_tools_and_not_to_work_around_it(registry):
    out = await registry.execute("boom", {})
    assert "inside the tool" in out
    assert "do not work around it" in out


async def test_it_warns_that_the_call_may_already_have_taken_effect(registry):
    """The measured case: the jobs were running and the ledger was written before
    the exception was raised."""
    out = await registry.execute("boom", {})
    assert "already have taken effect" in out


async def test_it_does_not_tell_the_caller_to_try_a_different_approach(registry):
    """Right for a refusal, wrong for a bug -- and it was the wrong one here."""
    out = await registry.execute("boom", {})
    assert "different approach" not in out


async def test_a_traceback_reaches_the_log(registry, caplog):
    from loguru import logger

    seen: list[str] = []
    sink = logger.add(lambda m: seen.append(m), level="ERROR", backtrace=True)
    try:
        await registry.execute("boom", {})
    finally:
        logger.remove(sink)
    joined = "".join(seen)
    assert "KeyError" in joined and "Traceback" in joined


@pytest.mark.asyncio
async def test_a_machine_argument_a_tool_would_ignore_is_refused():
    """An ignored ``machine`` answers about the wrong machine.

    Most unknown arguments land in **kwargs harmlessly. This one says WHERE.
    Measured 2026-08-19: read_file was given machine="conn_cpu_32c" for a path
    that also exists here and returned this computer's copy, with nothing in the
    result to say so. That run's path happened to be missing locally, so "file not
    found" made the loop correct itself -- a shared mount, or a path like /opt,
    would have handed over the wrong contents in silence.
    """
    from raven.agent.tools.filesystem import ReadFileTool
    from raven.agent.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(ReadFileTool())

    out = await registry.execute("read_file", {"path": "/etc/hosts", "machine": "conn_box"})

    assert out.startswith("Error")
    assert "has no 'machine'" in out
    assert "exec" in out and "conn_box" in out, "the refusal has to name the way to do it"
    assert "Nothing was done" in out


@pytest.mark.asyncio
async def test_a_tool_that_does_take_a_machine_is_untouched():
    from raven.agent.tools.registry import ToolRegistry
    from raven.agent.tools.shell import ExecTool

    registry = ToolRegistry()
    registry.register(ExecTool(working_dir="/tmp"))

    out = await registry.execute("exec", {"command": "echo hi"})

    assert "hi" in out
