"""Cross-PROCESS primary allocation race.

Real processes, one shared repo allocation lock: exactly one contender may
bind the primary checkout; every other lands in the occupied/ask-user path.
Threads cannot test this - flock is per file description and the gate's
atomicity claim is about independent processes.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from raven.agent import workspace_gate as wg

_CHILD = """
import asyncio, os, sys, time
from pathlib import Path
from raven.agent import workspace_gate as wg

session = sys.argv[1]
start_flag = Path(sys.argv[2])
repo = Path(sys.argv[3])

deadline = time.time() + 10
while not start_flag.exists():
    if time.time() > deadline:
        print("TIMEOUT"); sys.exit(2)
    time.sleep(0.002)

gate = wg.build_workspace_gate(repo)
gate.cid = lambda: session
verdict = asyncio.run(gate("write_file", {"path": "b.py"}))
print("ALLOWED" if verdict is None else "ASKED" if "authorization" in verdict else "OTHER:" + verdict[:80])
"""


def make_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def test_exactly_one_process_binds_primary(tmp_path):
    repo = tmp_path / "repo"
    make_repo(repo)
    base = tmp_path / "acp"
    repos = tmp_path / "repos"
    start_flag = tmp_path / "go"
    script = tmp_path / "contender.py"
    script.write_text(_CHILD)

    env = dict(os.environ)
    env[wg.ALLOC_BASE_ENV] = str(base)
    env[wg.REPOS_ENV] = str(repos)
    env.pop(wg.ALLOC_DIR_ENV, None)

    n = 6
    procs = [
        subprocess.Popen(
            [sys.executable, str(script), f"acp:racer-{i}", str(start_flag), str(repo)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for i in range(n)
    ]
    time.sleep(0.3)  # let every contender reach the poll loop
    start_flag.write_text("go")

    outcomes = []
    try:
        for p in procs:
            out, err = p.communicate(timeout=30)
            assert p.returncode == 0, err
            outcomes.append(out.strip())
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()

    assert outcomes.count("ALLOWED") == 1, outcomes
    assert outcomes.count("ASKED") == n - 1, outcomes

    repo_id, _ = wg.repo_identity(repo)
    owner_record = json.loads((repos / repo_id / "primary.json").read_text())
    winner = outcomes.index("ALLOWED")
    assert owner_record["instance"] == wg._identity_for_session(f"acp:racer-{winner}")
    # Every loser wrote a pending question, none of them a working binding.
    for i, outcome in enumerate(outcomes):
        record = base / "allocations" / f"{wg._safe_segment(wg._identity_for_session(f'acp:racer-{i}'))}.json"
        alloc = json.loads(record.read_text())
        if outcome == "ALLOWED":
            assert alloc["state"] == "working" and alloc["mode"] == "primary"
        else:
            assert alloc["state"] == "awaiting_user"
            assert alloc["pending"]["reason"].startswith("the workspace is held by instance")
