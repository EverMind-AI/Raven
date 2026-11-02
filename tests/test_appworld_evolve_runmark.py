"""runmark wiring in the AppWorld eval seam (cold-start / confirm trials)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks.appworld.evolve import adapter, entry
from benchmarks.appworld.evolve import eval as aw_eval
from evolver.runmark import is_complete, read_mark


@pytest.fixture()
def aw(tmp_path: Path) -> adapter.AppWorldConfig:
    return adapter.AppWorldConfig(
        appworld_root=tmp_path,
        python_exe="python",
        config_path=tmp_path / "subject.json",
        out_dir_root=tmp_path / "runs",
        split="train",
    )


def _fake_batch(monkeypatch, *, calls: list[list[str]]):
    def run(argv, *, cwd=None, env=None, check=None, timeout=None):
        calls.append(list(argv))
        out_dir = Path(argv[argv.index("--out-dir") + 1])
        (out_dir / "t1_k1.json").write_text(json.dumps({"task_id": "t1", "success": True}))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(adapter, "subprocess", SimpleNamespace(run=run))


def test_run_eval_writes_marker_and_reuses_complete_batch(aw, monkeypatch):
    calls: list[list[str]] = []
    _fake_batch(monkeypatch, calls=calls)

    evals = adapter.run_eval(aw, K=1, experiment="vanilla", task_ids=["t1"])
    assert evals["t1"].passes == 1
    out_dir = aw.out_dir_root / "vanilla"
    assert is_complete(out_dir, run_id="vanilla") is True
    data = read_mark(out_dir)
    assert data is not None
    assert data["harness"] == "base"
    assert data["tasks"] == adapter._selection_digest(["t1"])
    assert len(calls) == 1

    # second call with reuse_complete=True: no subprocess, same results
    again = adapter.run_eval(aw, K=1, experiment="vanilla", task_ids=["t1"], reuse_complete=True)
    assert again["t1"].passes == 1
    assert len(calls) == 1


def test_run_eval_reruns_when_task_selection_changes_same_count(aw, monkeypatch):
    calls: list[list[str]] = []
    _fake_batch(monkeypatch, calls=calls)

    adapter.run_eval(aw, K=1, experiment="confirm", task_ids=["t1"])
    adapter.run_eval(aw, K=1, experiment="confirm", task_ids=["t2"], reuse_complete=True)
    assert len(calls) == 2


def test_run_eval_reuses_same_selection_in_different_order(aw, monkeypatch):
    calls: list[list[str]] = []
    _fake_batch(monkeypatch, calls=calls)

    adapter.run_eval(aw, K=1, experiment="confirm", task_ids=["t1", "t2"])
    adapter.run_eval(aw, K=1, experiment="confirm", task_ids=["t2", "t1"], reuse_complete=True)
    assert len(calls) == 1


def test_run_eval_reruns_when_k_changes(aw, monkeypatch):
    calls: list[list[str]] = []
    _fake_batch(monkeypatch, calls=calls)

    adapter.run_eval(aw, K=1, experiment="confirm", task_ids=["t1"])
    adapter.run_eval(aw, K=3, experiment="confirm", task_ids=["t1"], reuse_complete=True)
    assert len(calls) == 2


def test_run_eval_reruns_when_task_set_changes(aw, monkeypatch):
    calls: list[list[str]] = []
    _fake_batch(monkeypatch, calls=calls)

    adapter.run_eval(aw, K=1, experiment="confirm", task_ids=["t1"])
    adapter.run_eval(aw, K=1, experiment="confirm", task_ids=["t1", "t2"], reuse_complete=True)
    assert len(calls) == 2


def test_run_eval_reruns_when_marker_exists_but_results_are_gone(aw, monkeypatch):
    calls: list[list[str]] = []
    _fake_batch(monkeypatch, calls=calls)

    adapter.run_eval(aw, K=1, experiment="vanilla", task_ids=["t1"])
    (aw.out_dir_root / "vanilla" / "t1_k1.json").unlink()
    adapter.run_eval(aw, K=1, experiment="vanilla", task_ids=["t1"], reuse_complete=True)
    assert len(calls) == 2


def test_run_eval_reruns_when_harness_changes(aw, monkeypatch, tmp_path: Path):
    calls: list[list[str]] = []
    _fake_batch(monkeypatch, calls=calls)

    harness = tmp_path / "wt"
    harness.mkdir()
    (harness / "harness.py").write_text("x = 1\n")

    adapter.run_eval(aw, K=1, experiment="confirm", task_ids=["t1"], cwd=harness)
    assert len(calls) == 1

    # same harness tree: reuse
    adapter.run_eval(aw, K=1, experiment="confirm", task_ids=["t1"], cwd=harness, reuse_complete=True)
    assert len(calls) == 1

    # different harness tree: run again
    (harness / "harness.py").write_text("x = 2\n")
    adapter.run_eval(aw, K=1, experiment="confirm", task_ids=["t1"], cwd=harness, reuse_complete=True)
    assert len(calls) == 2


class _Worktree:
    def __init__(self, path: Path):
        self.path = path

    def __enter__(self) -> Path:
        return self.path

    def __exit__(self, *exc) -> bool:
        return False


def test_cold_start_eval_runs_in_the_base_worktree_with_reuse(aw, monkeypatch, tmp_path: Path):
    seen: dict = {}
    wt = tmp_path / "base-wt"

    def fake_run_eval(cfg, **kwargs):
        seen.update(kwargs)
        return {}

    monkeypatch.setattr(entry.git_ops, "worktree_at", lambda root, sha: _Worktree(wt))
    monkeypatch.setattr(entry.aw_adapter, "run_eval", fake_run_eval)

    base_eval = entry._make_cold_start_eval(aw, repo_root=tmp_path, base_sha="deadbeef")
    base_eval(None, ["t1"], 3, "vanilla")

    assert seen["experiment"] == "vanilla"
    assert seen["task_ids"] == ["t1"]
    assert seen["K"] == 3
    assert seen["cwd"] == wt
    assert seen["reuse_complete"] is True


def test_candidate_eval_runs_in_the_candidate_worktree_with_reuse(aw, monkeypatch, tmp_path: Path):
    seen: dict = {}
    wt = tmp_path / "candidate-wt"

    def fake_run_eval(cfg, **kwargs):
        seen.update(kwargs)
        return {}

    monkeypatch.setattr(aw_eval.git_ops, "worktree_at", lambda root, sha: _Worktree(wt))
    monkeypatch.setattr(aw_eval.aw_adapter, "run_eval", fake_run_eval)

    eval_fn = aw_eval.make_appworld_eval_fn(aw, tmp_path)
    node = SimpleNamespace(git_commit_sha="deadbeef")
    eval_fn(node, ["t1"], 3, "confirm")

    assert seen["experiment"] == "confirm"
    assert seen["task_ids"] == ["t1"]
    assert seen["K"] == 3
    assert seen["cwd"] == wt
    assert seen["reuse_complete"] is True
