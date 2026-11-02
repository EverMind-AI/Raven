"""runmark wiring in the AppWorld eval seam (cold-start / confirm trials)."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks.appworld.evolve import adapter, entry
from benchmarks.appworld.evolve import eval as aw_eval
from evolver import runmark
from evolver.orchestrator.gates import policy
from evolver.runmark import is_complete, read_mark


@pytest.fixture()
def aw(tmp_path: Path) -> adapter.AppWorldConfig:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "harness.py").write_text("x = 1\n")
    return adapter.AppWorldConfig(
        appworld_root=repo,
        python_exe="python",
        config_path=tmp_path / "subject.json",
        out_dir_root=tmp_path / "runs",
        split="train",
        n=1,
    )


def _parse(path: Path) -> dict | None:
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _requested_tasks(argv: list[str]) -> list[str]:
    if "--tasklist" not in argv:
        return []
    path = Path(argv[argv.index("--tasklist") + 1])
    return [line.strip() for line in path.read_text().splitlines() if line.strip()]


def _fake_batch(monkeypatch, *, calls: list[list[str]], trials: list[tuple[str, int]] | None = None):
    """A stand-in for ``batch.py`` that keeps its trial-level resume rule.

    It writes the same ``{task}_k{k}.json`` layout the real runner uses (K is
    0-based) and skips a trial whose file already parses, so a test can tell a
    real re-measurement from a resumed one.
    """

    def run(argv, *, cwd=None, env=None, check=None, timeout=None):
        calls.append(list(argv))
        out_dir = Path(argv[argv.index("--out-dir") + 1])
        k = int(argv[argv.index("--k") + 1])
        if cwd is not None:
            assert Path(cwd).is_dir()
        for task_id in _requested_tasks(argv):
            for attempt in range(k):
                path = out_dir / f"{task_id}_k{attempt}.json"
                if _parse(path) is not None:
                    continue
                if trials is not None:
                    trials.append((task_id, attempt))
                path.write_text(json.dumps({"task_id": task_id, "success": True}))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(adapter, "subprocess", SimpleNamespace(run=run))


def test_run_eval_writes_marker_and_reuses_complete_batch(aw, monkeypatch):
    calls: list[list[str]] = []
    _fake_batch(monkeypatch, calls=calls)

    evals = adapter.run_eval(aw, K=1, experiment="vanilla", task_ids=["t1"])
    assert evals["t1"].passes == 1
    out_dir = aw.out_dir_root / "vanilla"
    assert is_complete(out_dir, run_id="vanilla", harness_dir=aw.appworld_root) is True
    data = read_mark(out_dir)
    assert data is not None
    assert data["harness"] == runmark.fingerprint(aw.appworld_root)
    assert data["harness"] != "base"
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


def test_a_different_selection_is_not_reused_even_when_its_files_exist(aw, monkeypatch):
    calls: list[list[str]] = []
    trials: list[tuple[str, int]] = []
    _fake_batch(monkeypatch, calls=calls, trials=trials)

    adapter.run_eval(aw, K=1, experiment="confirm", task_ids=["t1", "t2"])
    got = adapter.run_eval(aw, K=1, experiment="confirm", task_ids=["t2"], reuse_complete=True)
    assert set(got) == {"t2"}
    assert len(calls) == 2
    # t2 was measured again, not read back from the old batch that also held it
    assert trials.count(("t2", 0)) == 2


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
    (aw.out_dir_root / "vanilla" / "t1_k0.json").unlink()
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


def test_a_partial_result_set_is_not_reused_and_the_base_dir_is_refilled(aw, monkeypatch):
    calls: list[list[str]] = []
    trials: list[tuple[str, int]] = []
    _fake_batch(monkeypatch, calls=calls, trials=trials)

    adapter.run_eval(aw, K=1, experiment="vanilla", task_ids=["t1", "t2"])
    out_dir = aw.out_dir_root / "vanilla"
    (out_dir / "t2_k0.json").unlink()

    got = adapter.run_eval(aw, K=1, experiment="vanilla", task_ids=["t1", "t2"], reuse_complete=True)
    assert set(got) == {"t1", "t2"}
    assert len(calls) == 2
    # only the missing trial was re-run, and the base dir is whole again:
    # cold_start_done() counts exactly these files, so the launcher can finish.
    assert trials.count(("t2", 0)) == 2 and trials.count(("t1", 0)) == 1
    assert all((out_dir / f"{tid}_k0.json").is_file() for tid in ("t1", "t2"))


def test_a_corrupt_result_set_is_not_reused_and_is_remeasured(aw, monkeypatch):
    calls: list[list[str]] = []
    trials: list[tuple[str, int]] = []
    _fake_batch(monkeypatch, calls=calls, trials=trials)

    adapter.run_eval(aw, K=1, experiment="vanilla", task_ids=["t1"])
    out_dir = aw.out_dir_root / "vanilla"
    (out_dir / "t1_k0.json").write_text("{not json")

    got = adapter.run_eval(aw, K=1, experiment="vanilla", task_ids=["t1"], reuse_complete=True)
    assert set(got) == {"t1"}
    assert len(calls) == 2
    assert trials.count(("t1", 0)) == 2
    assert _parse(out_dir / "t1_k0.json") is not None


def test_a_whole_split_selection_counts_against_n(tmp_path: Path):
    out_dir = tmp_path / "vanilla"
    out_dir.mkdir()
    assert adapter._results_complete(out_dir, None, 1, 2) is False
    (out_dir / "a_k0.json").write_text(json.dumps({"task_id": "a", "success": True}))
    assert adapter._results_complete(out_dir, None, 1, 2) is False
    (out_dir / "b_k0.json").write_text(json.dumps({"task_id": "b", "success": True}))
    assert adapter._results_complete(out_dir, None, 1, 2) is True
    (out_dir / "b_k0.json").write_text("{broken")
    assert adapter._results_complete(out_dir, None, 1, 2) is False


def test_a_reused_run_returns_only_the_requested_tasks(aw, monkeypatch):
    calls: list[list[str]] = []
    _fake_batch(monkeypatch, calls=calls)

    adapter.run_eval(aw, K=1, experiment="confirm", task_ids=["t1"])
    # a left-over from another selection in the same out-dir
    (aw.out_dir_root / "confirm" / "t2_k0.json").write_text(json.dumps({"task_id": "t2", "success": True}))

    got = adapter.run_eval(aw, K=1, experiment="confirm", task_ids=["t1"], reuse_complete=True)
    assert set(got) == {"t1"}
    assert len(calls) == 1


def test_a_fresh_run_returns_only_the_requested_tasks(aw, monkeypatch):
    calls: list[list[str]] = []
    _fake_batch(monkeypatch, calls=calls)

    adapter.run_eval(aw, K=1, experiment="confirm", task_ids=["t1"])
    got = adapter.run_eval(aw, K=1, experiment="confirm", task_ids=["t2"], reuse_complete=True)
    assert set(got) == {"t2"}


def test_a_stale_result_that_cannot_be_cleared_is_refused(aw, monkeypatch):
    calls: list[list[str]] = []
    _fake_batch(monkeypatch, calls=calls)

    adapter.run_eval(aw, K=1, experiment="vanilla", task_ids=["t1"])
    out_dir = aw.out_dir_root / "vanilla"
    (out_dir / "t1_k0.json").unlink()
    (out_dir / "t1_k0.json").mkdir()

    with pytest.raises(OSError):
        adapter.run_eval(aw, K=1, experiment="vanilla", task_ids=["t2"], reuse_complete=True)
    assert len(calls) == 1, "the batch must not run on an out-dir it could not clean"


def test_run_eval_reruns_when_the_config_file_content_changes(aw, monkeypatch):
    calls: list[list[str]] = []
    trials: list[tuple[str, int]] = []
    _fake_batch(monkeypatch, calls=calls, trials=trials)
    aw.config_path.write_text('{"model": "a"}')

    adapter.run_eval(aw, K=1, experiment="vanilla", task_ids=["t1"])
    data = read_mark(aw.out_dir_root / "vanilla")
    assert data is not None
    assert data["config"] == adapter._content_digest(aw.config_path)

    aw.config_path.write_text('{"model": "b"}')
    adapter.run_eval(aw, K=1, experiment="vanilla", task_ids=["t1"], reuse_complete=True)
    assert len(calls) == 2
    # a changed identity must not resume the old trial files either
    assert trials == [("t1", 0), ("t1", 0)]


def test_run_eval_reruns_when_extra_args_change(aw, monkeypatch):
    calls: list[list[str]] = []
    trials: list[tuple[str, int]] = []
    _fake_batch(monkeypatch, calls=calls, trials=trials)

    adapter.run_eval(aw, K=1, experiment="vanilla", task_ids=["t1"])
    changed = adapter.AppWorldConfig(
        appworld_root=aw.appworld_root,
        python_exe=aw.python_exe,
        config_path=aw.config_path,
        out_dir_root=aw.out_dir_root,
        split=aw.split,
        n=aw.n,
        extra_args=("--task-timeout", "400"),
    )
    adapter.run_eval(changed, K=1, experiment="vanilla", task_ids=["t1"], reuse_complete=True)
    assert len(calls) == 2
    assert trials.count(("t1", 0)) == 2


def test_a_failed_marker_write_does_not_fail_a_paid_eval(aw, monkeypatch, caplog):
    calls: list[list[str]] = []
    _fake_batch(monkeypatch, calls=calls)

    def refuse(*args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(adapter.runmark, "mark_complete", refuse)
    with caplog.at_level(logging.WARNING, logger=adapter.__name__):
        evals = adapter.run_eval(aw, K=1, experiment="vanilla", task_ids=["t1"])

    assert evals["t1"].passes == 1
    assert any("marker" in record.message for record in caplog.records)


def test_a_control_arm_in_a_new_session_re_measures(aw, monkeypatch, tmp_path: Path):
    calls: list[list[str]] = []
    trials: list[tuple[str, int]] = []
    _fake_batch(monkeypatch, calls=calls, trials=trials)
    harness = tmp_path / "harness"
    harness.mkdir()
    (harness / "harness.py").write_text("x = 1\n")

    def eval_fn(_node, task_ids, k, job_name, *, split="train"):
        return adapter.run_eval(aw, K=k, experiment=job_name, task_ids=task_ids, cwd=harness, reuse_complete=True)

    parent = SimpleNamespace(node_id="C0")
    first = policy.SameSessionPairedBaseline(k=1, launch_nonce="aaaa1111").for_round(
        1, parent, eval=eval_fn, train_task_ids=["t1"], anchor=None
    )
    second = policy.SameSessionPairedBaseline(k=1, launch_nonce="bbbb2222").for_round(
        1, parent, eval=eval_fn, train_task_ids=["t1"], anchor=None
    )

    assert first.mean == second.mean == 1.0
    assert len(calls) == 2
    assert trials == [("t1", 0), ("t1", 0)], "a new session must re-measure the control"
    assert (aw.out_dir_root / "control_r1_aaaa1111").is_dir()
    assert (aw.out_dir_root / "control_r1_bbbb2222").is_dir()


def test_a_control_arm_in_the_same_session_reuses_its_measurement(aw, monkeypatch, tmp_path: Path):
    calls: list[list[str]] = []
    trials: list[tuple[str, int]] = []
    _fake_batch(monkeypatch, calls=calls, trials=trials)
    harness = tmp_path / "harness"
    harness.mkdir()
    (harness / "harness.py").write_text("x = 1\n")

    def eval_fn(_node, task_ids, k, job_name, *, split="train"):
        return adapter.run_eval(aw, K=k, experiment=job_name, task_ids=task_ids, cwd=harness, reuse_complete=True)

    parent = SimpleNamespace(node_id="C0")
    policy.SameSessionPairedBaseline(k=1, launch_nonce="same0001").for_round(
        1, parent, eval=eval_fn, train_task_ids=["t1"], anchor=None
    )
    policy.SameSessionPairedBaseline(k=1, launch_nonce="same0001").for_round(
        1, parent, eval=eval_fn, train_task_ids=["t1"], anchor=None
    )

    assert len(calls) == 1
    assert trials == [("t1", 0)]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _init_repo(repo: Path) -> str:
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "harness.py").write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "base")
    return _git(repo, "rev-parse", "HEAD")


def test_a_real_worktree_trial_writes_and_reuses_its_marker(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    sha = _init_repo(repo)
    aw = adapter.AppWorldConfig(
        appworld_root=repo,
        python_exe="python",
        config_path=tmp_path / "subject.json",
        out_dir_root=tmp_path / "runs",
        split="train",
        n=1,
    )
    calls: list[list[str]] = []
    seen_worktrees: list[Path] = []

    def run(argv, *, cwd=None, env=None, check=None, timeout=None):
        calls.append(list(argv))
        worktree = Path(cwd)
        assert (worktree / "harness.py").read_text() == "x = 1\n"
        seen_worktrees.append(worktree)
        out_dir = Path(argv[argv.index("--out-dir") + 1])
        (out_dir / "t1_k0.json").write_text(json.dumps({"task_id": "t1", "success": True}))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(adapter, "subprocess", SimpleNamespace(run=run))

    eval_fn = aw_eval.make_appworld_eval_fn(aw, repo)
    node = SimpleNamespace(git_commit_sha=sha)
    first = eval_fn(node, ["t1"], 1, "confirm")
    assert first["t1"].passes == 1
    marker = read_mark(aw.out_dir_root / "confirm")
    assert marker is not None
    assert marker["harness"] != "base"

    second = eval_fn(node, ["t1"], 1, "confirm", split="train")
    assert second["t1"].passes == 1
    assert len(calls) == 1, "the same harness tree must reuse its marker"
    assert seen_worktrees and not seen_worktrees[0].exists(), "the ephemeral worktree is torn down"


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
        seen.update(kwargs, cfg=cfg)
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


def test_cold_start_eval_honours_the_requested_split(aw, monkeypatch, tmp_path: Path):
    seen: dict = {}

    def fake_run_eval(cfg, **kwargs):
        seen.update(kwargs, cfg=cfg)
        return {}

    monkeypatch.setattr(entry.git_ops, "worktree_at", lambda root, sha: _Worktree(tmp_path / "wt"))
    monkeypatch.setattr(entry.aw_adapter, "run_eval", fake_run_eval)

    base_eval = entry._make_cold_start_eval(aw, repo_root=tmp_path, base_sha="deadbeef")
    base_eval(None, ["t1"], 3, "vanilla", split="test")

    assert seen["cfg"].split == "test"
    assert aw.split == "train"


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


def test_unlink_with_retry_survives_a_transient_permission_error(tmp_path: Path, monkeypatch):
    target = tmp_path / "t1_k0.json"
    target.write_text("{}")
    original = Path.unlink
    attempts = {"n": 0}

    def flaky(self, *args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise PermissionError("held open")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky)
    adapter._unlink_with_retry(target)
    assert attempts["n"] == 3
    assert not target.exists()


def test_discard_stale_trials_removes_the_old_marker(tmp_path: Path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "t1_k0.json").write_text("{}")
    runmark.mark_complete(out_dir, run_id="old")

    adapter._discard_stale_trials(out_dir)

    assert not (out_dir / "t1_k0.json").exists()
    assert runmark.read_mark(out_dir) is None


def test_unreadable_config_does_not_discard_matching_trials(aw, monkeypatch):
    calls: list[list[str]] = []
    _fake_batch(monkeypatch, calls=calls)
    aw.config_path.write_text('{"model": "a"}')
    adapter.run_eval(aw, K=1, experiment="vanilla", task_ids=["t1"])

    discarded: list[Path] = []
    monkeypatch.setattr(adapter, "_content_digest", lambda path: "unreadable")
    monkeypatch.setattr(adapter, "_discard_stale_trials", lambda out_dir: discarded.append(out_dir))

    adapter.run_eval(aw, K=1, experiment="vanilla", task_ids=["t1"], reuse_complete=True)

    assert discarded == []
    assert len(calls) == 2


def test_content_digest_retries_a_transient_read_failure(aw, monkeypatch):
    aw.config_path.write_text('{"model": "a"}')
    original = Path.read_bytes
    attempts = {"n": 0}

    def flaky(self):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise PermissionError("held open")
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", flaky)
    assert adapter._content_digest(aw.config_path) != "unreadable"
    assert attempts["n"] == 3


def test_failed_stale_clear_keeps_the_marker_for_the_retry(tmp_path: Path, monkeypatch):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    trial = out_dir / "t1_k0.json"
    trial.write_text("{}")
    runmark.mark_complete(out_dir, run_id="old")

    monkeypatch.setattr(adapter, "_IO_ATTEMPTS", 2)
    monkeypatch.setattr(adapter, "_IO_BACKOFF", 0.001)
    original = Path.unlink

    def held(self, *args, **kwargs):
        if self.name == "t1_k0.json":
            raise PermissionError("held open")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", held)
    with pytest.raises(OSError):
        adapter._discard_stale_trials(out_dir)
    assert trial.exists()
    assert runmark.read_mark(out_dir) is not None, "the marker must survive a failed clear"

    monkeypatch.setattr(Path, "unlink", original)
    adapter._discard_stale_trials(out_dir)
    assert not trial.exists()
    assert runmark.read_mark(out_dir) is None
