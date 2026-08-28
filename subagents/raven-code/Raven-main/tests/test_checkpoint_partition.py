"""Checkpoint shadow data lives in the state partition, never in the checkout.

The work-tree is the effective working directory; the shadow git dir sits
under ``<state>/checkpoints/<sha256(workdir)[:16]>/shadow.git`` - one bucket
per checkout, so a primary binding and every worktree keep isolated
histories, and the checkout itself gains no runtime files at all.
"""

import asyncio
import subprocess
from pathlib import Path

from raven.agent.loop.checkpoint import CheckpointService


def make_ws(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "a.py").write_text("x = 1\n")
    return path


def commit_turn(svc, label="turn"):
    return asyncio.run(svc.commit_turn(label))


def test_checkpoint_records_the_effective_workdir(tmp_path):
    ws = make_ws(tmp_path / "workdir")
    state = tmp_path / "state" / "checkpoints"
    svc = CheckpointService(ws, shadow_base=state)

    cid, changed = commit_turn(svc, "first")
    assert cid and changed == ["a.py"]
    (ws / "b.py").write_text("y = 2\n")
    cid2, changed2 = commit_turn(svc, "second")
    assert cid2 and changed2 == ["b.py"]
    # The snapshot lives in the state partition, keyed off this workdir.
    assert svc._git_dir.is_relative_to(state)
    assert (svc._git_dir / "HEAD").exists()


def test_two_workdirs_get_isolated_shadow_repos(tmp_path):
    state = tmp_path / "state" / "checkpoints"
    ws1 = make_ws(tmp_path / "one")
    ws2 = make_ws(tmp_path / "two")
    s1 = CheckpointService(ws1, shadow_base=state)
    s2 = CheckpointService(ws2, shadow_base=state)
    assert s1._git_dir != s2._git_dir

    commit_turn(s1)
    (ws2 / "only-two.py").write_text("z = 3\n")
    _, changed2 = commit_turn(s2)
    assert "only-two.py" in changed2
    # ws1's shadow never saw ws2's file.
    ls = subprocess.run(["git", f"--git-dir={s1._git_dir}", "ls-files"], capture_output=True, text=True)
    assert "only-two.py" not in ls.stdout


def test_the_checkout_gains_no_runtime_files(tmp_path):
    ws = make_ws(tmp_path / "workdir")
    subprocess.run(["git", "init", "-q", str(ws)], check=True)
    state = tmp_path / "state" / "checkpoints"
    svc = CheckpointService(ws, shadow_base=state)
    commit_turn(svc)

    assert not (ws / ".raven").exists()
    assert not (ws / "NOTICE.txt").exists()
    assert not (ws / ".gitignore").exists()
    status = subprocess.run(
        ["git", "-C", str(ws), "status", "--porcelain", "--ignored=matching"],
        capture_output=True,
        text=True,
    )
    listed = [ln for ln in status.stdout.splitlines() if ln.strip() and "a.py" not in ln]
    assert listed == [], f"runtime left droppings: {listed}"


def test_rendered_config_never_enters_a_checkpoint(tmp_path):
    # The hostile layout: the work-tree IS the agent home holding a rendered
    # provider config. The default excludes must keep it out of the snapshot.
    home = make_ws(tmp_path / "agent-home")
    (home / ".config.rendered.1234.json").write_text('{"api_key": "sk-or-v1-SECRET"}')
    (home / "sessions").mkdir()
    (home / "sessions" / "s1.jsonl").write_text("{}\n")
    state = tmp_path / "elsewhere" / "checkpoints"
    svc = CheckpointService(home, shadow_base=state)
    commit_turn(svc)

    ls = subprocess.run(["git", f"--git-dir={svc._git_dir}", "ls-files"], capture_output=True, text=True)
    assert ".config.rendered" not in ls.stdout
    assert "sessions/" not in ls.stdout
    blob = subprocess.run(["git", f"--git-dir={svc._git_dir}", "log", "-p", "--all"], capture_output=True, text=True)
    assert "sk-or-v1-SECRET" not in blob.stdout


def test_shadow_base_inside_the_workspace_is_refused(tmp_path):
    ws = make_ws(tmp_path / "workdir")
    try:
        CheckpointService(ws, shadow_base=ws / "state")
    except ValueError as exc:
        assert "outside" in str(exc)
    else:
        raise AssertionError("a shadow base inside the workspace must be refused")
