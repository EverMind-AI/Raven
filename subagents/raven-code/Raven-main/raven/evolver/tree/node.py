"""Harness node schema + JSON round-trip.

Each evolver iteration produces a :class:`HarnessNode` — one version of
the harness with metadata about how it was made, how it performed, and
what next iterations might do.

The structure (spec §12.2) has five blocks:

1. **Identity**: ``node_id`` / ``parent_id`` / Git pointers / created_at.
2. **AppliedPatch**: what was changed relative to ``parent_id`` to create
   this node — ``target_file``, ``patch_where``, ``patch_why``, diff,
   reasoning, source-evidence pointers back to trajectories.
3. **EvalResult**: what was measured (bandit task subset, per-task
   pass/fail, subset pass rate, dense signals, L1 alert count).
4. **JudgeAnalysis**: what the judge inferred from this node's
   trajectories — counts of L1/L2/L3 seen + candidate patches the judge
   proposes for future child nodes.
5. **Status**: where this node stands in the evolver lifecycle.

Storage layout (spec §12.3):

- Git commit handles the physical code state of the node (we don't
  duplicate it here).
- One JSON file per node in ``evolver/nodes/<node_id>.json`` holds the
  metadata defined here.
- Trajectory data lives in ``evolver/trajectories/`` keyed by id; this
  schema only carries trajectory *pointers*, never inline bodies.

Why explicit ``to_dict`` / ``from_dict`` instead of stdlib ``dataclasses.asdict``?
Two reasons:

- We need to deserialise enums (``PatchWhere``, ``PatchWhy``,
  ``NodeStatus``) from their string values, which ``asdict`` doesn't help
  with on load.
- Tuples like ``turn_range = (start, end)`` round-trip through JSON as
  lists — we want to coerce back to tuple on load for type stability.

Both are surface-level concerns, but the explicit code keeps the failure
modes loud (missing fields raise, unknown enums raise).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Union

from raven.evolver.judge.schema import PatchWhere, PatchWhy

# ---------------------------------------------------------------------------
# Status enum
# ---------------------------------------------------------------------------


class NodeStatus(str, Enum):
    """Where a node stands in the evolver lifecycle.

    - ``active``: live in the tree, eligible for bandit-on-nodes selection.
    - ``pruned_low_score``: still in archive (Git keeps the commit) but
      bandit deprioritises it; rare for it to be re-selected.
    - ``pruned_inert``: culled at the zero-GPU preflight (SOP §2 ③) — its
      trigger predicate had zero hits over the historical trajectory corpus,
      so the mechanism was never applied or evaluated. Distinct from
      ``pruned_at_screen``: an inert death indicts the trigger's
      reachability, not the mechanism body.
    - ``pruned_at_screen``: culled at the K=1 anchor screen — its anchor-mean
      pass@1 fell more than ``cull_threshold`` below vanilla, so it never ran
      the full-set confirm (SOP §2 ⑤a: screen on the anchor, then return).
    - ``pruned_at_confirm``: ran the full-set K=3 confirm but did not beat
      vanilla / pass the gates (SOP §2 ⑥).
    - ``blocked_l1``: judge flagged an L1 infra bug while evaluating this
      node; evolver paused until human review.
    - ``promoted_to_baseline``: this node became a new baseline (rare;
      happens at major lift inflection points).
    - ``archived_methodology_failure``: rewards from this node are
      contaminated by a methodology defect; excluded from the scheduler
      at load time so the bandit never learns from tainted outcomes.
    - ``errored``: apply or evaluation raised for this candidate; the round
      recorded the reason and skipped it rather than aborting (a single
      candidate's crash must not sink the round).
    """

    active = "active"
    pruned_low_score = "pruned_low_score"
    pruned_inert = "pruned_inert"
    pruned_at_screen = "pruned_at_screen"
    pruned_at_confirm = "pruned_at_confirm"
    blocked_l1 = "blocked_l1"
    promoted_to_baseline = "promoted_to_baseline"
    archived_methodology_failure = "archived-methodology-failure"
    errored = "errored"


# ---------------------------------------------------------------------------
# Patch-related substructures
# ---------------------------------------------------------------------------


@dataclass
class SourceEvidence:
    """A pointer into a trajectory used as evidence for a patch.

    A single applied patch may cite multiple trajectories (e.g., five
    trajectories all hit ``repetition_breaker`` at turns 20-30); each is
    one ``SourceEvidence``.
    """

    trajectory_id: str
    turn_range: tuple[int, int]
    finding: str  # one-line summary of what the evidence shows

    def to_dict(self) -> dict[str, Any]:
        return {
            "trajectory_id": self.trajectory_id,
            "turn_range": list(self.turn_range),
            "finding": self.finding,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SourceEvidence":
        tr = d.get("turn_range")
        if not isinstance(tr, (list, tuple)) or len(tr) != 2:
            raise ValueError(f"SourceEvidence.turn_range must be 2-element list/tuple, got {tr!r}")
        return cls(
            trajectory_id=_require(d, "trajectory_id", "SourceEvidence"),
            turn_range=(int(tr[0]), int(tr[1])),
            finding=_require(d, "finding", "SourceEvidence"),
        )


@dataclass
class PatchComponent:
    """One independently-rollbackable piece of a multi-file patch.

    A patch may bundle several file edits that together implement one
    semantic improvement (e.g., add a new hook file + register it in a
    config + reference it from a prompt template). Each such file edit
    is one ``PatchComponent``. The bundle hangs together via the
    ``depends_on`` graph: if ``comp_3`` lists ``comp_1`` as dependency,
    ``comp_3`` MUST be applied after ``comp_1`` (and cannot survive in
    isolation when ``comp_1`` is dropped during bisect).

    Component-level bisect (spec §18.5.1.x): when an applied patch
    causes regression, the evolver tries dropping subsets of components
    (respecting ``depends_on``) and keeping the highest-quality subset
    that still beats the parent.

    For the simple "1 file, 1 fix" case, an ``AppliedPatch`` carries
    exactly one ``PatchComponent`` — the multi-component machinery
    becomes a no-op.
    """

    component_id: str  # "comp_1" / "comp_2" — unique within one patch
    target_file: str  # repo-relative path
    diff: str  # unified diff for THIS file only
    rationale: str  # what this component does
    depends_on: list[str] = field(default_factory=list)  # other component_ids

    def to_dict(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "target_file": self.target_file,
            "diff": self.diff,
            "rationale": self.rationale,
            "depends_on": list(self.depends_on),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PatchComponent":
        return cls(
            component_id=_require(d, "component_id", "PatchComponent"),
            target_file=_require(d, "target_file", "PatchComponent"),
            diff=_require(d, "diff", "PatchComponent"),
            rationale=_require(d, "rationale", "PatchComponent"),
            depends_on=list(d.get("depends_on") or []),
        )


@dataclass
class AppliedPatch:
    """The actual patch that was applied to the parent to create this node.

    Multi-component design (spec §12.2, updated 2026-05-28):
    A patch is a non-empty ordered list of ``PatchComponent``.
    Most patches have exactly one component (single-file edit). Multi-file
    coupled patches use ``components`` with explicit ``depends_on`` so the
    evolver can do component-level bisect on regression (§18.5.1.x).

    Convenience properties for the common single-component case:
    - ``target_file`` returns the first component's file
    - ``diff`` returns the concatenated diff across components

    The canonical code state still lives in the node's Git commit;
    components describe *how* the diff is logically structured.
    """

    patch_where: PatchWhere
    patch_why: PatchWhy
    components: list[PatchComponent]  # >= 1
    overall_reasoning: str  # judge's reasoning at the whole-patch level
    source_evidence: list[SourceEvidence] = field(default_factory=list)
    patch_why_extra: Optional[str] = None  # only when patch_why=other

    # Bisect bookkeeping (spec §18.5.1.x). Set when a patch was rescued
    # from regression by dropping some components.
    partial: bool = False
    dropped_components: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.patch_why == PatchWhy.other and not self.patch_why_extra:
            raise ValueError("AppliedPatch with patch_why=other requires non-empty patch_why_extra")
        if not self.components and self.patch_where != PatchWhere.control:
            raise ValueError("AppliedPatch requires at least one component")
        # Component ids must be unique within the patch
        ids = [c.component_id for c in self.components]
        if len(ids) != len(set(ids)):
            raise ValueError(f"AppliedPatch components have duplicate component_id: {ids}")
        # depends_on references must resolve to existing component_ids
        valid_ids = set(ids)
        for c in self.components:
            for dep in c.depends_on:
                if dep not in valid_ids:
                    raise ValueError(
                        f"PatchComponent {c.component_id!r} depends_on {dep!r} which is not a sibling component"
                    )

    @property
    def target_file(self) -> Optional[str]:
        """Primary file = first component's file (single-component default).

        Returns None for control-arm nodes that carry no components.
        """
        return self.components[0].target_file if self.components else None

    @property
    def diff(self) -> str:
        """Concatenated diff across all components (Git-applicable as a whole)."""
        return "\n".join(c.diff for c in self.components)

    def to_dict(self) -> dict[str, Any]:
        return {
            "patch_where": self.patch_where.value,
            "patch_why": self.patch_why.value,
            "patch_why_extra": self.patch_why_extra,
            "overall_reasoning": self.overall_reasoning,
            "components": [c.to_dict() for c in self.components],
            "source_evidence": [e.to_dict() for e in self.source_evidence],
            "partial": self.partial,
            "dropped_components": list(self.dropped_components),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AppliedPatch":
        components_raw = _require(d, "components", "AppliedPatch")
        patch_where_raw = d.get("patch_where", "")
        is_control = patch_where_raw == PatchWhere.control.value
        if not isinstance(components_raw, list) or (not components_raw and not is_control):
            raise ValueError("AppliedPatch.components must be a non-empty list")
        return cls(
            patch_where=_coerce_enum(
                _require(d, "patch_where", "AppliedPatch"),
                PatchWhere,
                "patch_where",
            ),
            patch_why=_coerce_enum(
                _require(d, "patch_why", "AppliedPatch"),
                PatchWhy,
                "patch_why",
            ),
            components=[PatchComponent.from_dict(c) for c in components_raw],
            overall_reasoning=_require(d, "overall_reasoning", "AppliedPatch"),
            source_evidence=[SourceEvidence.from_dict(e) for e in (d.get("source_evidence") or [])],
            patch_why_extra=d.get("patch_why_extra"),
            partial=bool(d.get("partial", False)),
            dropped_components=list(d.get("dropped_components") or []),
        )


# ---------------------------------------------------------------------------
# Evaluation substructure
# ---------------------------------------------------------------------------


@dataclass
class PerTaskResult:
    """One task's outcome under this node.

    ``pass_outcome`` rather than ``pass`` to avoid the Python keyword.
    ``turns`` and ``elapsed_sec`` are optional because the eval driver
    may or may not surface them depending on the task.
    """

    pass_outcome: bool
    turns: Optional[int] = None
    elapsed_sec: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"pass": self.pass_outcome}
        if self.turns is not None:
            d["turns"] = self.turns
        if self.elapsed_sec is not None:
            d["elapsed_sec"] = self.elapsed_sec
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PerTaskResult":
        if "pass" not in d:
            raise ValueError("PerTaskResult missing required 'pass'")
        return cls(
            pass_outcome=bool(d["pass"]),
            turns=d.get("turns"),
            elapsed_sec=d.get("elapsed_sec"),
        )


@dataclass
class EvalResult:
    """Aggregated evaluation of a single node.

    ``dense_signals`` is a flexible bag — current planned keys
    (avg_token_usage / avg_turn_count / redundancy_rate /
    test_run_frequency) are documented but not enforced, so future signals
    can be added without schema migration.
    """

    bandit_tasks_chosen: list[str]
    per_task_results: dict[str, PerTaskResult]
    subset_pass_rate: float
    dense_signals: dict[str, float] = field(default_factory=dict)
    l1_alert_count: int = 0

    def __post_init__(self) -> None:
        if not 0.0 <= self.subset_pass_rate <= 1.0:
            raise ValueError(f"subset_pass_rate must be in [0,1], got {self.subset_pass_rate}")
        if self.l1_alert_count < 0:
            raise ValueError(f"l1_alert_count must be >= 0, got {self.l1_alert_count}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "bandit_tasks_chosen": list(self.bandit_tasks_chosen),
            "per_task_results": {k: v.to_dict() for k, v in self.per_task_results.items()},
            "subset_pass_rate": self.subset_pass_rate,
            "dense_signals": dict(self.dense_signals),
            "l1_alert_count": self.l1_alert_count,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EvalResult":
        return cls(
            bandit_tasks_chosen=list(_require(d, "bandit_tasks_chosen", "EvalResult")),
            per_task_results={
                k: PerTaskResult.from_dict(v) for k, v in _require(d, "per_task_results", "EvalResult").items()
            },
            subset_pass_rate=float(_require(d, "subset_pass_rate", "EvalResult")),
            dense_signals=dict(d.get("dense_signals") or {}),
            l1_alert_count=int(d.get("l1_alert_count", 0)),
        )


# ---------------------------------------------------------------------------
# Judge analysis substructure
# ---------------------------------------------------------------------------


@dataclass
class ProposedComponent:
    """A proposed component of a future patch (judge output, no diff yet).

    Counterpart to :class:`PatchComponent`: the judge describes WHAT to
    change (target_file + natural-language summary) but doesn't write
    the actual diff. The mutation operator (e.g., GEPA library) later
    turns each ``ProposedComponent`` into a concrete ``PatchComponent``
    with a unified diff.
    """

    component_id: str
    target_file: str
    summary: str  # natural language description of the proposed change
    depends_on: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "target_file": self.target_file,
            "summary": self.summary,
            "depends_on": list(self.depends_on),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ProposedComponent":
        return cls(
            component_id=_require(d, "component_id", "ProposedComponent"),
            target_file=_require(d, "target_file", "ProposedComponent"),
            summary=_require(d, "summary", "ProposedComponent"),
            depends_on=list(d.get("depends_on") or []),
        )


@dataclass
class CandidatePatch:
    """A patch the judge proposes for a *future* child node.

    These are not yet applied — the bandit-on-nodes / bandit-on-WHY will
    later pick one of these from this node's pool to spawn an actual child
    (with its own ``AppliedPatch``). The bridge from candidate to applied
    is the mutation operator producing the unified diff.

    Multi-component design (spec §12.2, updated 2026-05-28): a candidate
    patch carries one or more :class:`ProposedComponent`. The judge is
    encouraged to keep N=1 for simple single-file fixes, and to use N>1
    only when the change is structurally coupled (e.g., new hook file +
    config registration). ``depends_on`` between components is honored
    by both the mutation operator and any component-level bisect during
    evolution.
    """

    patch_where: PatchWhere
    patch_why: PatchWhy
    components: list[ProposedComponent]  # >= 1
    overall_reasoning: str
    source_trajectory_id: str
    patch_why_extra: Optional[str] = None  # only when patch_why=other

    def __post_init__(self) -> None:
        if self.patch_why == PatchWhy.other and not self.patch_why_extra:
            raise ValueError("CandidatePatch with patch_why=other requires non-empty patch_why_extra")
        if not self.components:
            raise ValueError("CandidatePatch requires at least one component")
        ids = [c.component_id for c in self.components]
        if len(ids) != len(set(ids)):
            raise ValueError(f"CandidatePatch components have duplicate component_id: {ids}")
        valid_ids = set(ids)
        for c in self.components:
            for dep in c.depends_on:
                if dep not in valid_ids:
                    raise ValueError(
                        f"ProposedComponent {c.component_id!r} depends_on {dep!r} which is not a sibling component"
                    )

    @property
    def target_file(self) -> str:
        """Primary file = first component's file (single-component default)."""
        return self.components[0].target_file

    def to_dict(self) -> dict[str, Any]:
        return {
            "patch_where": self.patch_where.value,
            "patch_why": self.patch_why.value,
            "patch_why_extra": self.patch_why_extra,
            "overall_reasoning": self.overall_reasoning,
            "components": [c.to_dict() for c in self.components],
            "source_trajectory_id": self.source_trajectory_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CandidatePatch":
        components_raw = _require(d, "components", "CandidatePatch")
        if not isinstance(components_raw, list) or not components_raw:
            raise ValueError("CandidatePatch.components must be a non-empty list")
        return cls(
            patch_where=_coerce_enum(
                _require(d, "patch_where", "CandidatePatch"),
                PatchWhere,
                "patch_where",
            ),
            patch_why=_coerce_enum(
                _require(d, "patch_why", "CandidatePatch"),
                PatchWhy,
                "patch_why",
            ),
            components=[ProposedComponent.from_dict(c) for c in components_raw],
            overall_reasoning=_require(d, "overall_reasoning", "CandidatePatch"),
            source_trajectory_id=_require(d, "source_trajectory_id", "CandidatePatch"),
            patch_why_extra=d.get("patch_why_extra"),
        )


@dataclass
class JudgeAnalysis:
    """The judge's verdict on this node's trajectories.

    ``issue_types_seen`` counts L1 / L2 / L3 occurrences across the
    node's evaluated trajectories — used by the L1 routing layer to
    decide whether to pause the evolver (high L1 count) and by
    bandit-on-WHY to rebalance pathology coverage.

    ``candidate_patches`` is the menu the evolver chooses from for the
    next iteration of child-node creation.
    """

    issue_types_seen: dict[str, int] = field(default_factory=dict)
    candidate_patches: list[CandidatePatch] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_types_seen": dict(self.issue_types_seen),
            "candidate_patches": [p.to_dict() for p in self.candidate_patches],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "JudgeAnalysis":
        return cls(
            issue_types_seen=dict(d.get("issue_types_seen") or {}),
            candidate_patches=[CandidatePatch.from_dict(p) for p in (d.get("candidate_patches") or [])],
        )


# ---------------------------------------------------------------------------
# Top-level node
# ---------------------------------------------------------------------------


SCHEMA_VERSION = 1  # bump when breaking change to JSON layout


@dataclass
class HarnessNode:
    """One harness version in the evolver tree.

    Three sub-blocks (``patch`` / ``eval`` / ``judge_analysis``) are all
    ``Optional`` — they get populated as the node moves through its
    lifecycle:

    - Root node: all three are None (no parent, no eval yet).
    - Right after creation: ``patch`` set; ``eval`` and ``judge_analysis``
      still None.
    - After evaluation: ``eval`` populated.
    - After judge runs on trajectories: ``judge_analysis`` populated.
    """

    node_id: str
    parent_id: Optional[str]
    git_commit_sha: str
    git_branch: str
    created_at: str  # ISO 8601 UTC
    created_at_iter: int
    # core_version (spec §22.5.4): the immutable-kernel version this node was
    # created under. Used for cohort-controlled cross-experiment comparison —
    # nodes with different ``core_version`` must not be directly compared on
    # eval numbers. Read from ``raven.__version__`` at
    # evolver startup and injected into each child node. Defaults to
    # ``"unknown"`` so old JSON files without this field deserialise (with a
    # warning logged at the call site, not here).
    core_version: str = "unknown"
    status: NodeStatus = NodeStatus.active
    patch: Optional[AppliedPatch] = None
    eval: Optional[EvalResult] = None
    judge_analysis: Optional[JudgeAnalysis] = None

    def __post_init__(self) -> None:
        if self.created_at_iter < 0:
            raise ValueError(f"created_at_iter must be >= 0, got {self.created_at_iter}")
        if not self.node_id:
            raise ValueError("node_id must be non-empty")
        if not self.git_commit_sha:
            raise ValueError("git_commit_sha must be non-empty")
        if not self.core_version:
            # Empty string isn't valid — caller must pass either a real
            # version like "1.0.0" or the sentinel "unknown".
            raise ValueError("core_version must be non-empty (use 'unknown' if not available)")
        # Root invariant: no parent → no applied patch
        if self.parent_id is None and self.patch is not None:
            raise ValueError("root node (parent_id=None) must not carry an AppliedPatch")

    @staticmethod
    def utc_now() -> str:
        """ISO 8601 UTC timestamp for ``created_at`` fields."""
        return datetime.now(timezone.utc).isoformat()

    # -- serialisation ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "_schema_version": SCHEMA_VERSION,
            "node_id": self.node_id,
            "parent_id": self.parent_id,
            "git_commit_sha": self.git_commit_sha,
            "git_branch": self.git_branch,
            "created_at": self.created_at,
            "created_at_iter": self.created_at_iter,
            "core_version": self.core_version,
            "status": self.status.value,
            "patch": self.patch.to_dict() if self.patch else None,
            "eval": self.eval.to_dict() if self.eval else None,
            "judge_analysis": self.judge_analysis.to_dict() if self.judge_analysis else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "HarnessNode":
        ver = d.get("_schema_version", 1)
        if ver != SCHEMA_VERSION:
            raise ValueError(
                f"HarnessNode schema version mismatch: file has {ver!r}, "
                f"code understands {SCHEMA_VERSION}. Migration not yet implemented."
            )
        patch_raw = d.get("patch")
        eval_raw = d.get("eval")
        judge_raw = d.get("judge_analysis")
        # Missing ``core_version`` is tolerated for backwards compat with
        # JSON files written before §22.5 — the node falls back to
        # ``"unknown"`` and downstream cohort analysis must exclude or
        # specially mark these nodes (spec §22.5.4).
        return cls(
            node_id=_require(d, "node_id", "HarnessNode"),
            parent_id=d.get("parent_id"),
            git_commit_sha=_require(d, "git_commit_sha", "HarnessNode"),
            git_branch=_require(d, "git_branch", "HarnessNode"),
            created_at=_require(d, "created_at", "HarnessNode"),
            created_at_iter=int(_require(d, "created_at_iter", "HarnessNode")),
            core_version=d.get("core_version", "unknown"),
            status=_coerce_enum(
                d.get("status", NodeStatus.active.value),
                NodeStatus,
                "status",
            ),
            patch=AppliedPatch.from_dict(patch_raw) if patch_raw else None,
            eval=EvalResult.from_dict(eval_raw) if eval_raw else None,
            judge_analysis=JudgeAnalysis.from_dict(judge_raw) if judge_raw else None,
        )

    @staticmethod
    def current_core_version() -> str:
        """Read the current immutable-kernel version from
        :mod:`raven` version (spec §22.5).

        Evolver code should call this when constructing new
        :class:`HarnessNode` instances so the node carries the kernel
        version it was created under. Falls back to ``"unknown"`` if
        the import fails (shouldn't happen in normal operation).
        """
        try:
            from raven.__core_version__ import __version__

            return __version__
        except ImportError:
            return "unknown"

    def save(self, path: Union[str, Path]) -> None:
        """Write this node's metadata to ``path`` as pretty JSON.

        Parent dirs are created on demand so callers can pass
        ``evolver/nodes/<node_id>.json`` without pre-mkdir.
        """
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        # Write through a temp + rename so an interrupted write doesn't
        # leave a half-formed JSON file — common pattern for crash-safe
        # file replacement on POSIX.
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        tmp.replace(p)

    @classmethod
    def load(cls, path: Union[str, Path]) -> "HarnessNode":
        p = Path(path)
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"node file {p} top-level must be a JSON object, got {type(raw).__name__}")
        return cls.from_dict(raw)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require(d: dict[str, Any], key: str, what: str) -> Any:
    """Pull a required key from ``d`` or raise with a contextual message."""
    if key not in d:
        raise ValueError(f"{what} missing required field {key!r}")
    return d[key]


def _coerce_enum(value: Any, enum_cls: type, field_name: str) -> Any:
    """Coerce a string into ``enum_cls`` or raise with valid options."""
    if isinstance(value, enum_cls):
        return value
    if not isinstance(value, str):
        raise ValueError(f"field {field_name!r} must be a string, got {type(value).__name__}")
    try:
        return enum_cls(value)
    except ValueError as exc:
        valid = [m.value for m in enum_cls]
        raise ValueError(f"field {field_name!r}={value!r} not one of {valid}") from exc


__all__ = [
    "AppliedPatch",
    "CandidatePatch",
    "EvalResult",
    "HarnessNode",
    "JudgeAnalysis",
    "NodeStatus",
    "PatchComponent",
    "PerTaskResult",
    "ProposedComponent",
    "SCHEMA_VERSION",
    "SourceEvidence",
]
