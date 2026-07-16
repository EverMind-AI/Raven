"""The seven-step funnel as a finite-state machine.

This is the layer the SOP used to delegate to a long, high-compliance Claude
session. Here the control flow is code: the round loop, the per-candidate fork,
parent selection, and the stop decision. Everything bench-specific is bundled in
an injected :class:`~raven.evolver.orchestrator.scoring.EvalBackend`; the
per-candidate decision (screen -> confirm -> promote) and the control arm are
injected as a :class:`GatePolicy` and a :class:`BaselineProvider`. So a weaker
driver model (Qwen / Kimi) can run the loop without remembering the funnel's
shape, and SWE-bench / AppWorld / a no-benchmark LLM judge all share one loop —
they differ only in which backend + policy + baseline get wired in.

Two per-round signals feed termination, and they are NOT the same thing:

- ``promoted`` — the parent changed: some candidate passed the gate against its
  round baseline AND beat the incumbent parent's train score (the Alg.1 L135
  argmax). A gate-passer that loses the argmax banks but does not take over.
- ``beat_vanilla`` — a candidate's full-train confirm beat the FIXED vanilla
  cold-start mean. This is the SOP's patience signal (no candidate's train mean
  beats vanilla for N consecutive rounds), measured against vanilla for every
  benchmark regardless of which
  baseline provider gates promotion. A round that erred out entirely sets
  ``errored`` instead and burns neither counter (it has its own stop).

The semantic steps are still injected callables:

- ``diagnose_fn`` (①): read the last child's trajectories, return a failure map.
- ``design_fn``   (②): pick WHYs and design candidates (:class:`AppliedPatch`).
- ``preflight_fn`` (③, optional): drop inert candidates; default keeps all.
- ``apply_fn``    (④): apply a patch on the parent, persist a child node.
- ``verdict_fn``  (⑦, optional): draft a per-round verdict for the findings log.

⑤/⑥ (screen/confirm/gate) are the ``gate_policy``'s job; the control arm each
round comes from the ``baseline_provider`` (frozen cold-start by default, or the
methodology-correct same-session provider). ``focused_source`` supplies a
candidate's WHY subset (AppWorld's Fisher gate); ``outcome_hook`` lets a bench
learn across rounds (AppWorld's attempt history), and ``inert_hook`` feeds it
the preflight-pruned candidates that never reach a DecisionContext. All
default off.

On-disk state under ``config.work_dir`` (all best-effort, all resume-safe):
``failure_map.json`` (the cross-round live map), ``nodes/<node_id>.json`` (the
node ledger: identity + git anchor + final status + gate stats, one file per
candidate), and ``findings.md`` (a human-readable per-round log with the
driver's verdict). The round journal the caller passes to :meth:`run` is the
loop-progress record those three complement.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional, Protocol

if TYPE_CHECKING:
    from raven.evolver.orchestrator.state.journal import RoundJournal

from raven.evolver.orchestrator.archive import (
    CellElite,
    GsmeArchive,
    describe_candidate,
)
from raven.evolver.orchestrator.config import OrchestratorConfig
from raven.evolver.orchestrator.gates.fisher import train_mean
from raven.evolver.orchestrator.gates.policy import (
    BaselineProvider,
    CandidateOutcome,
    DecisionContext,
    FiredSourceFn,
    FocusedSourceFn,
    FrozenColdStartBaseline,
    GatePolicy,
)
from raven.evolver.orchestrator.gates.strategies import PairedTwoSigmaGate
from raven.evolver.orchestrator.nodes.diagnose import merge_failure_maps
from raven.evolver.orchestrator.scoring import EvalBackend, TaskEval, flip_summary
from raven.evolver.orchestrator.sealed.runner import assert_no_test_leak
from raven.evolver.orchestrator.termination import TerminationTracker
from raven.evolver.scheduler.anchor_selection import AnchorSelection
from raven.evolver.tree.node import AppliedPatch, HarnessNode, NodeStatus


class DiagnoseFn(Protocol):
    def __call__(self, round_index: int, parent: HarnessNode) -> dict: ...


class DesignFn(Protocol):
    def __call__(
        self, round_index: int, failure_map: dict, parent: HarnessNode
    ) -> list[AppliedPatch]: ...


class ApplyFn(Protocol):
    def __call__(
        self, parent_id: str, patch: AppliedPatch, round_index: int
    ) -> HarnessNode: ...


# (candidate, parent) -> keep? The parent gives preflight its historical
# corpus (the trajectories a trigger predicate is checked against).
PreflightFn = Callable[[AppliedPatch, HarnessNode], bool]
VerdictFn = Callable[["RoundResult"], str]
OutcomeHook = Callable[[DecisionContext, CandidateOutcome], None]

# (candidate, outcome) for a preflight-pruned candidate: it was never applied,
# so there is no node/DecisionContext — the raw candidate is all there is.
InertHook = Callable[[Any, CandidateOutcome], None]
# Materialise a cell elite's edit onto the parent as a bench candidate; None =
# the pairing cannot be built (commit gone / nothing to stack) -> skip it.
RecombineFn = Callable[[HarnessNode, CellElite], Optional[object]]


@dataclass
class RoundResult:
    round_index: int
    parent_id: str
    next_parent_id: str
    promoted: bool
    outcomes: list[CandidateOutcome] = field(default_factory=list)
    verdict: Optional[str] = None
    # SOP patience signal: some candidate's full-train confirm beat the FIXED
    # vanilla mean this round (independent of the gate's own baseline).
    beat_vanilla: bool = False
    # Every candidate/phase erred — no real decision was made this round.
    errored: bool = False
    # Recorded for the post-hoc sealed unseal (C3, approach B): the deliverable
    # harness's commit + its train pass@1, so its test curve is reconstructable
    # after evolution without any decision-time test scoring.
    next_parent_sha: Optional[str] = None
    next_parent_train: Optional[float] = None


@dataclass
class RunResult:
    rounds: list[RoundResult] = field(default_factory=list)
    stop_reason: Optional[str] = None
    final_parent_id: Optional[str] = None
    resumed_rounds: int = 0  # rounds replayed from a journal, not re-run


def summarize_round(rr: RoundResult) -> str:
    """Factual one-round summary for the verdict draft / findings log."""
    lines = [f"round {rr.round_index}: parent={rr.parent_id} promoted={rr.promoted}"]
    for o in rr.outcomes:
        parts = [f"  {o.node_id}: {o.status.value}"]
        if o.screen is not None:
            parts.append(
                f"screen={o.screen.candidate_mean:.3f} vs van "
                f"{o.screen.vanilla_mean:.3f} ({o.screen.bucket})"
            )
        if o.paired is not None:
            parts.append(
                f"confirm={o.paired.candidate_mean:.3f} vs van "
                f"{o.paired.control_mean:.3f} z={o.paired.z:.2f} "
                f"credited={o.paired.credited_2sigma}"
            )
        if o.stats:
            parts.append(" ".join(f"{k}={v}" for k, v in o.stats.items()))
        lines.append(" ".join(parts))
    return "\n".join(lines)


def _sha_or_none(sha: Optional[str]) -> Optional[str]:
    """A journal-safe commit SHA: the root shim's ``"unknown"`` placeholder is
    recorded as None so the post-hoc unseal never tries to check it out."""
    return None if sha in (None, "", "unknown") else sha


def _vanilla_control(vanilla_stability) -> dict[str, TaskEval]:
    """The cold-start baseline as an eval map to serve as the control arm."""
    return {
        tid: TaskEval(task_id=tid, passes=st.passes, attempts=st.attempts)
        for tid, st in vanilla_stability.items()
    }


class EvolutionOrchestrator:
    """Drives the seven-step funnel across rounds until a stop condition fires."""

    def __init__(
        self,
        config: OrchestratorConfig,
        *,
        backend: EvalBackend,
        diagnose_fn: DiagnoseFn,
        design_fn: DesignFn,
        apply_fn: ApplyFn,
        gate_policy: Optional[GatePolicy] = None,
        baseline_provider: Optional[BaselineProvider] = None,
        preflight_fn: Optional[PreflightFn] = None,
        verdict_fn: Optional[VerdictFn] = None,
        fired_source: Optional[FiredSourceFn] = None,
        focused_source: Optional[FocusedSourceFn] = None,
        outcome_hook: Optional[OutcomeHook] = None,
        inert_hook: Optional[InertHook] = None,
        seed_failure_map: Optional[dict] = None,
        archive: Optional[GsmeArchive] = None,
        recombine_fn: Optional[RecombineFn] = None,
    ) -> None:
        self._cfg = config
        self._backend = backend
        self._diagnose = diagnose_fn
        self._design = design_fn
        self._apply = apply_fn
        self._eval = backend.eval
        self._preflight = preflight_fn or (lambda _patch, _parent: True)
        self._verdict = verdict_fn
        self._fired_source = fired_source
        self._focused_source = focused_source
        self._outcome_hook = outcome_hook
        self._inert_hook = inert_hook
        # GSME: the per-cell elite bank + the cross-cell recombiner. The archive
        # loads its persisted state itself, so a resumed run keeps its elites.
        self._archive = archive
        self._recombine = recombine_fn

        self._vanilla_stability = backend.cold_start()
        if not self._vanilla_stability:
            raise ValueError("backend.cold_start() returned an empty baseline")
        self._train_task_ids = list(backend.train_task_ids) or sorted(self._vanilla_stability)
        self._sentinel_task_ids = self._sample_sentinels(config.anchor.n_sentinel)
        # The FIXED comparison anchor for the patience signal (SOP: candidate
        # train mean vs VANILLA, never vs the previous round's parent) — the
        # same for every benchmark no matter which baseline provider gates
        # promotion.
        self._vanilla_train_mean = train_mean(
            _vanilla_control(self._vanilla_stability), self._train_task_ids
        )

        # Default policy = the SWE paired-2σ line; default baseline = frozen
        # cold-start (cost-bound; cross-time-invalid — see gates.policy). AppWorld
        # / same-session runs inject their own.
        self._gate: GatePolicy = gate_policy or PairedTwoSigmaGate(
            k_screen=config.k_screen, k_confirm=config.k_confirm
        )
        self._baselines: BaselineProvider = baseline_provider or FrozenColdStartBaseline(
            _vanilla_control(self._vanilla_stability)
        )

        # Sealed-test iron law as a mechanism: fail loudly if a held-out id has
        # crept into the anchor or train sets (SOP §0).
        if backend.test_task_ids:
            assert_no_test_leak(
                anchor_task_ids=self.select_anchor().task_ids,
                train_task_ids=self._train_task_ids,
                sealed_test_ids=list(backend.test_task_ids),
            )

        # Cross-round live failure map (SOP §2 ①): accumulated, not frozen.
        # ``seed_failure_map`` pre-populates it (taxonomy induction's seed, with
        # the root in ``_diagnosed_parents`` so round 1 skips re-judging the very
        # trajectories induction judged); a journal resume overrides it from disk.
        self._failure_map: dict = dict(seed_failure_map) if seed_failure_map else {}

        # Real applied nodes by id (seeded with the root when run() gets one).
        # A promoted parent must resolve to its real node — with its commit SHA
        # and patch — not a shim, or a same-session baseline / worktree eval
        # would check out the wrong (or an "unknown") commit.
        self._node_registry: dict[str, HarnessNode] = {}
        # Ledger metadata for non-AppliedPatch candidates (the wired bench
        # lines), where node.patch is None and the WHERE/WHY/files/activation
        # info would otherwise never reach the node record.
        self._cand_meta: dict[str, dict] = {}

    @property
    def vanilla_train_mean(self) -> float:
        """The fixed vanilla train anchor (benches read it at unseal time)."""
        return self._vanilla_train_mean

    def _sample_sentinels(self, n: int) -> list[str]:
        """Deterministic default sentinel set (used when no node id is at hand)."""
        stable, fragile = self._sentinel_pools()
        return stable[: n - n // 2] + fragile[: n // 2]

    def _sentinel_pools(self) -> tuple[list[str], list[str]]:
        from raven.evolver.analysis.stability_bucket import StabilityBucket

        train = set(self._train_task_ids)
        stable, fragile = [], []
        for tid, st in self._vanilla_stability.items():
            if tid not in train:
                continue
            if st.bucket == StabilityBucket.STABLE_PASS:
                stable.append(tid)
            elif st.bucket in (
                StabilityBucket.BORDERLINE_2_3, StabilityBucket.BORDERLINE_1_3
            ):
                fragile.append(tid)
        return sorted(stable), sorted(fragile)

    def _sentinels_for(self, node_id: str, n: int) -> list[str]:
        """Per-candidate regression controls: half stable-pass, half borderline.

        Stratified because over-trigger regressions concentrate on BORDERLINE
        tasks (fragile passes flip first) — a stable-only sentinel set is
        systematically blind to them (observed live: a candidate with a 58%
        passing-task regression rate sailed through 3 stable sentinels).
        Rotated per candidate (hash of node_id) so no fixed control set can be
        sailed past twice."""
        import hashlib

        stable, fragile = self._sentinel_pools()

        def pick(pool: list[str], m: int, salt: str) -> list[str]:
            if not pool or m <= 0:
                return []
            h = int(hashlib.sha256(f"{salt}:{node_id}".encode()).hexdigest(), 16)
            start = h % len(pool)
            return [pool[(start + i) % len(pool)] for i in range(min(m, len(pool)))]

        return pick(stable, n - n // 2, "stable") + pick(fragile, n // 2, "fragile")

    def select_anchor(self, affinity: dict[str, float] | None = None) -> AnchorSelection:
        return self._backend.anchor(affinity)

    def run(
        self,
        root_node_id: str,
        journal: Optional["RoundJournal"] = None,
        *,
        root_node: Optional[HarnessNode] = None,
    ) -> RunResult:
        """Run rounds from ``root_node_id`` (vanilla) until termination.

        If ``journal`` is given, previously-completed rounds are replayed to seed
        the termination counters, current parent, and round index, and the loop
        continues from the next round — a killed run resumes without re-running
        the evals it already did. Replay also re-registers each promoted
        parent's recorded commit SHA, so a resumed round's design / worktree
        eval / baseline fallback sees the real commit, not an "unknown" shim;
        and the accumulated failure map is re-read from disk so the cross-round
        live map (SOP §2 ①) is not truncated back to empty.

        ``root_node`` (optional) registers the real vanilla node so a round whose
        parent is the root can resolve its commit SHA (needed by same-session
        baselines / worktree evals); without it the root falls back to a shim.
        """
        term = TerminationTracker(
            patience=self._cfg.termination.patience,
            max_rounds=self._cfg.termination.max_rounds,
            max_consecutive_errors=self._cfg.termination.max_consecutive_errors,
        )
        result = RunResult()
        parent_id = root_node_id
        # The incumbent's train score — the argmax bar a challenger must beat to
        # take over as parent (Alg.1 L135). The root's score is vanilla's mean.
        parent_score = self._vanilla_train_mean
        round_index = 0
        if root_node is not None:
            self._node_registry[root_node.node_id] = root_node

        if journal is not None:
            records = journal.load()
            for rec in records:
                term.record_round(
                    promoted=rec.get("beat_vanilla", rec["promoted"]),
                    errored=rec.get("errored", False),
                )
                round_index = rec["round_index"]
                parent_id = rec["next_parent_id"]
                if rec.get("next_parent_train") is not None:
                    parent_score = rec["next_parent_train"]
                sha = rec.get("next_parent_sha")
                if (
                    sha
                    and rec["next_parent_id"] != rec["parent_id"]
                    and rec["next_parent_id"] not in self._node_registry
                ):
                    self._node_registry[rec["next_parent_id"]] = HarnessNode(
                        node_id=rec["next_parent_id"],
                        parent_id=rec["parent_id"],
                        git_commit_sha=sha,
                        git_branch="journal-resume",
                        created_at=HarnessNode.utc_now(),
                        created_at_iter=rec["round_index"],
                    )
                result.resumed_rounds += 1
            if records:
                self._reload_failure_map()
            stop, reason = term.should_stop()
            if stop:
                result.stop_reason = reason
                result.final_parent_id = parent_id
                return result

        while True:
            round_index += 1
            round_result = self._run_round(round_index, parent_id, parent_score)
            result.rounds.append(round_result)
            if journal is not None:
                journal.append(round_result)
            self._persist_node_records(round_result)
            self._append_findings(round_result)
            if self._archive is not None:
                self._archive.save()
            parent_id = round_result.next_parent_id
            if round_result.promoted and round_result.next_parent_train is not None:
                parent_score = round_result.next_parent_train

            term.record_round(
                promoted=round_result.beat_vanilla, errored=round_result.errored
            )
            stop, reason = term.should_stop()
            if stop:
                result.stop_reason = reason
                break

        result.final_parent_id = parent_id
        return result

    def _run_round(
        self, round_index: int, parent_id: str, parent_score: float
    ) -> RoundResult:
        # Gate0 (SOP §0, before any scoring): verify the environment before scoring anything
        # this round. A dirty env (sandbox down / network unroutable / verifier
        # can't emit results) makes every score invalid, so let it raise — fix
        # the box, then resume. Benches with no precheck wired skip this.
        if self._backend.precheck is not None:
            self._backend.precheck()
        parent = self._load_parent(parent_id)
        anchor = self.select_anchor()
        # The control arm may itself be an eval (same-session pairing) or a disk
        # rebuild — a transient failure here is round-scoped like any other eval
        # failure: record an errored round and let the error counter decide,
        # instead of aborting an unattended run with no journal record.
        try:
            baseline = self._baselines.for_round(
                round_index, parent, eval=self._eval,
                train_task_ids=self._train_task_ids, anchor=anchor,
            )
        except Exception as exc:  # noqa: BLE001 — record + continue, don't abort
            outcome = CandidateOutcome(
                f"r{round_index}-baseline", NodeStatus.errored,
                stats={"phase": "baseline", "error": repr(exc)},
            )
            return RoundResult(
                round_index=round_index, parent_id=parent_id,
                next_parent_id=parent_id, promoted=False,
                outcomes=[outcome], errored=True,
                next_parent_sha=_sha_or_none(parent.git_commit_sha),
            )

        # ① diagnose -> merge into the cross-round live failure map, ② design,
        # ③ preflight prune. A parent already diagnosed in an earlier round (no
        # promotion since) is NOT re-diagnosed: its trajectories haven't changed,
        # re-judging them would only re-spend the driver and double-count the
        # same failures in the accumulated map. A diagnose/design failure must
        # not abort the whole run (same discipline as the per-candidate catch
        # below): record the reason on an errored outcome and finish the round
        # with no candidates; the tracker's error counter stops a persistent
        # outage with an honest reason instead of burning patience.
        outcomes: list[CandidateOutcome] = []
        try:
            diagnosed = set(self._failure_map.get("_diagnosed_parents") or [])
            if parent_id not in diagnosed:
                round_map = self._diagnose(round_index, parent)
                self._failure_map = merge_failure_maps(self._failure_map, round_map)
                self._failure_map["_diagnosed_parents"] = sorted(diagnosed | {parent_id})
                self._persist_failure_map()
            candidates = []
            for i, c in enumerate(self._design(round_index, self._failure_map, parent)):
                if self._preflight(c, parent):
                    candidates.append(c)
                else:
                    # ③ zero-inference prune, recorded (never silently dropped):
                    # a pruned-inert candidate is a real decision this round.
                    outcome = CandidateOutcome(
                        f"r{round_index}-preflight{i}", NodeStatus.pruned_inert,
                        stats={
                            "phase": "preflight",
                            "why": str(getattr(c, "why", "")),
                            "reason": "trigger has zero historical hits",
                        },
                    )
                    outcomes.append(outcome)
                    if self._inert_hook is not None:
                        try:
                            self._inert_hook(c, outcome)
                        except Exception:  # noqa: BLE001 — advisory learning hook, non-fatal
                            pass
        except Exception as exc:  # noqa: BLE001 — record + continue, don't abort
            outcomes.append(
                CandidateOutcome(
                    f"r{round_index}-design", NodeStatus.errored,
                    stats={"phase": "diagnose_design", "error": repr(exc)},
                )
            )
            candidates = []

        # GSME cross-cell recombination: stack elites from cells the parent's
        # lineage has not covered onto the parent, as ordinary candidates through
        # the same apply -> gate pipeline. Deliberately OUTSIDE the design
        # try/except — a driver outage does not stop deterministic recombination.
        if self._archive is not None and self._recombine is not None:
            try:
                elites = self._archive.eligible_elites(
                    parent_id, limit=self._cfg.budget.recombinations_per_round
                )
                for elite in elites:
                    recomb = self._recombine(parent, elite)
                    if recomb is None:
                        self._archive.record_pairing(
                            parent_id, elite.node_id, "recombine_failed"
                        )
                        continue
                    candidates.append(recomb)
            except Exception as exc:  # noqa: BLE001 — record + continue
                outcomes.append(
                    CandidateOutcome(
                        f"r{round_index}-recombine", NodeStatus.errored,
                        stats={"phase": "recombine", "error": repr(exc)},
                    )
                )

        best_node_id: Optional[str] = None
        best_score = -1.0

        for idx, patch in enumerate(candidates):
            # A single candidate's apply/eval crash must not sink the round: catch
            # it, record the reason on an ``errored`` outcome, and move on (C).
            elite_id = getattr(patch, "elite_node_id", None)
            try:
                node = self._apply(parent_id, patch, round_index)  # ④
            except Exception as exc:  # noqa: BLE001 — record + skip, don't abort
                if elite_id and self._archive is not None:
                    self._archive.record_pairing(parent_id, elite_id, "errored")
                outcomes.append(
                    CandidateOutcome(
                        f"r{round_index}-cand{idx}", NodeStatus.errored,
                        stats={"phase": "apply", "error": repr(exc)},
                    )
                )
                continue
            self._node_registry[node.node_id] = node
            meta = describe_candidate(patch)
            if meta:
                self._cand_meta[node.node_id] = meta
            ctx = DecisionContext(
                node=node,
                parent_id=parent_id,
                round_index=round_index,
                eval=self._eval,
                baseline=baseline,
                train_task_ids=self._train_task_ids,
                anchor=anchor,
                focused_task_ids=(
                    self._focused_source(node) if self._focused_source else []
                ),
                sentinel_task_ids=self._sentinels_for(
                    node.node_id, self._cfg.anchor.n_sentinel
                ),
                fired_source=self._fired_source,
            )
            try:
                outcome = self._gate.decide(ctx)  # ⑤⑥ delegated to the policy
            except Exception as exc:  # noqa: BLE001 — record + skip, don't abort
                if elite_id and self._archive is not None:
                    self._archive.record_pairing(parent_id, elite_id, "errored")
                outcomes.append(
                    CandidateOutcome(
                        node.node_id, NodeStatus.errored,
                        stats={"phase": "decide", "error": repr(exc)},
                    )
                )
                continue
            if elite_id:
                outcome.stats["recombination_of"] = elite_id
            # Flip table (SOP §2 ①: which tasks flipped): rescued/regressed vs
            # this round's control, recorded on the ledger and the live failure
            # map so the next diagnose/design sees CAUSAL feedback, not just the
            # static failure set.
            if outcome.confirm_evals:
                flips = flip_summary(
                    outcome.confirm_evals, baseline.evals, self._train_task_ids
                )
                outcome.stats["flips"] = flips
                self._failure_map.setdefault("_flips", {})[node.node_id] = {
                    "round": round_index, "vs": baseline.label, **flips
                }
            outcomes.append(outcome)
            if self._archive is not None:
                try:
                    self._archive.consider(
                        parent_id=parent_id, node=node, cand=patch,
                        outcome=outcome, round_index=round_index,
                        vanilla_train_mean=self._vanilla_train_mean,
                    )
                except Exception:  # noqa: BLE001 — bank-keeping must not sink a round
                    pass
            if self._outcome_hook is not None:
                try:
                    self._outcome_hook(ctx, outcome)
                except Exception:  # noqa: BLE001 — advisory learning hook, non-fatal
                    pass
            if outcome.promoted:
                self._baselines.on_promote(
                    node, outcome, train_task_ids=self._train_task_ids
                )
                if outcome.score > best_score:
                    best_score = outcome.score
                    best_node_id = node.node_id

        if any(o.confirm_evals for o in outcomes):
            self._persist_failure_map()  # the round's flips joined the live map

        # ⑦ select parent — Alg.1 L135 argmax: the round's best gate-passer takes
        # over only when it beats the incumbent's train score; a tie keeps the
        # incumbent (no churn without improvement). A gate-passer that loses the
        # argmax still banks (status/archive) — it just doesn't become parent.
        # Under the ratcheted baseline the gate already implies this; under a
        # frozen-vanilla control this keeps the champion chain monotone.
        promoted = best_node_id is not None and best_score > parent_score
        next_parent_id = best_node_id if promoted else parent_id
        next_parent_node = self._node_registry.get(next_parent_id) or parent
        beat_vanilla = any(
            o.confirm_evals and o.score > self._vanilla_train_mean for o in outcomes
        )
        errored = bool(outcomes) and all(
            o.status is NodeStatus.errored for o in outcomes
        )

        round_result = RoundResult(
            round_index=round_index,
            parent_id=parent_id,
            next_parent_id=next_parent_id,
            promoted=promoted,
            outcomes=outcomes,
            beat_vanilla=beat_vanilla,
            errored=errored,
            next_parent_sha=_sha_or_none(next_parent_node.git_commit_sha),
            next_parent_train=(best_score if promoted else None),
        )
        if self._verdict is not None:
            round_result.verdict = self._verdict(round_result)
        return round_result

    def _persist_failure_map(self) -> None:
        """Write the accumulated failure map for audit/resume (best-effort)."""
        try:
            path = self._cfg.failure_map_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self._failure_map, indent=2))
        except OSError:
            pass

    def _reload_failure_map(self) -> None:
        """Re-read the accumulated failure map from disk (resume path)."""
        try:
            path = self._cfg.failure_map_path
            if path.exists():
                self._failure_map = json.loads(path.read_text())
        except (OSError, ValueError):
            pass

    def _persist_node_records(self, rr: RoundResult) -> None:
        """Write the node ledger (SOP §3.1): one JSON per candidate under
        ``work_dir/nodes/``, carrying identity + git anchor + final status +
        gate stats. Best-effort — the ledger is the audit trail, not control
        state (resume runs off the journal)."""
        try:
            ndir = self._cfg.nodes_dir
            ndir.mkdir(parents=True, exist_ok=True)
            for o in rr.outcomes:
                node = self._node_registry.get(o.node_id)
                if node is None:  # errored pseudo-candidates never got a node
                    continue
                patch = node.patch
                patch_to_dict = getattr(patch, "to_dict", None)
                rec = {
                    "node_id": node.node_id,
                    "parent_id": node.parent_id,
                    "git_commit_sha": node.git_commit_sha,
                    "git_branch": node.git_branch,
                    "created_at": node.created_at,
                    "created_at_iter": node.created_at_iter,
                    "patch": (
                        patch_to_dict() if callable(patch_to_dict)
                        else (repr(patch) if patch is not None else None)
                    ),
                    "status": o.status.value,
                    "round_index": rr.round_index,
                    "score": o.score,
                }
                if o.node_id in self._cand_meta:
                    rec["candidate"] = self._cand_meta[o.node_id]
                if o.screen is not None:
                    rec["screen"] = dataclasses.asdict(o.screen)
                if o.paired is not None:
                    rec["paired"] = dataclasses.asdict(o.paired)
                if o.stats:
                    rec["stats"] = o.stats
                (ndir / f"{o.node_id}.json").write_text(
                    json.dumps(rec, indent=2, default=str)
                )
        except OSError:
            pass

    def _append_findings(self, rr: RoundResult) -> None:
        """Append the per-round findings-log entry (SOP §3 layer 1) to
        ``work_dir/findings.md`` — the human-readable record of what each round
        tried, what the gates said, and the driver's verdict. Best-effort."""
        try:
            path = self._cfg.findings_path
            path.parent.mkdir(parents=True, exist_ok=True)
            block = [f"\n## round {rr.round_index}\n", "```", summarize_round(rr), "```"]
            if rr.verdict:
                block.append(f"\nverdict: {rr.verdict}")
            with path.open("a") as f:
                f.write("\n".join(block) + "\n")
        except OSError:
            pass

    def _load_parent(self, parent_id: str) -> HarnessNode:
        # A promoted parent was applied in an earlier round (or re-registered
        # from the journal on resume), so it is in the registry with its real
        # commit SHA; return that. Only the root (never applied) falls back to a
        # shim — pass ``root_node`` to run() to give it a real SHA too.
        node = self._node_registry.get(parent_id)
        if node is not None:
            return node
        return HarnessNode(
            node_id=parent_id,
            parent_id=None,
            git_commit_sha="unknown",
            git_branch="unknown",
            created_at=HarnessNode.utc_now(),
            created_at_iter=0,
        )


__all__ = [
    "EvolutionOrchestrator",
    "CandidateOutcome",
    "RoundResult",
    "RunResult",
    "DiagnoseFn",
    "DesignFn",
    "ApplyFn",
    "PreflightFn",
    "VerdictFn",
    "InertHook",
    "OutcomeHook",
    "RecombineFn",
    "summarize_round",
]
