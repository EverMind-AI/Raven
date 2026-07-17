"""Production wiring — turn the injectable FSM into a runnable orchestrator.

:class:`EvolutionOrchestrator` takes one :class:`EvalBackend` plus injected
semantic steps, so this module supplies the real ones:

- ``make_diagnose_fn`` / ``make_design_fn`` / ``make_verdict_fn`` bind a driver
  ``call_fn`` (from :mod:`.providers.openai_compat`) to the semantic nodes.
- ``make_llm_backend`` builds the one bench-neutral :class:`EvalBackend`: an LLM
  judges each trajectory pass/fail with no external verifier at all. The
  concrete benchmark backends (AppWorld, EvoAgentBench) are built by their own
  ``make_*_backend`` factories under ``benchmarks.<bench>.evolve`` — the
  orchestrator core names no benchmark. All emit the same ``dict[str, TaskEval]``
  contract, so the loop stays bench-agnostic.
- ``make_metadata_apply_fn`` records a child node without touching git (replay /
  dry runs); a real run passes ``EvolverTreeStore.create_child_node``.

``build_orchestrator`` assembles these into an :class:`EvolutionOrchestrator`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from raven.evolver.analysis.stability_bucket import (
    TaskStability,
    _bucket_for,
)
from raven.evolver.judge.llm_client import JudgeLLMBackend
from raven.evolver.judge.parser import JudgeParseError, parse_pass_fail
from raven.evolver.judge.prompts import build_pass_fail_messages
from raven.evolver.orchestrator.config import Budget, OrchestratorConfig
from raven.evolver.orchestrator.loop import (
    EvolutionOrchestrator,
    RoundResult,
    summarize_round,
)
from raven.evolver.orchestrator.nodes.design import design_round
from raven.evolver.orchestrator.nodes.diagnose import diagnose_round
from raven.evolver.orchestrator.nodes.semantic import CallFn, SemanticNode
from raven.evolver.orchestrator.nodes.verdict import draft_verdict
from raven.evolver.orchestrator.scoring import (
    EvalBackend,
    TaskEval,
    TrajectorySource,
)
from raven.evolver.scheduler.anchor_selection import simple_anchor
from raven.evolver.tree.node import AppliedPatch, HarnessNode

# per-task trajectory runner for the no-benchmark scorer:
# (node, task_ids, k) -> [(task_id, trajectory_id, task_description, text), ...]
ScoringTrajectoryRun = Callable[[HarnessNode, list, int], list]


@dataclass(frozen=True)
class EndpointConfig:
    """One OpenAI-compatible driver endpoint."""

    base_url: str
    model: str
    max_tokens: int = 8192
    temperature: float = 0.0


# ---- semantic step adapters -------------------------------------------------


def make_diagnose_fn(
    call_fn: CallFn,
    trajectory_source: TrajectorySource,
    *,
    min_why_classes: int = 7,
) -> Callable[[int, HarnessNode], dict]:
    """Bind a driver + a trajectory source into the loop's ``diagnose_fn``."""

    def diagnose_fn(round_index: int, parent: HarnessNode) -> dict:
        trajectories = trajectory_source(round_index, parent)
        return diagnose_round(call_fn, trajectories, min_why_classes=min_why_classes)

    return diagnose_fn


def make_design_fn(
    call_fn: CallFn,
    budget: Budget,
    *,
    file_context_for: Any = None,
    parent_summary_of: Optional[Callable[[HarnessNode], str]] = None,
    archive_summary_of: Optional[Callable[[], str]] = None,
) -> Callable[[int, dict, HarnessNode], list[AppliedPatch]]:
    def design_fn(round_index: int, failure_map: dict, parent: HarnessNode) -> list[AppliedPatch]:
        parent_summary = parent_summary_of(parent) if parent_summary_of else parent.node_id
        return design_round(
            call_fn,
            failure_map,
            budget,
            parent_summary=parent_summary,
            file_context_for=file_context_for,
            archive_summary=archive_summary_of() if archive_summary_of else "",
        )

    return design_fn


