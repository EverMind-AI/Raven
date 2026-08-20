"""A wait long enough to matter is a wake, whichever path it takes.

Measured 2026-08-19, one session: twelve waits written as shell commands, six on
a machine and six without one, the longest 290 seconds, against three uses of the
tool that exists for it. Four of the machine-path waits came back "killed at the
60s limit -- anything that takes longer is a job", and the identical wait was then
made without a machine, where nothing applied.

Three ceilings for the same act is what made that work: a minute on a remote
machine, ten on a local one (the cap rode on a `timeout` wrapper the local path
skips), and ten more for a plain call. None of this refuses a wait; it stops one
path being a better place to do it than another.
"""

from __future__ import annotations

import time

import pytest

from raven.agent.tools.ops_exec import _TIMEOUT_S, waiting_note, waiting_only


def test_only_a_leading_wait_counts():
    assert waiting_only("sleep 290 && echo check") == 290
    assert waiting_only("  sleep 60") == 60
    assert waiting_only("sleep 90 && tail -3 progress.jsonl") == 90

    assert waiting_only("build && sleep 2 && check") == 0, "that waits for a reason"
    assert waiting_only("sleep 2 && echo ok") == 0, "settling a filesystem is not a wake"
    assert waiting_only("python3 train.py") == 0
    assert waiting_only("sleepy_script.sh") == 0, "a name that starts with sleep is not sleep"
    assert waiting_only("") == 0


def test_the_note_names_the_tool_and_carries_the_time_asked_for():
    note = waiting_note(290, 60)

    assert "ops_check_later(eta_seconds=290" in note, (
        "the loop asked for 290s; handing that straight to the wake is the whole point"
    )
    for cost in ("window", "restart", "context"):
        assert cost in note, "a clipped wait has to say what waiting in a call costs"


@pytest.mark.asyncio
async def test_a_plain_wait_is_clipped_to_the_look_ceiling(monkeypatch):
    from raven.agent.tools.shell import ExecTool

    tool = ExecTool(working_dir="/tmp", timeout=600)
    started = time.monotonic()
    out = await tool.execute(command=f"sleep {_TIMEOUT_S + 30} && echo check", timeout=600)
    elapsed = time.monotonic() - started

    assert elapsed < _TIMEOUT_S + 15, f"waited {elapsed:.0f}s; the ceiling did not apply"
    assert "ops_check_later" in out


@pytest.mark.asyncio
async def test_work_that_takes_a_while_is_untouched():
    # The ceiling is about waiting, not about duration. Compiling and installing
    # still get their ten minutes.
    from raven.agent.tools.shell import ExecTool

    out = await ExecTool(working_dir="/tmp").execute(
        command="python3 -c \"import time; time.sleep(2); print('built')\"", timeout=120)

    assert "built" in out
    assert "ops_check_later" not in out


def test_a_local_machine_gets_the_same_ceiling_as_a_remote_one():
    """The cap for a remote look rides on a `timeout` wrapper the local path skips.

    Without passing it to the runner, a look at a machine that happens to be this
    computer had ten minutes where a look at any other machine had one.
    """
    from raven.ops.transport import make_local_runner, runner_from

    runner = runner_from({"transport": "local"}, cap_seconds=_TIMEOUT_S)
    started = time.monotonic()
    rc, _ = runner(f"sleep {_TIMEOUT_S + 20}")
    elapsed = time.monotonic() - started

    assert rc == 124 and elapsed < _TIMEOUT_S + 10, f"rc={rc} after {elapsed:.0f}s"
    assert callable(make_local_runner())
