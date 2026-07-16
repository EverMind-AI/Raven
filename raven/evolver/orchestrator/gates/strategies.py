"""The two concrete gate policies (SWE paired-2sigma, AppWorld focused-Fisher).

Both implement :class:`GatePolicy.decide` over a :class:`DecisionContext`,
owning their own ``eval`` calls. Neither knows how its control arm was produced
(frozen vs same-session) — that is the :class:`BaselineProvider`'s concern.

Both stages follow the SOP's two disciplines:

- **Wide-pass screen (SOP §2 ⑤a).** A candidate advances to the full-train
  confirm unless it is *clearly worse* than the baseline on its probe set. A
  slightly-low or noise-band probe is NOT evidence — small K=1/K=3 probes have
  huge variance and the probe mean does not predict the full-set mean.
- **Two-threshold verdict (SOP §0).** Promotion (banking, parent selection) is
  the loose *navigator* condition: candidate full-train mean beats the control.
  The paired-2σ significance is a separate *credited* label reported alongside
  (``CandidateOutcome.paired.credited_2sigma``), never the promotion bar.

The confirm job name is defined once here (:func:`confirm_job_name`) because it
doubles as the on-disk out-dir a bench's diagnosis later reads trajectories
from — the naming is a cross-module contract, not a local detail.
"""

from __future__ import annotations

from raven.evolver.orchestrator.gates.fisher import (
    fisher_one_sided,
    focused_counts,
    train_mean,
)
from raven.evolver.orchestrator.gates.pipeline import run_gates
from raven.evolver.orchestrator.gates.policy import CandidateOutcome, DecisionContext
from raven.evolver.orchestrator.nodes.screen import screen_candidate
from raven.evolver.tree.node import NodeStatus

CONFIRM_JOB_SUFFIX = "_confirm"


def confirm_job_name(node_id: str) -> str:
    """The job name (= out-dir name for dir-based scorers) of a node's full-train
    confirm eval. Bench wiring that reads a promoted parent's confirm artifacts
    (e.g. AppWorld diagnosis over the confirm out-dir) must use this, not a
    hand-rolled f-string, so the gate and the reader cannot drift apart."""
    return f"{node_id}{CONFIRM_JOB_SUFFIX}"


class PairedTwoSigmaGate:
    """SWE line: K=1 anchor wide-pass screen -> K=3 full-train three-shield gate.

    Promotion is the navigator condition (candidate mean beats vanilla); the 2sigma
    credited label is reported alongside (see ``gates/paired``), not the bar.
    """

    def __init__(self, *, k_screen: int = 1, k_confirm: int = 3, z_threshold: float = 2.0):
        self.k_screen = k_screen
        self.k_confirm = k_confirm
        self.z_threshold = z_threshold

    def decide(self, ctx: DecisionContext) -> CandidateOutcome:
        if ctx.anchor is None:
            raise ValueError("PairedTwoSigmaGate requires an anchor in the context")
        node_id = ctx.node.node_id
        screen_evals = ctx.eval(
            ctx.node, ctx.anchor.task_ids, self.k_screen, f"{node_id}_screen"
        )
        screen = screen_candidate(
            candidate_evals=screen_evals, anchor=ctx.anchor, vanilla_evals=ctx.baseline.evals
        )
        if not screen.passes_to_confirm:
            return CandidateOutcome(node_id, NodeStatus.pruned_at_screen, screen=screen)

        confirm = ctx.eval(
            ctx.node, ctx.train_task_ids, self.k_confirm, confirm_job_name(node_id)
        )
        fired = ctx.fired_source(ctx.node, ctx.train_task_ids) if ctx.fired_source else None
        gate = run_gates(
            candidate_evals=confirm,
            control_evals=ctx.baseline.evals,
            task_ids=ctx.train_task_ids,
            fired_tasks=fired,
            z_threshold=self.z_threshold,
        )
        # The reported score is ALWAYS the full-train mean over the fixed
        # denominator; the paired stats may be narrowed to Gate-b's fired
        # subset, and a subset mean leaking out as "the score" would poison
        # beat_vanilla / parent selection / the journal curve with a number
        # that has a different denominator (review round-2 P0-4). Promotion is
        # the dual condition: the (possibly subset) paired verdict holds AND
        # the full-train mean does not regress the control — the fired subset
        # may claim credit, but never at the expense of the whole set.
        full_mean = train_mean(confirm, ctx.train_task_ids)
        control_full_mean = train_mean(ctx.baseline.evals, ctx.train_task_ids)
        promoted = gate.promoted and full_mean >= control_full_mean
        status = (
            NodeStatus.promoted_to_baseline if promoted else NodeStatus.pruned_at_confirm
        )
        return CandidateOutcome(
            node_id, status, score=full_mean, confirm_evals=confirm,
            screen=screen, paired=gate.paired, gate=gate,
            stats={"full_mean": full_mean, "control_full_mean": control_full_mean},
        )