def make_verdict_fn(
    call_fn: CallFn,
    *,
    why_keys_of: Optional[Callable[[], Optional[list[str]]]] = None,
) -> Callable[[RoundResult], str]:
    """``why_keys_of`` is a lazy getter: with taxonomy induction the WHY keys
    only exist after the first diagnose, which still precedes round 1's verdict."""
    past: list[str] = []

    def verdict_fn(rr: RoundResult) -> str:
        summary = summarize_round(rr)
        history = "\n".join(past[-5:])
        past.append(summary)
        try:
            v = draft_verdict(
                call_fn,
                round_index=rr.round_index,
                round_summary=summary,
                history=history,
                why_keys=why_keys_of() if why_keys_of else None,
            )
            return f"{v.summary} | next: {v.next_target} | ceiling={v.ceiling_signal}"
        except Exception:  # noqa: BLE001 — verdict is advisory; fall back to facts
            return summary

    return verdict_fn


# ---- backend factory (the one bench-neutral EvalBackend) --------------------


def make_llm_backend(
    judge_backend: JudgeLLMBackend,
    run_trajectories: ScoringTrajectoryRun,
    *,
    train_task_ids: list[str],
    vanilla_node: HarnessNode,
    test_task_ids: list[str] = (),
    k: int = 3,
    max_tokens: int = 1024,
    cull_sigma_mult: float = 1.5,
) -> EvalBackend:
    """No-benchmark backend: an LLM judges each trajectory pass/fail.

    ``run_trajectories(node, task_ids, k)`` yields ``(task_id, trajectory_id,
    task_description, text)`` — the same trajectory machinery that feeds
    diagnosis, here also driving scoring. Verdicts aggregate to
    ``TaskEval(passes, attempts=K)``; ``infra_attempts`` is 0 so Gate-f is a
    no-op and every downstream consumer is unchanged.
    """

    def call_fn(messages):
        return asyncio.run(judge_backend.call(messages, max_tokens=max_tokens))

    verdict_node: SemanticNode = SemanticNode(
        name="pass_fail",
        call_fn=call_fn,
        parse_fn=parse_pass_fail,
        parse_error_types=(JudgeParseError,),
    )

    def _score(node, task_ids, k_):
        passes: dict[str, int] = {}
        attempts: dict[str, int] = {}
        for task_id, traj_id, desc, text in run_trajectories(node, list(task_ids), k_):
            attempts[task_id] = attempts.get(task_id, 0) + 1
            verdict = verdict_node.run(build_pass_fail_messages(desc, text, trajectory_id=traj_id))
            if verdict.passed:
                passes[task_id] = passes.get(task_id, 0) + 1
        return {t: TaskEval(t, passes.get(t, 0), n) for t, n in attempts.items()}

    def eval_fn(node, task_ids, k_, job_name, *, split="train"):
        return _score(node, task_ids, k_)

    # One judge-scored vanilla pass, shared by cold_start AND anchor — scoring
    # twice would double the judge cost and, worse, derive the screen control
    # and the anchor thresholds from two different samples.
    _stab: dict[str, TaskStability] = {}

    def cold_start() -> dict[str, TaskStability]:
        if not _stab:
            evals = _score(vanilla_node, list(train_task_ids), k)
            _stab.update(
                {
                    t: TaskStability(t, ev.attempts, ev.passes, _bucket_for(ev.passes, ev.attempts))
                    for t, ev in evals.items()
                }
            )
        return dict(_stab)

    def anchor(affinity=None):
        return simple_anchor(cold_start(), cull_sigma_mult=cull_sigma_mult)

    def trajectories(round_index, node):
        return [(traj_id, desc, text) for _tid, traj_id, desc, text in run_trajectories(node, list(train_task_ids), 1)]

    return EvalBackend(
        train_task_ids=list(train_task_ids),
        test_task_ids=list(test_task_ids),
        eval=eval_fn,
        cold_start=cold_start,
        anchor=anchor,
        trajectories=trajectories,
    )


