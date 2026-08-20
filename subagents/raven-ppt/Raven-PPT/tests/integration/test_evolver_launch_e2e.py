"""End-to-end tests of the launcher state machine with a fake bench.

Everything is real except the scorer: real CLI entry (cmd_run/status/finalize),
real RunMeta guards, real EvolutionOrchestrator + journal replay. The fake
bench writes trial marker files for cold start and drives one-candidate rounds
that die at preflight, so no subprocess or LLM is needed. Interruption is
injected via KeyboardInterrupt (BaseException — the loop must not swallow it).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from raven.evolver.launch import runner as runner_mod
from raven.evolver.launch.contract import BenchBundle
from raven.evolver.launch.state import RunMeta
from raven.evolver.tree import git_ops


@pytest.fixture(autouse=True)
def _reset_ephemeral_root():
    yield
    git_ops.set_ephemeral_root(None)


@pytest.fixture()
def repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "subject"
    (repo / "src").mkdir(parents=True)
    (repo / "src/x.py").write_text("x = 1\n")
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "PATH": "/usr/bin:/bin",
    }
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=repo, check=True, env=env, capture_output=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    return repo, sha


@pytest.fixture()
def spec_path(tmp_path: Path, repo) -> Path:
    repo_dir, sha = repo
    path = tmp_path / "spec.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "bench": "fake",
                "repo_root": str(repo_dir),
                "base_sha": sha,
                "work_dir": str(tmp_path / "work"),
                "funnel": {
                    "k_confirm": 1,
                    "budget": {"max_why_per_round": 1, "candidates_per_why": 1, "recombinations_per_round": 0},
                    "termination": {"patience": 5, "max_rounds": 2},
                },
            }
        )
    )
    return path


@pytest.fixture()
def fake_bench(monkeypatch):
    """Install a fake bench and return its control/observation flags."""
    flags = {
        "cold_interrupt_after": None,  # write N trial files, then KeyboardInterrupt
        "kb_on_round": None,  # raise inside design of this round
        "cold_writes": 0,
        "design_calls": 0,
        "unseal_calls": 0,
        "no_unseal": False,  # build the bundle without a sealed test
        "unseal_fail_times": 0,  # make the next N unseal calls raise
        "precheck_error": None,  # make the Gate0 precheck raise this message
        "precheck_calls": 0,
    }

    def build(ctx) -> BenchBundle:
        from raven.evolver.analysis.stability_bucket import StabilityBucket, TaskStability
        from raven.evolver.orchestrator.loop import EvolutionOrchestrator
        from raven.evolver.orchestrator.scoring import EvalBackend, TaskEval
        from raven.evolver.scheduler.anchor_selection import simple_anchor
        from raven.evolver.tree.node import HarnessNode

        work = Path(ctx.spec.work_dir)
        van = work / "runs" / "vanilla"
        train = ["t1", "t2", "t3"]
        k = ctx.spec.funnel.k_confirm

        def cold_start_done() -> int:
            return len(list(van.glob("*.json"))) if van.is_dir() else 0

        def run_cold_start() -> None:
            van.mkdir(parents=True, exist_ok=True)
            for tid in train:
                for i in range(k):
                    out = van / f"{tid}_k{i}.json"
                    if out.exists():
                        continue
                    if (
                        flags["cold_interrupt_after"] is not None
                        and flags["cold_writes"] >= flags["cold_interrupt_after"]
                    ):
                        raise KeyboardInterrupt
                    out.write_text("{}")
                    flags["cold_writes"] += 1

        stability = {
            t: TaskStability(task_id=t, passes=p, attempts=3, bucket=b)
            for t, p, b in [
                ("t1", 3, StabilityBucket.STABLE_PASS),
                ("t2", 0, StabilityBucket.STABLE_FAIL),
                ("t3", 1, StabilityBucket.BORDERLINE_1_3),
            ]
        }
        backend = EvalBackend(
            train_task_ids=train,
            test_task_ids=[],
            eval=lambda node, ids, k_, job, **kw: {t: TaskEval(task_id=t, passes=0, attempts=k_) for t in ids},
            cold_start=lambda: stability,
            anchor=lambda affinity=None: simple_anchor(stability),
        )

        class Cand:
            why = "w"
            files = {"src/x.py": b"x"}
            summary = "fake candidate"

        def design_fn(round_index, failure_map, parent):
            flags["design_calls"] += 1
            if flags["kb_on_round"] == round_index:
                flags["kb_on_round"] = None
                raise KeyboardInterrupt
            return [Cand()]

        def build_orchestrator():
            return EvolutionOrchestrator(
                ctx.spec.funnel,
                backend=backend,
                diagnose_fn=lambda ri, parent: {"why_distribution": {"w": 5.0}},
                design_fn=design_fn,
                apply_fn=lambda pid, patch, ri: (_ for _ in ()).throw(
                    AssertionError("preflight must reject before apply")
                ),
                preflight_fn=lambda cand, parent: False,
            )

        root = HarnessNode(
            node_id="C0",
            parent_id=None,
            git_commit_sha=ctx.spec.base_sha,
            git_branch="",
            created_at=HarnessNode.utc_now(),
            created_at_iter=0,
        )

        def unseal(records, orch) -> dict:
            flags["unseal_calls"] += 1
            if flags["unseal_fail_times"] > 0:
                flags["unseal_fail_times"] -= 1
                raise RuntimeError("sealed scoring endpoint died")
            return {"best_round": len(records), "retention": 0.5}

        def precheck() -> None:
            flags["precheck_calls"] += 1
            if flags["precheck_error"]:
                raise RuntimeError(flags["precheck_error"])

        return BenchBundle(
            root_node_id="C0",
            root_node=root,
            journal_path=work / "journal" / "rounds.jsonl",
            cold_start_total=len(train) * k,
            cold_start_done=cold_start_done,
            run_cold_start=run_cold_start,
            build_orchestrator=build_orchestrator,
            unseal=None if flags["no_unseal"] else unseal,
            precheck=precheck,
        )

    monkeypatch.setattr(runner_mod, "load_bench", lambda name, repo_root=None: build)
    return flags


class TestFullPipeline:
    def test_run_completes_all_three_phases(self, spec_path, fake_bench, tmp_path):
        assert runner_mod.cmd_run(str(spec_path)) == 0
        work = tmp_path / "work"
        assert len(list((work / "runs" / "vanilla").glob("*.json"))) == 3
        journal = (work / "journal" / "rounds.jsonl").read_text().splitlines()
        assert len(journal) == 2  # max_rounds=2, all candidates pruned
        report = json.loads((work / "retention.json").read_text())
        assert report == {"best_round": 2, "retention": 0.5}
        meta = RunMeta.load(work)
        assert meta.unsealed_at and "max_rounds" in (meta.finalize_reason or "")

    def test_completed_run_refuses_to_resume(self, spec_path, fake_bench):
        assert runner_mod.cmd_run(str(spec_path)) == 0
        with pytest.raises(SystemExit) as exc:
            runner_mod.cmd_run(str(spec_path))
        assert exc.value.code == 2

    def test_status_after_completion(self, spec_path, fake_bench, capsys):
        runner_mod.cmd_run(str(spec_path))
        capsys.readouterr()
        assert runner_mod.cmd_status(str(spec_path)) == 0
        assert "UNSEALED" in capsys.readouterr().out


class TestInterruptResume:
    def test_cold_start_interrupt_then_resume(self, spec_path, fake_bench, tmp_path):
        fake_bench["cold_interrupt_after"] = 2
        assert runner_mod.cmd_run(str(spec_path)) == 130
        van = tmp_path / "work" / "runs" / "vanilla"
        assert len(list(van.glob("*.json"))) == 2  # partial work kept

        fake_bench["cold_interrupt_after"] = None
        assert runner_mod.cmd_run(str(spec_path)) == 0
        # Resume filled only the missing trial (2 before + 1 after = 3 writes).
        assert fake_bench["cold_writes"] == 3

    def test_round_interrupt_then_resume_replays_journal(self, spec_path, fake_bench, tmp_path):
        fake_bench["kb_on_round"] = 2  # round 1 completes, round 2 dies mid-design
        assert runner_mod.cmd_run(str(spec_path)) == 130
        journal = tmp_path / "work" / "journal" / "rounds.jsonl"
        assert len(journal.read_text().splitlines()) == 1

        calls_before = fake_bench["design_calls"]
        assert runner_mod.cmd_run(str(spec_path)) == 0
        # Round 1 is replayed from the journal, not re-designed: only the
        # re-run of round 2 costs a design call.
        assert fake_bench["design_calls"] == calls_before + 1
        assert len(journal.read_text().splitlines()) == 2

    def test_status_midway_shows_rounds_and_no_test_numbers(self, spec_path, fake_bench, capsys):
        fake_bench["kb_on_round"] = 2
        runner_mod.cmd_run(str(spec_path))
        capsys.readouterr()
        assert runner_mod.cmd_status(str(spec_path)) == 0
        out = capsys.readouterr().out
        assert "1 completed round" in out
        assert "sealed" in out  # the reminder, not the numbers
        assert "retention" not in out


class TestFinalize:
    def test_unseal_failure_leaves_run_resumable(self, spec_path, fake_bench, tmp_path):
        """A dead endpoint during sealed scoring must not stamp the run;
        re-running retries the unseal and succeeds."""
        fake_bench["unseal_fail_times"] = 1
        assert runner_mod.cmd_run(str(spec_path)) == 1
        work = tmp_path / "work"
        meta = RunMeta.load(work)
        assert meta.unsealed_at is None
        assert not (work / "retention.json").exists()

        assert runner_mod.cmd_run(str(spec_path)) == 0
        meta = RunMeta.load(work)
        assert meta.unsealed_at is not None
        assert (work / "retention.json").exists()
        assert fake_bench["unseal_calls"] == 2

    def test_finalize_after_no_sealed_run_is_stable(self, spec_path, fake_bench, tmp_path):
        """A run finalized without a sealed test must not have its stamp
        rewritten (or --yes bypassed) by later finalize calls."""
        fake_bench["no_unseal"] = True
        assert runner_mod.cmd_run(str(spec_path)) == 0
        work = tmp_path / "work"
        meta = RunMeta.load(work)
        stamped_at, reason = meta.unsealed_at, meta.finalize_reason
        assert "no sealed test" in reason

        for _ in range(2):
            assert runner_mod.cmd_finalize(str(spec_path), yes=False) == 0
            meta = RunMeta.load(work)
            assert (meta.unsealed_at, meta.finalize_reason) == (stamped_at, reason)
        assert not (work / "retention.json").exists()
        assert fake_bench["unseal_calls"] == 0

    def test_finalize_midway_unseals_and_locks(self, spec_path, fake_bench, tmp_path):
        fake_bench["kb_on_round"] = 2
        runner_mod.cmd_run(str(spec_path))

        assert runner_mod.cmd_finalize(str(spec_path), yes=False) == 2  # needs --yes
        assert fake_bench["unseal_calls"] == 0

        assert runner_mod.cmd_finalize(str(spec_path), yes=True) == 0
        assert fake_bench["unseal_calls"] == 1
        work = tmp_path / "work"
        assert json.loads((work / "retention.json").read_text())["best_round"] == 1
        meta = RunMeta.load(work)
        assert meta.finalize_reason == "user_finalized"

        with pytest.raises(SystemExit):
            runner_mod.cmd_run(str(spec_path))

    def test_finalize_before_any_round_refuses(self, spec_path, fake_bench):
        fake_bench["cold_interrupt_after"] = 1
        runner_mod.cmd_run(str(spec_path))  # dies in cold start
        assert runner_mod.cmd_finalize(str(spec_path), yes=True) == 2


class TestCheck:
    def test_check_runs_precheck_and_passes(self, spec_path, fake_bench, capsys):
        assert runner_mod.cmd_check(str(spec_path)) == 0
        assert fake_bench["precheck_calls"] == 1
        out = capsys.readouterr().out
        assert "bench precheck: OK" in out
        assert "check OK" in out

    def test_check_fails_on_dead_environment(self, spec_path, fake_bench, capsys):
        """A dead subject endpoint must fail `check`, not the first cold-start
        trial hours later."""
        fake_bench["precheck_error"] = "subject endpoint unreachable (http://x:1/v1)"
        assert runner_mod.cmd_check(str(spec_path)) == 1
        captured = capsys.readouterr()
        assert "subject endpoint unreachable" in captured.err
        assert "check OK" not in captured.out


class TestEphemeralWorktreeSweep:
    def test_run_sweeps_stale_worktrees_from_a_hard_killed_run(self, spec_path, fake_bench, tmp_path, repo):
        """SIGKILL leaves ephemeral worktrees behind (context managers never
        ran); the next launch of the same run must sweep them and drop their
        registration from the subject repo."""
        repo_dir, sha = repo
        stale = tmp_path / "work" / "tmp" / "evolver-wt-stale" / "wt"
        git_ops.create_worktree(repo_dir, stale, sha)
        assert stale.is_dir()

        assert runner_mod.cmd_run(str(spec_path)) == 0
        assert not (tmp_path / "work" / "tmp" / "evolver-wt-stale").exists()
        listed = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "evolver-wt-stale" not in listed

    def test_ephemeral_worktrees_land_under_work_dir(self, spec_path, fake_bench, tmp_path, repo):
        repo_dir, sha = repo
        assert runner_mod.cmd_run(str(spec_path)) == 0
        with git_ops.worktree_at(repo_dir, sha) as wt:
            assert str(wt).startswith(str(tmp_path / "work" / "tmp"))
