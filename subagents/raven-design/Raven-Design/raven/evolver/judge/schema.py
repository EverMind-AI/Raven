"""Schema definitions for LLM judge output.

The judge reads a trajectory and produces a structured analysis with two
orthogonal axes:

- **Issue type** (L1 / L2 / L3, per spec §3) — routes the output:
  L1 → human review; L2/L3 → evolver patch.
- **Patch location & purpose** ((WHERE, WHY), per spec §12.4-§12.5) — only
  populated for L2 / L3, identifies what to edit and which pathology
  is being addressed.

All enums are ``str`` subclasses so they JSON-serialize to their string
values without custom encoders, and so they compare cleanly against
strings parsed from LLM output (``IssueType.L1 == "L1"``).

PatchWhy.other accepts a free-form sub-name (``"other:<new_category>"``)
to support the evolving WHY taxonomy described in spec §12.5: judge may
propose a new pathology class, which gets routed for human review before
becoming a first-class enum.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class IssueType(str, Enum):
    """Three-state classification per spec §3 — drives downstream routing."""

    L1 = "L1"  # infrastructure bug — block evolver, raise to human
    L2 = "L2"  # harness config error — evolver patches docs/configs
    L3 = "L3"  # harness capability gap — evolver patches skills/memory/hooks


class PatchWhere(str, Enum):
    """Structural location of a proposed patch (spec §12.4, 14 categories).

    Each value corresponds to a class of files in the mutable surface
    (spec §2.2). The judge picks one based on the failure signature.

    `control` (2026-06-10): added for evolution-restart round 1 to
    represent control-arm nodes that carry no real patch surface.
    """

    system_prompt_template = "system_prompt_template"  # templates/*.md
    task_wrapper_prompt = "task_wrapper_prompt"  # scorer src/domains/*/prompt.md
    judge_prompt = "judge_prompt"  # eval_engine/prompts/*.py (L-B internal)
    tool_description = "tool_description"  # agent/tools/*.py description field
    hook_new = "hook_new"  # new agent/hook/<name>.py
    hook_modify = "hook_modify"  # eval_engine/hooks/*.py existing
    skill = "skill"  # memory_engine/skills/*
    memory = "memory"  # memory_engine/everos/*
    tool_new = "tool_new"  # new agent/tools/<name>.py
    loop_override = "loop_override"  # scoped loop.py override (code class)
    context_override = "context_override"  # scoped context.py override (code class)
    tool_override = "tool_override"  # scoped tool override (code class)
    config = "config"  # yaml/json defaults
    control = "control"  # control arm — no patch surface


class PatchWhy(str, Enum):
    """Pathology category the patch addresses (spec §12.5, 11 named + other).

    Derived from the 244-paired SWE-bench failure analysis. Evolving: judge
    may propose a new class via ``other`` plus a free-form sub-name, which
    is reviewed before being promoted to a first-class enum value.

    `reasoning_visibility` (2026-06-01): promoted from B2 dry-run
    `patch_why_extra` accumulation — the dominant uncategorized class
    (5/10 ``other`` entries: reasoning_visibility_improvement,
    communication_traceability, communication verbosity nudge,
    explanatory_text_nudge, trajectory_logging_quality).

    `empty_response_recovery`, `method_lock_in_remedy`,
    `infra_neutrality_control` (2026-06-10): added for evolution-restart
    round 1 — empty-response streak recovery, early method lock-in remedy,
    and control-arm bookkeeping respectively.
    """

    repetition_breaker = "repetition_breaker"  # 72% trajectory tail repetition
    test_starvation_remedy = "test_starvation_remedy"  # PASS 25% TEST vs FAIL 12%
    budget_awareness = "budget_awareness"  # FAIL 100% hits maxIter
    tool_clarity = "tool_clarity"  # tool docs missing/misleading
    env_contract_clarify = "env_contract_clarify"  # env rules contradictory (e.g. NEVER prompt)
    skill_gap_fill = "skill_gap_fill"  # recurring task type, no skill
    memory_recall_fix = "memory_recall_fix"  # re-reads / re-verifies known facts
    reasoning_visibility = "reasoning_visibility"  # tool-only stretches w/o narrative explanation
    empty_response_recovery = "empty_response_recovery"  # repeated empty-response streak recovery
    method_lock_in_remedy = "method_lock_in_remedy"  # early method lock-in remedy
    infra_neutrality_control = "infra_neutrality_control"  # control-arm bookkeeping; not a real pathology
    other = "other"  # judge-proposed; sub-name in patch_why_extra


class ActionKind(str, Enum):
    """What downstream should do with this judge output."""

    human_review_needed = "human_review_needed"  # L1: evolver pauses, engineer fixes
    patch_proposal = "patch_proposal"  # L2/L3: evolver applies patch


@dataclass
class ProposedComponent:
    """One file the judge proposes to edit as part of a multi-file patch.

    Counterpart in the tree layer is
    :class:`raven.evolver.tree.node.PatchComponent` (with the actual
    diff). At judge time we only have natural-language ``summary`` — the
    mutation operator (GEPA library) later turns each ``ProposedComponent``
    into a concrete ``PatchComponent`` with a unified diff.

    Multi-component design rationale (spec §12.2, §18.5.1.x): a single
    "patch" may bundle several file edits that together implement one
    semantic improvement (e.g., new hook file + register it in a config).
    The ``depends_on`` graph lets component-level bisect drop subsets
    safely when a regression appears.

    For the simple 1-file fix, ``JudgeAction.components`` has length 1.
    """

    component_id: str  # "comp_1" / "comp_2" — unique within one JudgeAction
    target_file: str  # repo-relative path of the file to edit
    summary: str  # natural-language description of the intended edit
    depends_on: list[str] = field(default_factory=list)  # sibling component_ids

    def to_dict(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "target_file": self.target_file,
            "summary": self.summary,
            "depends_on": list(self.depends_on),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ProposedComponent":
        missing = [k for k in ("component_id", "target_file", "summary") if k not in d]
        if missing:
            raise ValueError(f"ProposedComponent missing required fields: {missing}")
        return cls(
            component_id=d["component_id"],
            target_file=d["target_file"],
            summary=d["summary"],
            depends_on=list(d.get("depends_on") or []),
        )


@dataclass
class JudgeAction:
    """The judge's recommended next action.

    For ``kind=human_review_needed`` (L1), only ``reasoning`` is populated
    — the evolver should NOT attempt to apply any patch, just surface the
    issue to a human operator.

    For ``kind=patch_proposal`` (L2/L3), patch fields are populated:
    WHERE / WHY and a non-empty ``components`` list. Each component
    names one file to edit plus a natural-language summary; mutation
    operators later turn these into unified diffs.

    ``patch_why_extra`` is non-empty only when ``patch_why == other``, and
    carries the judge's proposed new pathology sub-name (e.g.
    ``"other:plan_action_disconnect"``). Reviewing-and-promoting these
    sub-names is the mechanism for evolving the WHY taxonomy (spec §12.5).
    """

    kind: ActionKind
    reasoning: str

    # Populated only for patch_proposal:
    patch_where: Optional[PatchWhere] = None
    patch_why: Optional[PatchWhy] = None
    patch_why_extra: Optional[str] = None
    components: list[ProposedComponent] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.kind == ActionKind.patch_proposal:
            if not self.components:
                raise ValueError("patch_proposal JudgeAction requires at least one component")
            ids = [c.component_id for c in self.components]
            if len(ids) != len(set(ids)):
                raise ValueError(f"JudgeAction.components have duplicate component_id: {ids}")
            valid_ids = set(ids)
            for c in self.components:
                for dep in c.depends_on:
                    if dep not in valid_ids:
                        raise ValueError(
                            f"ProposedComponent {c.component_id!r} depends_on {dep!r} which is not a sibling component"
                        )

    def is_patch(self) -> bool:
        return self.kind == ActionKind.patch_proposal

    def is_human_review(self) -> bool:
        return self.kind == ActionKind.human_review_needed

    @property
    def target_file(self) -> Optional[str]:
        """Primary file = first component's target_file (backwards-compat shim)."""
        return self.components[0].target_file if self.components else None

    @property
    def patch_summary(self) -> Optional[str]:
        """For single-component patches, return that component's summary.

        Multi-component patches don't have a single summary — use
        ``components[i].summary`` directly, or ``reasoning`` for the
        overall narrative.
        """
        if not self.components:
            return None
        if len(self.components) == 1:
            return self.components[0].summary
        # Multi-component: concatenate for any legacy caller that still
        # asks for a single string, but the canonical access is per-comp.
        return " | ".join(c.summary for c in self.components)


