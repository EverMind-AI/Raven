"""Unit tests for the shared production wiring (orchestrator/production.py)."""

from __future__ import annotations

from pathlib import Path

from raven.evolver.analysis.stability_bucket import StabilityBucket, TaskStability
from raven.evolver.orchestrator.config import OrchestratorConfig
from raven.evolver.orchestrator.production import build_evolution_orchestrator
from raven.evolver.orchestrator.scoring import EvalBackend, TaskEval
from raven.evolver.scheduler.anchor_selection import simple_anchor


def _fake_backend() -> EvalBackend:
    stability = {
        t: TaskStability(task_id=t, passes=p, attempts=3, bucket=b)
        for t, p, b in [
            ("t1", 3, StabilityBucket.STABLE_PASS),
            ("t2", 0, StabilityBucket.STABLE_FAIL),
            ("t3", 1, StabilityBucket.BORDERLINE_1_3),
        ]
    }
    return EvalBackend(
        train_task_ids=list(stability),
        test_task_ids=[],
        eval=lambda node, ids, k, job, **kw: {t: TaskEval(task_id=t, passes=0, attempts=k) for t in ids},
        cold_start=lambda: stability,
        anchor=lambda affinity=None: simple_anchor(stability),
    )


def _build(tmp_path: Path, *, gsme: bool):
    config = OrchestratorConfig(
        repo_root=tmp_path,
        work_dir=tmp_path / "work",
        driver_llm_spec={},
        gsme=gsme,
    )
    captured: dict = {}

    def design_of(sha_of, history, archive_summary_of):
        captured["archive_summary_of"] = archive_summary_of
        return lambda round_index, failure_map, parent: []

    orch = build_evolution_orchestrator(
        config,
        repo_root=tmp_path,
        base_sha="deadbeef",
        root_node_id="C0",
        backend=_fake_backend(),
        gate_policy=None,
        diagnose_of=lambda vanilla_node: (lambda ri, parent: {}, None),
        design_of=design_of,
        baseline_of=lambda: None,
        files_of=lambda cand: dict(getattr(cand, "files", {})),
    )
    return orch, captured


class TestGsmeSwitch:
    def test_gsme_on_wires_archive_and_recombiner(self, tmp_path):
        orch, captured = _build(tmp_path, gsme=True)
        assert orch._archive is not None
        assert orch._recombine is not None
        assert captured["archive_summary_of"]() == ""  # empty bank, not disabled

    def test_gsme_off_ablates_archive_recombiner_and_prompt(self, tmp_path):
        orch, captured = _build(tmp_path, gsme=False)
        assert orch._archive is None
        assert orch._recombine is None
        assert captured["archive_summary_of"]() == ""

    def test_gsme_off_never_touches_archive_path(self, tmp_path):
        _build(tmp_path, gsme=False)
        assert not (tmp_path / "work" / "archive.json").exists()
