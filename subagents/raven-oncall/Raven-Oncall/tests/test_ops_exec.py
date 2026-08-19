"""A shell pointed at the machine the work runs on.

The loop's own exec runs on the operator's computer, so every remote question --
what is in that directory, what did the solver print, how big is that file --
could only be answered by a tool someone had thought to build. Measured
2026-08-17: a case that could be read a file at a time but not listed sent one
arm guessing seven filenames and then reading raven's own state files; a 2 MB
job.dat holding the metric a task asked for was reported as unobtainable; three
turns went into ls and ssh against the local machine.

The boundary is what keeps this from becoming a second way to start work: a hard
timeout, because a solver round here takes 16 to 30 minutes and a command that
cannot outlive its call cannot become an experiment, and a refusal of the shapes
whose purpose is to detach.
"""

from __future__ import annotations

import json

import pytest

from raven.agent.tools.ops_exec import OpsExecTool, _TIMEOUT_S


class _Backend:
    def __init__(self, rc: int = 0, out: str = "ok"):
        self.rc, self.out, self.seen = rc, out, []

    def _run(self, cmd: str):
        self.seen.append(cmd)
        return self.rc, self.out


@pytest.fixture
def campaign(tmp_path, monkeypatch):
    backend = _Backend()
    meta = {"backend": "process", "remote_dir": "/work/beam", "connection": "conn_cpu"}
    monkeypatch.setattr("raven.agent.tools.ops_case_dict._campaign",
                        lambda c, l: (backend, tmp_path / "ledger.json", tmp_path, meta))
    monkeypatch.setattr("raven.ops.connections.display_name", lambda c: "my CPU box")
    return backend


async def test_it_runs_in_the_campaigns_working_directory(campaign):
    out = await OpsExecTool().execute(campaign="beam", command="ls")
    assert "cd /work/beam" in campaign.seen[0]
    assert "my CPU box" in out and "/work/beam" in out


async def test_an_explicit_cwd_wins(campaign):
    await OpsExecTool().execute(campaign="beam", command="ls", cwd="/case/arena")
    assert "cd /case/arena" in campaign.seen[0]


async def test_the_command_is_capped_and_the_cap_is_not_an_argument(campaign):
    await OpsExecTool().execute(campaign="beam", command="ls")
    assert f"timeout {_TIMEOUT_S} bash -c" in campaign.seen[0]
    assert "timeout" not in OpsExecTool().parameters["properties"]


async def test_a_nonzero_exit_is_returned_not_treated_as_a_tool_error(campaign):
    """grep says 1 when it matches nothing, and that is an answer."""
    campaign.rc, campaign.out = 1, "no match"
    out = await OpsExecTool().execute(campaign="beam", command="grep x f")
    assert "exit 1" in out and "no match" in out
    assert not out.startswith("Error")


async def test_hitting_the_cap_says_so_rather_than_looking_like_a_dead_host(campaign):
    campaign.rc, campaign.out = 124, ""
    out = await OpsExecTool().execute(campaign="beam", command="sleep 999")
    assert f"{_TIMEOUT_S}s limit" in out and "ops_submit" in out


async def test_a_missing_directory_says_nothing_was_run(campaign):
    campaign.rc, campaign.out = 66, ""
    out = await OpsExecTool().execute(campaign="beam", command="ls", cwd="/nope")
    assert "no such directory" in out and "Nothing was run" in out


@pytest.mark.parametrize("command,named", [
    ("nohup bash run.sh &", "nohup"),
    ("bash run.sh &", "&"),
    ("setsid ./solve", "setsid"),
    ("tmux new -d 'ccx -i job'", "tmux"),
    ("crontab -l", "crontab"),
])
async def test_anything_that_would_detach_is_refused_and_points_at_submit(campaign, command, named):
    out = await OpsExecTool().execute(campaign="beam", command=command)
    assert named in out and "ops_submit" in out
    assert campaign.seen == [], "a refusal must not half-run"


async def test_an_empty_command_is_not_sent(campaign):
    out = await OpsExecTool().execute(campaign="beam", command="   ")
    assert campaign.seen == [] and "No command" in out


async def test_a_long_output_is_truncated_and_says_so(campaign):
    campaign.out = "x" * 50000
    out = await OpsExecTool().execute(campaign="beam", command="cat big")
    assert "truncated" in out and len(out) < 30000