def make_git_commit_apply_fn(
    repo_root: str | Path,
    files_of: Callable[[Any], dict[str, bytes]],
    *,
    root_node_id: str,
    base_sha: str,
    deletions_of: Optional[Callable[[Any], list[str]]] = None,
    guard_immutable: bool = True,
    git_branch: str = "evolver/orchestrator",
    sha_by_node: Optional[dict[str, str]] = None,
    node_id_salt: Optional[str] = None,
) -> Callable[[str, Any, int], HarnessNode]:
    """Edit-then-commit apply: turn a candidate's edited files into a REAL child
    commit off the parent node's commit.

    ``files_of(patch)`` returns the candidate's full new file bytes (repo-relative
    path -> bytes); the driver may have produced them however it likes (a bash
    edit loop, a template, a diff already applied). We materialise them onto the
    parent's commit and ``commit-tree`` — so the node gets a reproducible SHA and
    git ancestry, and multi-round chaining works because each promoted node's SHA
    is tracked and its children commit off it (no "sandbox" placeholder, no live
    working-tree mutation). Immutable-kernel paths are guarded on the changed set.

    Node ids carry ``node_id_salt`` (a fresh 4-hex token per factory unless
    given): ``v{round}-c{n}-{salt}``. A crash-resumed process re-runs the
    incomplete round with NEW candidates; without the salt they would reuse the
    dead run's ids and inherit its out-dirs, session files, and
    ``refs/evolver/*`` refs — contaminating the eval with another candidate's
    artifacts. The salt makes ids (and everything derived from them) unique per
    process, and refs from concurrent/earlier runs are never repointed.
    """
    import uuid

    from raven.evolver.applier import assert_patch_allowed
    from raven.evolver.tree import git_ops

    root = Path(repo_root)
    # A shared registry lets a design step base its sandbox off a promoted
    # parent's real commit (same dict passed to both apply and design).
    if sha_by_node is None:
        sha_by_node = {}
    sha_by_node.setdefault(root_node_id, base_sha)
    salt = node_id_salt if node_id_salt is not None else uuid.uuid4().hex[:4]
    counter = {"n": 0}

    def apply_fn(parent_id: str, patch: Any, round_index: int) -> HarnessNode:
        parent_sha = sha_by_node.get(parent_id)
        if parent_sha is None:
            raise KeyError(f"unknown parent node {parent_id!r}: no commit recorded (chain broken)")
        files = files_of(patch)
        deletions = tuple(deletions_of(patch)) if deletions_of else ()
        if guard_immutable:  # guard before committing, so no dangling commit on reject
            assert_patch_allowed(list(files) + list(deletions))
        child_sha, _changed = git_ops.commit_files_as_child(
            root,
            parent_sha,
            files,
            f"evolver: round {round_index} candidate off {parent_id}",
            deletions=deletions,
        )
        counter["n"] += 1
        node_id = f"v{round_index}-c{counter['n']}-{salt}" if salt else f"v{round_index}-c{counter['n']}"
        sha_by_node[node_id] = child_sha
        # Anchor the (otherwise unreferenced) candidate commit against git gc,
        # so a late worktree eval / post-hoc sealed unseal still finds it.
        git_ops.create_ref(root, f"refs/evolver/{node_id}", child_sha)
        return HarnessNode(
            node_id=node_id,
            parent_id=parent_id,
            git_commit_sha=child_sha,
            git_branch=git_branch,
            created_at=HarnessNode.utc_now(),
            created_at_iter=round_index,
            patch=patch if isinstance(patch, AppliedPatch) else None,
        )

    return apply_fn


