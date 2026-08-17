"""Orchestrator configuration — one object wiring the whole seven-step funnel.

Everything the FSM needs that is *not* code: where the scorer lives
(``framework``), where the vanilla thick ledger sits (``cold_start_ledger_dir``,
the fixed comparison baseline — the funnel always compares against vanilla, not
the previous parent), the anchor/screen knobs, the per-round design budget, the
termination thresholds, and the on-disk state roots.

The driver model is *not* constructed here — ``driver_llm_spec`` is the dict
handed to ``raven.evolver.judge.llm_client.build_judge_llm``, so the same
config serialises to yaml and a test can inject a ``MockBackend`` without a
factory. Broad model support (self-hosted Qwen, Claude, OpenRouter) comes for
free from that existing backend stack.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AnchorParams:
    """Slot budget + cull width for the K=1 anchor screen (see ``select_anchor``)."""

    # 12 = 6 stable + 6 borderline controls per candidate (stratified + rotated
    # in loop._sentinels_for): regressions concentrate on borderline tasks, and
    # 3 stable-only sentinels were observed missing a 58%-regression candidate.
    n_sentinel: int = 12
    n_icebreaker: int = 5
    n_borderline: int = 7
    cull_sigma_mult: float = 1.5


@dataclass(frozen=True)
class Budget:
    """Per-round design budget (SOP §2 ②: 1-2 WHY x 2-3 candidates).

    ``recombinations_per_round`` caps the deterministic cross-cell GSME
    recombinations appended after the designed candidates (0 disables).
    """

    max_why_per_round: int = 2
    candidates_per_why: int = 3
    driver_token_budget: int | None = None
    recombinations_per_round: int = 1


@dataclass(frozen=True)
class Termination:
    """Loop stop conditions. Compared against vanilla; test is never consulted.

    ``patience`` is the SOP's primary exhaustion signal (consecutive rounds with
    no candidate beating vanilla on train); ``max_rounds`` is the hard cap.
    ``max_consecutive_errors`` stops a run whose rounds keep erroring out
    (driver/infra outage) with an honest ``errors_exhausted`` reason instead of
    letting the outage burn patience and masquerade as exploration exhaustion.
    """

    patience: int = 10
    max_rounds: int = 20
    max_consecutive_errors: int = 5


@dataclass(frozen=True)
class OrchestratorConfig:
    """Top-level orchestrator configuration — bench-neutral.

    Nothing here names a benchmark: the scorer, the dataset splits, the
    cold-start baseline, and the anchor all live behind the injected
    :class:`~raven.evolver.orchestrator.scoring.EvalBackend`. This object only
    holds the driver model, the funnel's numeric knobs, and the on-disk state
    roots. ``anchor`` (AnchorParams) is neutral tuning a backend factory may
    consume when it builds a trial-ledger anchor.
    """

    repo_root: Path
    work_dir: Path
    driver_llm_spec: dict[str, Any]

    k_screen: int = 1
    k_confirm: int = 3
    anchor: AnchorParams = field(default_factory=AnchorParams)
    budget: Budget = field(default_factory=Budget)
    termination: Termination = field(default_factory=Termination)

    # Sealed test: scored by a script into a dir the driver never reads, so the
    # sealed-test rule is enforced by isolation, not by driver discipline.
    sealed_test_split: str = "test"
    sealed_output_dir: Path | None = None

    def __post_init__(self) -> None:
        for name in ("repo_root", "work_dir"):
            object.__setattr__(self, name, Path(getattr(self, name)))
        if self.sealed_output_dir is not None:
            object.__setattr__(self, "sealed_output_dir", Path(self.sealed_output_dir))
        if self.k_screen < 1 or self.k_confirm < 1:
            raise ValueError("k_screen and k_confirm must be >= 1")

    # Conventional on-disk state layout under work_dir.
    @property
    def nodes_dir(self) -> Path:
        return self.work_dir / "nodes"

    @property
    def failure_map_path(self) -> Path:
        return self.work_dir / "failure_map.json"

    @property
    def archive_path(self) -> Path:
        return self.work_dir / "archive.json"

    @property
    def findings_path(self) -> Path:
        return self.work_dir / "findings.md"

    @property
    def journal_dir(self) -> Path:
        return self.work_dir / "journal"


__all__ = [
    "AnchorParams",
    "Budget",
    "Termination",
    "OrchestratorConfig",
]
