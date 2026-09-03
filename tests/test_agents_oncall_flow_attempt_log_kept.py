"""Running the same trial twice must not erase the first attempt's log.

A retry of an identical config against an identical case is a legitimate move --
a transient host failure is the obvious reason -- and it lands in the same job
directory, because that sameness is what idempotency is keyed on. The launcher
redirected with ``>``, so the second attempt truncated the first one's job.log.
The record of what happened the first time was gone.

Appending would be worse than truncating. Success is decided by looking in the
log for ``End``: with two attempts in one file, the first attempt's ``End`` would
make a failed retry read as a success. So the old log is moved aside and the new
attempt always writes a fresh one.

Related to the same day's fix for trial identity: that one separated "the case
changed" into its own directory, which is most of the loss. This is the half left
over -- the same case, run again.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow.backend import JobSpec  # noqa: E402
from oncall_flow.process_backend import ProcessExecutor  # noqa: E402


class FakeShell:
    def __init__(self):
        self.commands: list[str] = []

    def __call__(self, cmd):
        self.commands.append(cmd)
        if "staged" in cmd:
            return 0, "staged\n"
        return 0, "12345\n"


def _launch() -> FakeShell:
    shell = FakeShell()
    ex = ProcessExecutor(shell, remote_dir="/root/ops", command="python train.py")
    import asyncio

    asyncio.run(ex.submit(JobSpec({"lr": 1e-5}, idem_key="t1")))
    return shell


def test_an_existing_log_is_moved_aside_before_the_new_one_starts():
    launch = [c for c in _launch().commands if "nohup" in c][0]
    assert "job.log" in launch
    assert "mv" in launch or "job.log." in launch, f"the previous attempt's log must survive: {launch}"


def test_the_new_attempt_still_writes_a_fresh_job_log():
    """Not an append: success is decided by finding End in this file, and the
    previous attempt's End would make a failed retry read as a success."""
    launch = [c for c in _launch().commands if "nohup" in c][0]
    assert "> job.log" in launch and ">> job.log" not in launch


def test_the_launch_still_returns_the_pid():
    """The rotation runs before the launch and must not disturb what it prints."""
    shell = _launch()
    launch = [c for c in shell.commands if "nohup" in c][0]
    assert launch.rstrip().endswith("/pid")