def make_zero_hit_preflight(trajectory_source: TrajectorySource):
    """SOP §2 ③ zero-hit prune: drop a candidate whose self-declared trigger
    regex matches NONE of the parent's failing trajectories — it would never
    fire, so screening/confirming it only burns eval budget (the "provably
    inert" preflight of the paper).

    Fail-open everywhere: no declared spec, a malformed regex, an unreadable
    corpus, or an empty corpus all keep the candidate — preflight may only
    prune on positive evidence of inertness. The regex runs over the same
    rendered trajectory text the driver read when authoring it
    (``read_trajectory``), so the predicate and its corpus are self-consistent.
    """
    import re as _re

    corpus_cache: dict[str, list[str]] = {}

    def preflight(cand, parent) -> bool:
        spec = getattr(cand, "activation_spec", None)
        if not isinstance(spec, dict) or spec.get("kind") != "trajectory_regex":
            return True
        try:
            pat = _re.compile(str(spec.get("pattern", "")), _re.M)
        except _re.error:
            return True
        texts = corpus_cache.get(parent.node_id)
        if texts is None:
            try:
                # description + transcript: the same surface read_trajectory
                # showed the driver, so a predicate authored against it matches.
                texts = [f"{t[1]}\n{t[2]}" for t in trajectory_source(0, parent)]
            except Exception:  # noqa: BLE001 — no corpus, no pruning signal
                texts = []
            corpus_cache[parent.node_id] = texts
        if not texts:
            return True
        return any(pat.search(x) for x in texts)

    return preflight


def make_git_recombine_fn(repo_root: str | Path):
    """GSME cross-cell recombination: re-materialise a cell elite's edit onto
    the current parent as an ordinary candidate.

    The wired candidates carry FULL new file bytes vs their parent commit, so
    stacking = reading the elite's changed paths back from its commit and
    handing them to the standard edit-then-commit apply off the new parent —
    the path guard, gc ref, node ledger, and gate pipeline all apply unchanged.
    Same-file overlaps never reach here (:meth:`GsmeArchive.eligible_elites`
    filters them); a missing commit/path returns None and the pairing is
    recorded as failed instead of retried every round.
    """
    from raven.evolver.orchestrator.archive import CellElite, RecombinantCandidate
    from raven.evolver.tree import git_ops
    from raven.evolver.tree.git_ops import GitOpError

    root = Path(repo_root)

    def recombine_fn(parent: HarnessNode, elite: "CellElite"):
        files: dict[str, bytes] = {}
        try:
            for rel in elite.files:
                files[rel] = git_ops.read_file_at(root, elite.git_commit_sha, rel)
        except GitOpError:
            return None
        if not files and not elite.deletions:
            return None
        return RecombinantCandidate(
            files=files,
            why=elite.why,
            cell=elite.cell,
            elite_node_id=elite.node_id,
            focused_task_ids=list(elite.focused_task_ids),
            deletions=list(elite.deletions),
            summary=(
                f"recombination: stack elite {elite.node_id} ({elite.cell}, "
                f"score {elite.score:.3f}) onto {parent.node_id}"
            ),
            has_beacon=any(rel.endswith(".py") and b"activation_beacon(" in data for rel, data in files.items()),
        )

    return recombine_fn


def make_worktree_eval_fn(
    repo_root: str | Path,
    score_worktree: Callable[..., dict[str, TaskEval]],
):
    """Eval a node by checking its commit out into an ephemeral worktree and
    scoring against that checkout — the counterpart to the edit-then-commit
    apply. ``score_worktree(worktree_path, node, task_ids, k, split)`` runs the
    bench in the clean checkout, so the live repo is never mutated (no RealPathSync)."""
    from raven.evolver.tree import git_ops

    root = Path(repo_root)

    def eval_fn(node, task_ids, k, job_name, *, split="train"):
        with git_ops.worktree_at(root, node.git_commit_sha) as wt:
            return score_worktree(wt, node, task_ids, k, split)

    return eval_fn


