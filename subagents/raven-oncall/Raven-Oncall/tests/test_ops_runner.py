"""Tests for the runner helpers (remote prep + one-call campaign)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from raven.ops import (
    Campaign,
    JobPlan,
    Ledger,
    LLMProposer,
    MockJobBackend,
    Trial,
    config_key,
    drive_polling,
    ensure_image,
    make_ssh_sync,
    prepare_remote,
    run_adaptive_polling,
    run_grid,
)


class _FakeRun:
    def __init__(self, image_present: bool) -> None:
        self._present = image_present
        self.calls: list[str] = []

    def __call__(self, cmd: str) -> tuple[int, str]:
        self.calls.append(cmd)
        if cmd.startswith("docker images -q"):
            return 0, ("sha256:abc" if self._present else "")
        return 0, ""


def test_ensure_image_pulls_only_when_absent() -> None:
    absent = _FakeRun(image_present=False)
    assert ensure_image(absent, "python:3.12-slim") is True
    assert any(c.startswith("docker pull") for c in absent.calls)

    present = _FakeRun(image_present=True)
    assert ensure_image(present, "python:3.12-slim") is False
    assert not any(c.startswith("docker pull") for c in present.calls)


def test_prepare_remote_syncs_then_ensures_image() -> None:
    run = _FakeRun(image_present=False)
    synced: list[tuple[str, str]] = []

    def sync(local: str, remote: str) -> tuple[int, str]:
        synced.append((local, remote))
        return 0, ""

    prepare_remote(run, sync, image="python:3.12-slim", app_local="./app", app_remote="/root/raven-ops/app")

    assert synced == [("./app", "/root/raven-ops/app")]
    assert any(c.startswith("docker pull") for c in run.calls)


def test_make_ssh_sync_sets_connect_timeout(monkeypatch) -> None:
    captured: dict = {}

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return _Proc()

    monkeypatch.setattr(subprocess, "run", fake_run)

    sync = make_ssh_sync("1.2.3.4", 2222, "~/.ssh/id_rsa", connect_timeout=15)
    sync("./app", "/root/raven-ops/app")

    ssh = captured["argv"][captured["argv"].index("-e") + 1]
    assert "ConnectTimeout=15" in ssh  # unreachable host fails fast, not hang on rsync
    assert "BatchMode=yes" in ssh  # never block on a password prompt


async def test_run_grid_returns_best(tmp_path: Path) -> None:
    grid = [{"k1": 0.8, "b": 0.4}, {"k1": 1.4, "b": 0.5}, {"k1": 2.0, "b": 0.6}]
    plans = {}
    for i, cfg in enumerate(grid):
        plans[config_key(cfg)] = JobPlan(metrics={"ndcg": 0.30 + i * 0.02}, output={"config": cfg})
    backend = MockJobBackend(plans=plans)

    best = await run_grid("bm25", grid, backend, Ledger(tmp_path / "l.json"), metric="ndcg", goal="max")

    assert best is not None
    assert best.idem_key == config_key({"k1": 2.0, "b": 0.6})
    assert round(best.result.metrics["ndcg"], 2) == 0.34


async def test_run_adaptive_polling_seeds_then_lets_llm_explore(tmp_path: Path) -> None:
    seed = {"k1": 0.8, "b": 0.4}
    proposed = {"k1": 1.4, "b": 0.6}
    plans = {
        config_key(seed): JobPlan(metrics={"ndcg": 0.28}, output={"config": seed}),
        config_key(proposed): JobPlan(metrics={"ndcg": 0.33}, output={"config": proposed}),
    }
    backend = MockJobBackend(plans=plans)

    async def complete(prompt: str) -> str:
        return '[{"k1": 1.4, "b": 0.6}]'

    proposer = LLMProposer(complete, objective="tune bm25", seed=[seed], batch_size=3, max_rounds=2)
    best = await run_adaptive_polling(
        "bm25_tune",
        proposer,
        backend,
        Ledger(tmp_path / "l.json"),
        metric="ndcg",
        goal="max",
        max_rounds=2,
        interval=0.0,
    )

    assert best is not None
    assert best.idem_key == config_key(proposed)  # LLM's proposal beat the seed
    assert best.result.metrics["ndcg"] == 0.33


async def test_drive_polling_completes(tmp_path: Path) -> None:
    backend = MockJobBackend(default_plan=JobPlan(succeed_after_polls=2, metrics={"score": 0.5}))
    trials = [Trial("t1"), Trial("t2")]
    campaign = Campaign("c", trials, backend, Ledger(tmp_path / "l.json"), metric="score")

    best = await drive_polling(campaign, interval=0.0, max_ticks=50)

    assert campaign.is_done()
    assert best is not None and best.result.metrics["score"] == 0.5
