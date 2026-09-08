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

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow.tools import base as tools_base  # noqa: E402
from oncall_flow.tools.ops_exec import waiting_note, waiting_only  # noqa: E402


@pytest.fixture(autouse=True)
def _campaign_root(tmp_path):
    tools_base.set_home(tmp_path / "ops")


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
async def test_work_that_takes_a_while_is_untouched():
    # The ceiling is about waiting, not about duration. Compiling and installing
    # still get their ten minutes.
    from raven.agent.tools.shell import ExecTool

    out = await ExecTool(working_dir="/tmp").execute(
        command="python3 -c \"import time; time.sleep(2); print('built')\"", timeout=120
    )

    assert "built" in out
    assert "ops_check_later" not in out