def make_sealed_runner(
    backend: EvalBackend,
    sealed_dir: str | Path,
    *,
    k: int = 3,
    test_task_ids: Optional[list[str]] = None,
):
    """Build the C3 sealed test runner from a backend (approach B, post-hoc).

    Reuses ``backend.eval`` itself (invoked with ``split="test"``), so the sealed
    scorer is the same worktree-checkout / activation scorer the loop uses — no
    bench-specific sealed code. Call :func:`unseal_retention` with the journal
    records after the loop finishes; the loop never scores test.
    """
    from raven.evolver.orchestrator.sealed.runner import SealedTestRunner

    ids = list(test_task_ids if test_task_ids is not None else backend.test_task_ids)
    return SealedTestRunner(eval_fn=backend.eval, test_task_ids=ids, sealed_dir=Path(sealed_dir), k=k)


def make_metadata_apply_fn(
    *, git_branch: str = "evolver/orchestrator", git_commit_sha: str = "replay"
) -> Callable[[str, AppliedPatch, int], HarnessNode]:
    """Apply that records a child node without touching git (replay / dry runs).

    A real run passes ``EvolverTreeStore.create_child_node`` instead, which does
    the git apply + immutable-kernel guard + JSON persistence.
    """
    counter = {"n": 0}

    def apply_fn(parent_id: str, patch: AppliedPatch, round_index: int) -> HarnessNode:
        counter["n"] += 1
        node_id = f"v{round_index}-c{counter['n']}"
        return HarnessNode(
            node_id=node_id,
            parent_id=parent_id,
            git_commit_sha=git_commit_sha,
            git_branch=git_branch,
            created_at=HarnessNode.utc_now(),
            created_at_iter=round_index,
            patch=patch,
        )

    return apply_fn