@dataclass
class JudgeResult:
    """Complete judge analysis of one trajectory.

    Fields follow spec §3.2 schema. ``evidence_turn_range`` is the
    interval (inclusive) of turn indices the judge cites as
    supporting evidence — used by the evolver to anchor patches to
    specific failure points in the trajectory.

    ``confidence`` ∈ [0.0, 1.0]. Downstream may reject low-confidence
    judgments (e.g. evolver skips patch proposals with confidence < 0.5).
    """

    trajectory_id: str
    issue_type: IssueType
    confidence: float
    signal_description: str
    proposed_action: JudgeAction
    evidence_turn_range: Optional[tuple[int, int]] = None
    raw_response: Optional[str] = None  # original LLM text, for audit

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0.0, 1.0], got {self.confidence!r}")
        # Cross-field invariant: L1 must use human_review_needed
        if self.issue_type == IssueType.L1 and not self.proposed_action.is_human_review():
            raise ValueError(
                f"L1 issues must have proposed_action.kind=human_review_needed; got {self.proposed_action.kind!r}"
            )
        # L2/L3 must use patch_proposal with populated where/why
        if self.issue_type in (IssueType.L2, IssueType.L3):
            if not self.proposed_action.is_patch():
                raise ValueError(f"{self.issue_type.value} issues must have proposed_action.kind=patch_proposal")
            if self.proposed_action.patch_where is None:
                raise ValueError(f"{self.issue_type.value} patch must have patch_where set")
            if self.proposed_action.patch_why is None:
                raise ValueError(f"{self.issue_type.value} patch must have patch_why set")
            if self.proposed_action.patch_why == PatchWhy.other and not self.proposed_action.patch_why_extra:
                raise ValueError("patch_why=other requires patch_why_extra to carry the judge-proposed sub-name")


@dataclass(frozen=True)
class PassFailResult:
    """A no-benchmark pass/fail verdict for one trajectory.

    Distinct from :class:`JudgeResult` (which diagnoses failure *mode* and never
    carries a pass/fail): this is what an LLM scorer returns when there is no
    verifier — the orchestrator maps ``passed`` onto ``TaskEval.passes``.
    """

    trajectory_id: str
    passed: bool
    reasoning: str = ""
    raw_response: Optional[str] = None


__all__ = [
    "IssueType",
    "PatchWhere",
    "PatchWhy",
    "ActionKind",
    "ProposedComponent",
    "JudgeAction",
    "JudgeResult",
    "PassFailResult",
]
