"""Adaptive branching policy (spec §18.7 Option G).

Decides how many children :math:`B` to spawn per evolution round, as a
function of the current evolution state, rather than carrying a fixed
``B`` hyperparameter through every round. Sister algorithm to Option F
(``tree_aware_bandit.py``):

  - Option F: same B, picks K tasks per child more sample-efficiently
  - Option G: adaptive B per round (this module)

Per spec §18.7.3 the three implementation tiers are:

  1. **Rule-based** (this MVP) — ``B = f(state)`` with a small closed-
     form formula. ~2h engineering.
  2. **Learned rule** — fit a regression of B → final lift on each
     round's outcomes. ~3-4h.
  3. **Bandit-on-branching** — treat B as the arm, reward as
     ``Δ lift − cost`` per round. ~1 week, paper-worthy.

This module is tier 1. Future tiers can substitute ``branching_policy``
at its call sites without changing them.

State inputs:

  - ``round_idx``               — current round number (1-indexed)
  - ``remaining_budget``        — children we can still afford this evolution
  - ``n_uncovered_cells``       — failure_map cells with NO candidate yet
  - ``archive_coverage``        — fraction of target cells touched (0-1)
  - ``parent_lift_posterior``   — most recent parent's measured lift
                                  (Bernoulli p̂_child − p̂_root, optional)

Output is a :class:`BranchingDecision` carrying ``B`` plus a one-line
``rationale`` and the contributing components for logging — so future
analysis can reconstruct WHY a given round picked ``B=3`` rather than
``B=5``.

Typical B range per spec: ``[3, 5]``. The MVP keeps ``min_b=1`` so the
policy can land on a single child when state strongly indicates depth
(near-saturation coverage + strong parent lift), but the floor sits at
3 under default exploration pressure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class BranchingState:
    """Snapshot of evolution state at decision time.

    All fields except ``parent_lift_posterior`` are required — the
    policy doesn't try to infer missing inputs. ``parent_lift_posterior``
    is None for the very first round (no parent to measure from) and
    in that case the policy uses pure coverage-driven branching.
    """

    round_idx: int
    remaining_budget: int
    n_uncovered_cells: int
    archive_coverage: float
    parent_lift_posterior: Optional[float] = None

    def __post_init__(self) -> None:
        if self.round_idx < 1:
            raise ValueError(f"round_idx must be >= 1, got {self.round_idx}")
        if self.remaining_budget < 0:
            raise ValueError(
                f"remaining_budget must be >= 0, got {self.remaining_budget}"
            )
        if self.n_uncovered_cells < 0:
            raise ValueError(
                f"n_uncovered_cells must be >= 0, got {self.n_uncovered_cells}"
            )
        if not 0.0 <= self.archive_coverage <= 1.0:
            raise ValueError(
                f"archive_coverage must be in [0, 1], got "
                f"{self.archive_coverage}"
            )


@dataclass(frozen=True)
class BranchingDecision:
    """Output of :func:`branching_policy`."""

    B: int
    rationale: str
    components: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Defaults (spec §18.7.2 typical B ∈ [3, 5])
# ---------------------------------------------------------------------------

DEFAULT_MIN_B = 1
DEFAULT_MAX_B = 5
DEFAULT_TARGET_COVERAGE = 0.85

# Lift posterior thresholds (Bernoulli p̂_child − p̂_root)
LIFT_STRONG_POSITIVE = 0.10   # ≥ +10% pass rate → depth on winner (−1 to B)
LIFT_STRONG_NEGATIVE = -0.05  # ≤ -5%  → broaden search (+1 to B)

# Round-decay onset (≥ this round, B is shaded down by 1)
LATE_ROUND_ONSET = 5


def branching_policy(
    state: BranchingState,
    *,
    min_b: int = DEFAULT_MIN_B,
    max_b: int = DEFAULT_MAX_B,
    target_coverage: float = DEFAULT_TARGET_COVERAGE,
) -> BranchingDecision:
    """Decide branching factor ``B`` for the next evolution round.

    Algorithm (per spec §18.7.2 + §18.7.3 tier 1):

    1. **Coverage-driven base.** Start from the gap between current
       coverage and the target. Larger gap → larger B (explore).
       Buckets: gap > 0.5 → 4, > 0.3 → 3, > 0.15 → 2, else 1.

    2. **Uncovered-cell floor.** If there are still ≥ 5 cells without
       any candidate, ensure ``B ≥ 3`` regardless of coverage % — many
       open cells call for breadth.

    3. **Parent-lift adjustment.** If the previous round's parent had
       a strong measured lift (≥ +0.10), shade B down by 1 (depth on
       the winner). If lift was negative (≤ -0.05), shade B up by 1
       (escape the losing direction).

    4. **Budget cap.** Cannot exceed ``remaining_budget``.

    5. **Late-round decay.** Round ≥ 5 shades down by 1 (consolidation).

    6. **Clamp** to ``[min_b, max_b]``.

    Returns a decision with ``B`` plus a rationale string and per-step
    contribution dict for later analysis.
    """
    if min_b < 1:
        raise ValueError(f"min_b must be >= 1, got {min_b}")
    if max_b < min_b:
        raise ValueError(f"max_b ({max_b}) must be >= min_b ({min_b})")
    if not 0.0 <= target_coverage <= 1.0:
        raise ValueError(
            f"target_coverage must be in [0, 1], got {target_coverage}"
        )

    components: dict[str, int] = {}
    rationale_parts: list[str] = []

    # ── 1. Coverage-driven base ──
    gap = target_coverage - state.archive_coverage
    if gap > 0.5:
        b_base = 4
    elif gap > 0.3:
        b_base = 3
    elif gap > 0.15:
        b_base = 2
    else:
        b_base = 1
    components["coverage_base"] = b_base
    rationale_parts.append(
        f"coverage gap {gap:+.2f} → base {b_base}"
    )

    # ── 2. Uncovered-cell floor ──
    if state.n_uncovered_cells >= 5 and b_base < 3:
        b_after_floor = 3
        components["uncovered_floor"] = b_after_floor - b_base
        rationale_parts.append(
            f"{state.n_uncovered_cells} uncovered cells → floor to {b_after_floor}"
        )
        b_base = b_after_floor

    # ── 3. Parent-lift adjustment ──
    if state.parent_lift_posterior is not None:
        if state.parent_lift_posterior >= LIFT_STRONG_POSITIVE:
            components["lift_adjustment"] = -1
            rationale_parts.append(
                f"parent lift {state.parent_lift_posterior:+.3f} ≥ "
                f"{LIFT_STRONG_POSITIVE:+.2f} → depth (-1)"
            )
            b_base -= 1
        elif state.parent_lift_posterior <= LIFT_STRONG_NEGATIVE:
            components["lift_adjustment"] = +1
            rationale_parts.append(
                f"parent lift {state.parent_lift_posterior:+.3f} ≤ "
                f"{LIFT_STRONG_NEGATIVE:+.2f} → broaden (+1)"
            )
            b_base += 1
        else:
            components["lift_adjustment"] = 0

    # ── 4. Late-round decay ──
    if state.round_idx >= LATE_ROUND_ONSET:
        components["late_round_decay"] = -1
        rationale_parts.append(
            f"round {state.round_idx} ≥ {LATE_ROUND_ONSET} → decay (-1)"
        )
        b_base -= 1

    # ── 5. Budget cap ──
    if b_base > state.remaining_budget:
        components["budget_cap"] = state.remaining_budget - b_base
        rationale_parts.append(
            f"budget {state.remaining_budget} caps from {b_base}"
        )
        b_base = state.remaining_budget

    # ── 6. Clamp ──
    final_b = max(min_b, min(max_b, b_base))
    if final_b != b_base:
        components["clamp"] = final_b - b_base
        rationale_parts.append(
            f"clamped {b_base} → {final_b} (min_b={min_b}, max_b={max_b})"
        )

    components["final"] = final_b
    return BranchingDecision(
        B=final_b,
        rationale=" | ".join(rationale_parts),
        components=components,
    )


__all__ = [
    "BranchingState",
    "BranchingDecision",
    "branching_policy",
    "LIFT_STRONG_POSITIVE",
    "LIFT_STRONG_NEGATIVE",
    "LATE_ROUND_ONSET",
    "DEFAULT_MIN_B",
    "DEFAULT_MAX_B",
    "DEFAULT_TARGET_COVERAGE",
]
