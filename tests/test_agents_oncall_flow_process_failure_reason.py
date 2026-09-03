"""Why a process-backend job failed, and whether anyone can know.

The openfoam backend already answers this properly: it says how the failure was
detected and then appends what the job last printed, leaving which line explains
the crash to whoever has the domain knowledge. The process backend, which every
ML campaign runs on, returned the bare log tail with no statement at all.

That matters because the tail can look like anything. Measured 2026-08-14: a
trial killed from outside left the progress bars of the previous checkpoint write
as its last thirty lines -- no traceback, no exception, nothing saying "failure".
The loop read the bars as the crash site and named a code fault that did not
exist.

The discriminator is free and already in the same branch. A ``result.json`` means
the script reached its own exit path and could label what went wrong -- that is
where ``error_kind: cuda_oom`` comes from. Its absence means the job was ended
from outside, so the reason is, by definition, not in the artifacts. The code
read that distinction and then dropped it.

Pointed out by the window running the ML on-call tasks, which deliberately left
it rather than reaching into this lane.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow.backend import JobHandle, JobStatus  # noqa: E402
from oncall_flow.process_backend import ProcessExecutor  # noqa: E402

GONE = (0, "gone\n")


class FakeShell:
    def __init__(self, replies):
        self._replies = list(replies)
        self.commands = []

    def __call__(self, cmd):
        self.commands.append(cmd)
        return self._replies.pop(0) if self._replies else (0, "")


def _executor(replies):
    return ProcessExecutor(FakeShell(replies), remote_dir="/root/ops", command="python train.py")


PROGRESS_TAIL = (
    "Loading weights: 100%|##########| 310/310 [00:00<00:00, 423it/s]\n"
    "Writing model shards: 100%|##########| 2/2 [00:03<00:00,  1.6s/it]\n"
)


@pytest.mark.asyncio
async def test_a_job_that_left_no_result_is_said_to_be_unexplained():
    """No result.json means it was ended from outside, and the harness knows that
    for certain -- it should not be left to the reader to infer from a tail that
    happens to look like anything at all."""
    ex = _executor([(1, ""), GONE, (0, PROGRESS_TAIL)])
    r = await ex.fetch_result(JobHandle("process", "ops-t1"))

    assert r.status is JobStatus.FAILED
    err = r.error or ""
    assert "outside" in err or "ended" in err, "the harness states what it knows"
    assert "not in" in err and ("artifact" in err or "output" in err), (
        "and that the cause is not in what the job left behind"
    )
    assert "Writing model shards" in err, "the last lines still come along, verbatim"


@pytest.mark.asyncio
async def test_the_tail_is_never_offered_as_the_cause():
    """The failure this exists to stop: progress bars read as a crash site."""
    ex = _executor([(1, ""), GONE, (0, PROGRESS_TAIL)])
    r = await ex.fetch_result(JobHandle("process", "ops-t1"))
    head = (r.error or "").split("\n")[0]
    assert "Loading weights" not in head, "the first thing read must be the verdict"


@pytest.mark.asyncio
async def test_a_job_with_no_log_either_says_so():
    ex = _executor([(1, ""), GONE, (0, "")])
    r = await ex.fetch_result(JobHandle("process", "ops-t1"))
    assert "no log" in (r.error or "").lower()
    assert "outside" in (r.error or "") or "ended" in (r.error or "")


@pytest.mark.asyncio
async def test_a_job_that_reached_its_own_exit_path_keeps_its_own_words():
    """result.json means the script labelled the failure itself; that label is
    better than anything this layer could say, so nothing is added."""
    ex = _executor(
        [
            (
                0,
                '{"status": "failed", "error_kind": "cuda_oom", '
                '"error": "CUDA out of memory. Tried to allocate 256.00 MiB"}',
            )
        ]
    )
    r = await ex.fetch_result(JobHandle("process", "ops-t1"))
    assert "CUDA out of memory" in (r.error or "")
    assert "ended from outside" not in (r.error or "")