def build_evolution_orchestrator(
    config: OrchestratorConfig,
    *,
    repo_root: str | Path,
    base_sha: str,
    root_node_id: str,
    backend: EvalBackend,
    gate_policy,
    diagnose_of: Callable[[HarnessNode], tuple[Any, Optional[dict]]],
    design_of: Callable[[Callable[[HarnessNode], str], dict], Any],
    baseline_of: Callable[[], Any],
    files_of: Callable[[Any], dict[str, bytes]],
    deletions_of: Optional[Callable[[Any], list[str]]] = None,
    driver_call_fn: Optional[CallFn] = None,
    verdict_fn=None,
    verdict_call_fn: Optional[CallFn] = None,
    verdict_why_keys_of: Optional[Callable[[], Optional[list[str]]]] = None,
    harm_excerpt_of: Optional[Callable[[str, str], Optional[str]]] = None,
    git_branch: str = "evolver/orchestrator",
    run_gate0: bool = True,
    preflight_fn=None,
    fired_source_of=None,
) -> EvolutionOrchestrator:
    """Assemble a full evolution run, owning the wiring every bench shares.

    A benchmark only provides the ~7 things that genuinely differ; everything
    else — the vanilla root node, the ``node_id -> commit`` registry and the
    ``sha_of`` chain resolver, the edit-then-commit apply, the candidate/history
    bookkeeping, the focused-subset and outcome hooks, the Gate0-then-cold-start
    ordering, and the final :class:`EvolutionOrchestrator` assembly — lives here
    once. The bench-specific inputs are deliberately **deferred callables** so
    this function controls their firing order (Gate0 + cold start must run before
    taxonomy induction reads trajectories and before the baseline reads the
    ledger):

    - ``diagnose_of(vanilla_node) -> (diagnose_fn, seed_failure_map)`` — usually
      ``resolve_taxonomy`` + a bench diagnose builder.
    - ``design_of(sha_of, history, archive_summary_of) -> design_fn`` — receives
      the internally-owned ``sha_of`` chain resolver, the cross-round ``history``
      dict, and a zero-arg callable rendering the GSME elite bank (for the
      design prompt, so the driver knows what is already verified).
    - ``baseline_of() -> BaselineProvider`` — usually
      :func:`~...gates.policy.make_frozen_baseline`; called after cold start so
      it reads a materialised ledger.
    - ``backend`` (carries ``precheck``), ``gate_policy``, ``files_of`` /
      ``deletions_of`` (the candidate -> file-bytes extractors).
    """
    import json

    vanilla_node = HarnessNode(
        node_id=root_node_id,
        parent_id=None,
        git_commit_sha=base_sha,
        git_branch=git_branch,
        created_at=HarnessNode.utc_now(),
        created_at_iter=0,
    )
    # shared state across steps
    sha_by_node: dict[str, str] = {root_node_id: base_sha}
    cand_by_node: dict[str, Any] = {}
    # Cross-round per-WHY attempt history, persisted so a crash-resume does not
    # amnesia the designer (prior-attempt lessons + the editor's WHY decay both
    # read it); the archive already survives resume, this keeps them symmetric.
    history_path = Path(config.work_dir) / "history.json"
    history: dict[str, list[dict]] = {}
    try:
        if history_path.exists():
            history = json.loads(history_path.read_text())
    except (OSError, ValueError):
        history = {}

    # Gate0 BEFORE the (potentially hours-long) vanilla cold start — a dirty env
    # would bake contaminated trials into the run's permanent baseline — then
    # materialise the vanilla ledger so taxonomy induction and the baseline seed
    # have trajectories/scores to read.
    if run_gate0 and backend.precheck is not None:
        backend.precheck()
    backend.cold_start()

    diagnose_fn, seed_failure_map = diagnose_of(vanilla_node)

    def sha_of(parent: HarnessNode) -> str:
        sha = sha_by_node.get(parent.node_id)
        if sha is None and parent.git_commit_sha not in ("", "unknown"):
            sha = parent.git_commit_sha
            sha_by_node[parent.node_id] = sha
        if sha is None:
            raise KeyError(f"unknown parent commit for {parent.node_id!r} (chain broken)")
        return sha

    # GSME: the per-cell elite bank (persisted under work_dir, reloaded on
    # resume). Created before the design step so the designer can read it.
    from raven.evolver.orchestrator.archive import GsmeArchive

    archive = GsmeArchive(config.archive_path)

    design_fn = design_of(sha_of, history, archive.summary_text)

    raw_apply = make_git_commit_apply_fn(
        repo_root,
        files_of,
        root_node_id=root_node_id,
        base_sha=base_sha,
        sha_by_node=sha_by_node,
        deletions_of=deletions_of,
        git_branch=git_branch,
    )

    def apply_fn(parent_id, cand, round_index):
        node = raw_apply(parent_id, cand, round_index)
        cand_by_node[node.node_id] = cand
        return node

    def focused_source(node: HarnessNode) -> list[str]:
        cand = cand_by_node.get(node.node_id)
        return list(getattr(cand, "focused_task_ids", [])) if cand else []

    # Gate-b attribution, beacon-aware: only a candidate that actually carries
    # an activation_beacon call gets per-task firing data; for the rest (prompt/
    # config edits, recombinants of such) return None so the gate fails OPEN
    # instead of rejecting an uninstrumented-but-honest candidate on an empty
    # firing set. ``fired_source_of`` is the bench's raw ledger reader.
    fired_source = None
    if fired_source_of is not None:

        def fired_source(node: HarnessNode, task_ids: list[str]):
            cand = cand_by_node.get(node.node_id)
            if not getattr(cand, "has_beacon", False):
                return None
            return fired_source_of(node, task_ids)

    def _persist_history():
        try:
            history_path.parent.mkdir(parents=True, exist_ok=True)
            history_path.write_text(json.dumps(history, indent=2))
        except OSError:
            pass

    def inert_hook(cand, outcome):
        # A preflight-pruned candidate never got a node, but its death is a
        # designer lesson (the TRIGGER was unreachable, not the mechanism) —
        # without this entry the next round can redesign the same dead end.
        why = str(getattr(cand, "why", "") or "")
        if not why:
            return
        files = getattr(cand, "files", None)
        history.setdefault(why, []).append(
            {
                "node_id": outcome.node_id,
                "files": sorted(files) if isinstance(files, dict) else [],
                "summary": str(getattr(cand, "summary", "") or ""),
                "outcome": outcome.status.value,
                "promoted": False,
                "reason": (outcome.stats or {}).get("reason", ""),
            }
        )
        _persist_history()

    def outcome_hook(ctx, outcome):
        cand = cand_by_node.get(ctx.node.node_id)
        if cand is None:
            return
        entry = {
            "node_id": ctx.node.node_id,
            "files": sorted(cand.files),
            "summary": cand.summary,
            "outcome": outcome.status.value,
            "promoted": outcome.promoted,
        }
        flips = outcome.stats.get("flips") if outcome.stats else None
        if flips:
            entry["rescued"] = flips["n_rescued"]
            entry["regressed"] = flips["n_regressed"]
            entry["regressed_ids"] = list(flips["regressed"])[:3]
            # Harm replay: attach HOW the first regressed task broke under this
            # candidate (its own confirm trajectory), so the next attempt's
            # "narrow it sharply" has the wound, not just the count.
            if harm_excerpt_of and flips["regressed"]:
                try:
                    harm = harm_excerpt_of(ctx.node.node_id, list(flips["regressed"])[0])
                except Exception:  # noqa: BLE001 — replay is best-effort context
                    harm = None
                if harm:
                    entry["harm"] = harm
        history.setdefault(cand.why, []).append(entry)
        _persist_history()

    # Verdict rides its own driver when given (role->model splits, e.g. a cheap
    # diagnose model + a stronger narrative model), else the shared one.
    if verdict_fn is None and (verdict_call_fn or driver_call_fn) is not None:
        verdict_fn = make_verdict_fn(verdict_call_fn or driver_call_fn, why_keys_of=verdict_why_keys_of)

    # budget.recombinations_per_round=0 disables recombination proposals while
    # still banking elites for audit and the design prompt.
    return EvolutionOrchestrator(
        config,
        backend=backend,
        diagnose_fn=diagnose_fn,
        design_fn=design_fn,
        apply_fn=apply_fn,
        gate_policy=gate_policy,
        baseline_provider=baseline_of(),
        verdict_fn=verdict_fn,
        preflight_fn=preflight_fn,
        fired_source=fired_source,
        focused_source=focused_source,
        outcome_hook=outcome_hook,
        inert_hook=inert_hook,
        seed_failure_map=seed_failure_map,
        archive=archive,
        recombine_fn=make_git_recombine_fn(repo_root),
    )


