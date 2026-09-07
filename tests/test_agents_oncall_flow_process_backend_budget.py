"""The compute budget clamps down, never up.

Measured on a watch round: an agent that meant to spend thirty of its hundred
remaining minutes on one round had that written back up to a hundred, because the
backend replaced whatever the caller asked for with everything that was left. The
ceiling belongs to the experiment; the allocation below it is the agent's, and it
was the only allocation decision it had made.

These drive ``submit`` and read the config that reached the remote. A first
version computed ``min()`` in the test itself and passed with the fix reverted --
two of three tests were exercising Python's builtin rather than the backend.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

import base64  # noqa: E402
import binascii  # noqa: E402

from oncall_flow.backend import JobBackendError, JobSpec  # noqa: E402
from oncall_flow.process_backend import BUDGET_KEY, ProcessExecutor  # noqa: E402


class _Remote:
    """Enough of a host to reach the budget branch and read back what was staged.

    Synchronous, because the executor wraps it in a thread itself. The config is
    base64-encoded on the way out and the staging step is checked for the word
    "staged", so a fake that returns empty output never reaches the branch under
    test -- it fails earlier, for an unrelated reason, which is the sort of green
    that means nothing.
    """

    def __init__(self) -> None:
        self.config: dict = {}

    def __call__(self, cmd: str) -> tuple[int, str]:
        if "result.json" in cmd:
            return 0, "absent"  # nothing already running
        # Unquoted as often as quoted: shlex.quote leaves a plain base64 blob
        # alone, so a pattern that insisted on quotes matched nothing and the
        # captured config stayed empty while every assertion blamed the backend.
        for blob in re.findall(r"echo '?([A-Za-z0-9+/=]{16,})'? *\|", cmd):
            try:
                payload = json.loads(base64.b64decode(blob))
            except (ValueError, binascii.Error):
                continue
            if isinstance(payload, dict):
                self.config = payload
        if "staged" in cmd:
            return 0, "staged"
        if "cat" in cmd and "pid" in cmd:
            return 0, "4242"
        return 0, ""


def _executor(monkeypatch, *, spent: float, total: float) -> tuple[ProcessExecutor, _Remote]:
    remote = _Remote()
    ex = ProcessExecutor(remote, remote_dir="/remote", command="run {job_dir} {config}", budget_minutes_total=total)

    async def _spent() -> float:
        return spent

    monkeypatch.setattr(ex, "spent_minutes", _spent, raising=False)
    return ex, remote


@pytest.mark.asyncio
async def test_a_smaller_request_survives_to_the_remote(monkeypatch):
    """The allocation the agent made is the one that runs."""
    ex, remote = _executor(monkeypatch, spent=0.0, total=100.0)

    await ex.submit(JobSpec({BUDGET_KEY: 30.0}, idem_key="t1"))

    assert remote.config.get(BUDGET_KEY) == 30.0


@pytest.mark.asyncio
async def test_a_larger_request_is_capped_at_what_is_left(monkeypatch):
    """The ceiling belongs to the experiment."""
    ex, remote = _executor(monkeypatch, spent=80.0, total=100.0)

    await ex.submit(JobSpec({BUDGET_KEY: 500.0}, idem_key="t1"))

    assert remote.config.get(BUDGET_KEY) == 20.0


@pytest.mark.asyncio
async def test_no_request_gets_everything_that_is_left(monkeypatch):
    ex, remote = _executor(monkeypatch, spent=25.0, total=100.0)

    await ex.submit(JobSpec({"run": 1}, idem_key="t1"))

    assert remote.config.get(BUDGET_KEY) == 75.0


@pytest.mark.asyncio
async def test_an_exhausted_budget_refuses_rather_than_submitting_zero(monkeypatch):
    ex, _ = _executor(monkeypatch, spent=100.0, total=100.0)

    with pytest.raises(JobBackendError, match="exhausted"):
        await ex.submit(JobSpec({BUDGET_KEY: 10.0}, idem_key="t1"))
