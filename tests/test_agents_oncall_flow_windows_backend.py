"""The Windows job backend's choice of where trials live.

The submit path and the status path build the backend from different metas:
submit fills a missing ``remote_dir`` with ops_submit's POSIX default, status
reads ``meta.json``, which never got one. Both must land on the same
directory, or a trial that finished is read as one that vanished.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow.windows_backend import windows_from_meta  # noqa: E402


def _root(meta):
    return windows_from_meta(dict(meta, command="python run.py"))._remote_dir


def test_the_submit_default_and_a_missing_remote_dir_land_in_one_place():
    assert _root({"remote_dir": "/root/raven-ops"}) == _root({}) == str(Path.home() / "raven-ops")


def test_a_windows_remote_dir_is_used_as_given():
    assert _root({"remote_dir": r"E:\runs"}) == r"E:\runs"


_SUBMITTER = r"""
import asyncio, sys
sys.path.insert(0, sys.argv[1])
from oncall_flow.backend import JobSpec
from oncall_flow.windows_backend import WindowsExecutor

ex = WindowsExecutor(remote_dir=sys.argv[2], command=sys.argv[3])
asyncio.run(ex.submit(JobSpec(payload={"x": 1}, idem_key="survivor", labels={"campaign": "c1"})))
print("submitted", flush=True)
import time
time.sleep(120)
"""

_BODY = r"""
import json, time
time.sleep(4)
json.dump({"status": "succeeded", "written_by": "body", "gpu_minutes_used": 0.1}, open("result.json", "w"))
"""


@pytest.mark.skipif(sys.platform != "win32", reason="taskkill and DETACHED_PROCESS are Windows'")
def test_a_job_outlives_a_tree_kill_of_the_agent_that_submitted_it(tmp_path):
    # The host stops an agent with `taskkill /T`, which follows parent pids:
    # a job that is the agent's descendant dies with it, detached or not.
    body = tmp_path / "body.py"
    body.write_text(_BODY, encoding="utf-8")
    root = tmp_path / "ops"
    command = f'"{sys.executable}" "{body}"'
    agent = subprocess.Popen(
        [sys.executable, "-c", _SUBMITTER, str(PLUGIN_DIR), str(root), command],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert agent.stdout.readline().strip() == "submitted"
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(agent.pid)], capture_output=True, check=False)
        agent.wait(timeout=10)
        result = root / "jobs" / "survivor" / "result.json"
        deadline = time.time() + 30
        while not result.exists() and time.time() < deadline:
            time.sleep(0.2)
        assert json.loads(result.read_text(encoding="utf-8"))["written_by"] == "body"
    finally:
        if agent.poll() is None:
            agent.kill()


_ENV_BODY = r"""
import json, os
json.dump({"status": "succeeded", "devices": os.environ.get("CUDA_VISIBLE_DEVICES")}, open("result.json", "w"))
"""


@pytest.mark.parametrize("resources, seen", [({"width": 2, "device_ids": ["1", "3"]}, "1,3"), ({"width": 1}, None)])
def test_the_launcher_hands_the_job_the_devices_it_was_assigned(tmp_path, resources, seen, monkeypatch):
    # The gate picks the ids and ops_declare refuses commands that pick their
    # own, so the launcher is the only place they can reach the job from.
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    from oncall_flow.windows_backend import WindowsExecutor

    (tmp_path / "body.py").write_text(_ENV_BODY, encoding="utf-8")
    (tmp_path / ".raven-resources").write_text(json.dumps(resources), encoding="utf-8")
    launcher = WindowsExecutor(remote_dir=str(tmp_path), command="unused")._write_launcher(str(tmp_path))
    subprocess.run([sys.executable, launcher, f'"{sys.executable}" body.py'], cwd=tmp_path, check=True, timeout=60)
    assert json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))["devices"] == seen


def test_a_running_job_that_never_writes_pid_is_billed_through_the_launchers_own(tmp_path):
    # A bare command has no reason to write `pid`; the launcher always writes
    # `.raven-pid`. Ten minutes into such a run, ten minutes are spent.
    import asyncio

    from oncall_flow.windows_backend import WindowsExecutor

    job = tmp_path / "jobs" / "running"
    job.mkdir(parents=True)
    own = job / ".raven-pid"
    own.write_text(str(os.getpid()), encoding="utf-8")
    ten_minutes_ago = time.time() - 600
    os.utime(own, (ten_minutes_ago, ten_minutes_ago))

    ex = WindowsExecutor(remote_dir=str(tmp_path), command="unused", budget_minutes_total=60)
    spent = asyncio.run(ex.spent_minutes())
    assert 9.9 <= spent <= 11
    assert asyncio.run(ex.remaining_minutes()) <= 50.1