class FocusedFisherGate:
    """AppWorld line: focused-subset wide-pass probe (stage 1) -> full-train
    three-shield gate (stage 2).

    Stage 1 runs the candidate only on its WHY's focused subset (plus the
    sentinel controls) and culls it ONLY when the probe shows it clearly worse
    than the baseline — a one-sided Fisher test in the *worse* direction at
    ``alpha``, or a sentinel regression beyond one flaky trial. Everything else
    (better, slightly low, indistinguishable) advances to the full confirm:
    wide-pass, SOP §2 ⑤a. The improvement-direction Fisher p is still reported
    (``fisher_p``) as evidence, but it is not an advancement bar.

    Stage 2 confirms on the full train set through the same three-shield
    pipeline as the SWE line: Gate-f infra report, Gate-b attribution when the
    bench wires a ``fired_source``, then the paired gate — navigator promotion
    (mean beats the control) with the credited-2σ label reported alongside.
    ``min_confirm_lift`` (default 0, the SOP navigator bar) optionally demands a
    minimum full-train lift on top of the navigator condition.
    """

    def __init__(
        self,
        *,
        k: int = 3,
        alpha: float = 0.05,
        min_confirm_lift: float = 0.0,
        z_threshold: float = 2.0,
    ):
        self.k = k
        self.alpha = alpha
        self.min_confirm_lift = min_confirm_lift
        self.z_threshold = z_threshold

    def decide(self, ctx: DecisionContext) -> CandidateOutcome:
        node_id = ctx.node.node_id
        focused = ctx.focused_task_ids
        sentinels = ctx.sentinel_task_ids
        # One eval over focused (the WHY subset) + sentinels (stable-pass controls),
        # so the regression guard costs no extra run.
        probe_ids = list(dict.fromkeys(list(focused) + list(sentinels)))
        cand_probe = (
            ctx.eval(ctx.node, probe_ids, self.k, f"{node_id}_focused") if probe_ids else {}
        )
        stats: dict = {}
        if focused:
            cp, cn = focused_counts(cand_probe, focused)
            vp, vn = focused_counts(ctx.baseline.evals, focused)
            foc_c = cp / (cp + cn) if (cp + cn) else 0.0
            foc_v = vp / (vp + vn) if (vp + vn) else 0.0
            stats.update(
                fisher_p=fisher_one_sided(cp, cn, vp, vn),
                fisher_p_worse=fisher_one_sided(vp, vn, cp, cn),
                foc_c=foc_c,
                foc_v=foc_v,
            )

        # Sentinel regression guard (SOP §2 ⑤a), stratified: stable-pass
        # controls never flake under the baseline, so any drop beyond one flaky
        # trial is signal; borderline controls flip by nature, so their verdict
        # uses a trial-level Fisher test (worse direction) instead of the mean
        # guard — the mean guard on flaky tasks would fire on noise.
        if sentinels:
            base = ctx.baseline.evals

            def _bmean(tid: str) -> float | None:
                ev = base.get(tid)
                good = (ev.attempts - ev.infra_attempts) if ev else 0
                return (ev.passes / good) if ev and good else None

            stable = [t for t in sentinels if _bmean(t) == 1.0]
            fragile = [t for t in sentinels if t not in stable and _bmean(t)]
            stats.update(
                sent_c=train_mean(cand_probe, sentinels),
                sent_v=train_mean(base, sentinels),
            )
            if stable:
                st_c = train_mean(cand_probe, stable)
                st_v = train_mean(base, stable)
                guard = 1.5 / (len(stable) * self.k)
                stats.update(sentinel_guard=guard)
                if st_c < st_v - guard:
                    stats["sentinel_regression"] = True
                    return CandidateOutcome(
                        node_id, NodeStatus.pruned_at_screen, stats=stats
                    )
            if fragile:
                fc_p, fc_n = focused_counts(cand_probe, fragile)
                fv_p, fv_n = focused_counts(base, fragile)
                p_worse = fisher_one_sided(fv_p, fv_n, fc_p, fc_n)
                stats.update(sent_fragile_p_worse=p_worse)
                frag_c = fc_p / (fc_p + fc_n) if (fc_p + fc_n) else 0.0
                frag_v = fv_p / (fv_p + fv_n) if (fv_p + fv_n) else 0.0
                if frag_c < frag_v and p_worse < self.alpha:
                    stats["sentinel_regression"] = True
                    return CandidateOutcome(
                        node_id, NodeStatus.pruned_at_screen, stats=stats
                    )

        # Wide-pass cull: only a probe that is SIGNIFICANTLY worse than the
        # baseline on the WHY subset is pruned without a full run. Slightly-low
        # or indistinguishable probes advance (SOP: slightly-below does not eliminate).
        if focused and stats["foc_c"] < stats["foc_v"] and stats["fisher_p_worse"] < self.alpha:
            stats["pruned_significantly_worse"] = True
            return CandidateOutcome(node_id, NodeStatus.pruned_at_screen, stats=stats)

        confirm = ctx.eval(ctx.node, ctx.train_task_ids, self.k, confirm_job_name(node_id))
        fired = ctx.fired_source(ctx.node, ctx.train_task_ids) if ctx.fired_source else None
        gate = run_gates(
            candidate_evals=confirm,
            control_evals=ctx.baseline.evals,
            task_ids=ctx.train_task_ids,
            fired_tasks=fired,
            z_threshold=self.z_threshold,
        )
        cand_mean = train_mean(confirm, ctx.train_task_ids)
        lift = cand_mean - ctx.baseline.mean
        stats["full_lift"] = lift
        promoted = gate.promoted and lift >= self.min_confirm_lift
        status = (
            NodeStatus.promoted_to_baseline if promoted else NodeStatus.pruned_at_confirm
        )
        return CandidateOutcome(
            node_id, status, score=cand_mean, confirm_evals=confirm,
            paired=gate.paired, gate=gate, stats=stats,
        )


__all__ = [
    "PairedTwoSigmaGate",
    "FocusedFisherGate",
    "confirm_job_name",
    "CONFIRM_JOB_SUFFIX",
]