def build_orchestrator(
    config: OrchestratorConfig,
    *,
    backend: EvalBackend,
    diagnose_fn,
    design_fn,
    apply_fn,
    gate_policy=None,
    baseline_provider=None,
    verdict_fn=None,
    preflight_fn=None,
    fired_source=None,
    focused_source=None,
    outcome_hook=None,
    archive=None,
    recombine_fn=None,
) -> EvolutionOrchestrator:
    """Low-level assembler: wrap pre-built steps into an orchestrator with no
    shared-wiring conveniences (used by tests and the multibench smoke). Most
    real runs use :func:`build_evolution_orchestrator` instead."""
    return EvolutionOrchestrator(
        config,
        backend=backend,
        diagnose_fn=diagnose_fn,
        design_fn=design_fn,
        apply_fn=apply_fn,
        gate_policy=gate_policy,
        baseline_provider=baseline_provider,
        verdict_fn=verdict_fn,
        preflight_fn=preflight_fn,
        fired_source=fired_source,
        focused_source=focused_source,
        outcome_hook=outcome_hook,
        archive=archive,
        recombine_fn=recombine_fn,
    )


__all__ = [
    "EndpointConfig",
    "ScoringTrajectoryRun",
    "make_diagnose_fn",
    "make_design_fn",
    "make_verdict_fn",
    "summarize_round",
    "make_llm_backend",
    "make_git_commit_apply_fn",
    "make_git_recombine_fn",
    "make_zero_hit_preflight",
    "make_worktree_eval_fn",
    "make_sealed_runner",
    "make_metadata_apply_fn",
    "build_evolution_orchestrator",
    "build_orchestrator",
]
