"""A cancelled turn leaves nothing running, proved against real processes.

The unit tests assert that ``DirectExecutor`` signals a process group. What they
cannot show is the thing that matters: that a cancellation arriving at the top of
the stack -- a task cancel, which is what ``turn.cancel`` does -- reaches all the
way down through ``ToolRegistry`` and ``ExecTool`` and stops work that is already
underway.

The shape of the test is the point. The command starts a *child* and waits for it,
so the shell is not the process doing the work. ``process.kill()`` would reap the
shell and leave the child writing to the workspace for the rest of the agent's
life, and the assertion is written to fail in exactly that case: it measures a
file the child appends to, and asks whether it stops growing.

Measured both ways while this was written. With the process-group kill: 18 bytes
before the cancel, 18 after, 18 two seconds later. Without it: 18, then 28, then
47.

Marked ``integration`` because it spawns real processes and waits on wall-clock
time.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

# Long enough for the child to have written something and for its silence
# afterwards to mean silence rather than scheduling luck.
_RUN_S = 2.0
_SETTLE_S = 1.0
_WATCH_S = 2.0


def _child_script(directory: Path, marker: Path) -> Path:
    script = directory / "child.py"
    script.write_text(
        textwrap.dedent(f"""
            import time
            while True:
                with open({str(marker)!r}, "a") as handle:
                    handle.write("x")
                time.sleep(0.1)
        """)
    )
    return script


async def test_cancelling_a_tool_call_kills_the_shells_child(tmp_path):
    from raven.agent.tools.registry import ToolRegistry
    from raven.agent.tools.shell import ExecTool

    marker = tmp_path / "still_alive"
    script = _child_script(tmp_path, marker)
    registry = ToolRegistry()
    registry.register(ExecTool(working_dir=str(tmp_path), timeout=120, follow_binding=False))

    # ``& wait`` is what makes this a real test: the shell's own pid is not the
    # pid doing the work, so only a group kill reaches the python process.
    task = asyncio.create_task(registry.execute("exec", {"command": f"{sys.executable} {script} & wait"}))
    await asyncio.sleep(_RUN_S)
    written_while_running = marker.stat().st_size if marker.exists() else 0
    assert written_while_running > 0, "the child never started, so this test would pass for the wrong reason"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.sleep(_SETTLE_S)
    just_after = marker.stat().st_size
    await asyncio.sleep(_WATCH_S)
    later = marker.stat().st_size

    assert just_after == later, (
        f"the shell's child outlived the cancelled turn: {just_after} bytes then {later}. "
        "It still holds its pipes and still writes to the workspace."
    )


async def test_a_cancelled_tool_call_does_not_report_a_tool_failure(tmp_path):
    """``CancelledError`` derives from ``BaseException``, so neither
    ``ExecTool.execute``'s nor ``ToolRegistry.execute``'s ``except Exception``
    sees it -- and that is correct. Were one of them to catch it, a cancelled
    turn would be reported to the model as a failed tool call and it would try
    again, inside a turn the user just stopped."""
    from raven.agent.tools.registry import ToolRegistry
    from raven.agent.tools.shell import ExecTool

    registry = ToolRegistry()
    registry.register(ExecTool(working_dir=str(tmp_path), timeout=120, follow_binding=False))
    task = asyncio.create_task(
        registry.execute("exec", {"command": f"{sys.executable} -c 'import time; time.sleep(60)'"})
    )
    await asyncio.sleep(1.0)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled(), "a cancelled call must not come back as a result string"


async def test_a_timeout_still_reports_a_timeout_rather_than_a_cancellation(tmp_path):
    """The neighbouring path, checked because the two branches are one line apart:
    a tool that runs past its ceiling is killed the same way but has to come back
    as an error the model can read, not as a cancellation."""
    from raven.agent.tools.registry import ToolRegistry
    from raven.agent.tools.shell import ExecTool

    registry = ToolRegistry()
    registry.register(ExecTool(working_dir=str(tmp_path), timeout=1, follow_binding=False))

    result = await registry.execute("exec", {"command": f"{sys.executable} -c 'import time; time.sleep(30)'"})

    assert "Timed out" in str(result) or "timed out" in str(result)
