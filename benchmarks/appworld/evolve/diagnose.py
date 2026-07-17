"""AppWorld failure diagnosis into a WHY/WHERE taxonomy (in-package).

Two taxonomy sources, toggled by the caller (default = hardcoded):

- **hardcoded** (default): the hand-derived 7 AppWorld WHY classes (W1-W7), derived once by
  hand from real vanilla trajectories. Frozen constant :data:`DEFAULT_APPWORLD_TAXONOMY`.
- **induce**: the bench-neutral open-ended map-reduce in
  :mod:`raven.evolver.orchestrator.nodes.taxonomy` discovers a taxonomy from
  vanilla failures; :func:`ensure_taxonomy` here is the AppWorld-bound wrapper
  (AppWorld bench description, W1-W7 as the hardcoded default). Induction
  failure raises — it never silently substitutes the hardcoded table.

Diagnosis is **multi-label**: one trajectory can exhibit several failure modes,
so it may land in several ``WHERE::WHY`` cells (each hit increments that WHY's
count). Output is a failure_map dict shaped exactly like
``failure_map_builder.build_failure_map`` so the orchestrator's
``select_target_whys`` / design step are unchanged:

    {"why_distribution": {why: count},
     "cells": {"<WHERE>::<WHY>": {"candidates": [
         {"trajectory_id", "reasoning", "components": [{"summary": fix_hint}]}]}},
     "_n_judged": n}
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from raven.evolver.orchestrator.nodes.taxonomy import (
    TaxonomySpec,
    classify_failures,
)
from raven.evolver.orchestrator.nodes.taxonomy import (
    ensure_taxonomy as _generic_ensure_taxonomy,
)
from raven.evolver.orchestrator.nodes.taxonomy import (
    induce_taxonomy as _generic_induce_taxonomy,
)
from raven.evolver.tree.node import HarnessNode

# The hand-derived 7 AppWorld WHY classes (verbatim) + one escape hatch.
WHY_CLASSES = {
    "W1_empty_response_stall": "Agent emits an empty / no-tool-call turn and stops early — no real work done.",
    "W2_no_finalize": "Agent does work but NEVER calls apis.supervisor.complete_task (or only says 'done' in prose) — nothing submitted.",
    "W3_api_contract_misuse": "Agent misuses the API contract: wrong/missing access_token, blocked modules, guessing signatures/params by trial-and-error, login->token dance failing.",
    "W4_temporal_grounding": "Agent treats 'today'/'this year'/'recent' as real-world time (e.g. 2026) instead of the environment's simulated date (~2023) -> wrong date filtering.",
    "W5_premature_answer": "Agent answers/finalizes before gathering or computing enough — hallucinated value, or accepts a 0-result filtered query as the answer.",
    "W6_action_state_mismatch": "Agent's actions leave the DB in the wrong state (edited wrong records / missed some / did extra) — the mutation doesn't match the required outcome. Often a capability/logic limit, not harness-fixable.",
    "W7_borderline_flaky": "No stable pathology — the task flips pass/fail run-to-run; this is noise, not a fixable failure mode.",
    "other": "None of the above — provide a short sub-name.",
}

# AppWorld patch surface (WHERE a fix would go).
WHERE_CLASSES = {
    "appworld_prompt": "benchmarks/appworld/agent_cli.py APPWORLD_PROMPT (the agent instruction text).",
    "exec_tool": "benchmarks/appworld/tool.py AppWorldExecuteTool (execution / error-recovery behaviour).",
    "agent_hook_new": "a NEW lifecycle hook raven/agent/hook/<name>.py (runtime intervention: finalize/recovery/verify).",
    "agent_hook_wire": "benchmarks/appworld/agent_cli.py hook wiring (construct the hook and pass it to AgentLoop, unconditionally).",
    "agent_loop": "raven/agent/loop/ (loop-level change — use sparingly).",
    "none": "capability ceiling — no harness fix can help (typical for W6/W7); do not target.",
}

DEFAULT_APPWORLD_TAXONOMY = TaxonomySpec(dict(WHY_CLASSES), dict(WHERE_CLASSES))

APPWORLD_BENCH_DESC = (
    "an AppWorld agent harness (the agent writes Python calling "
    "apis.<app>.<method>(...) and must finish with apis.supervisor.complete_task)"
)


APPWORLD_BENCH_INTRO = (
    "You are a failure analyst for an AppWorld agent harness. AppWorld tasks: an agent writes "
    "Python calling apis.<app>.<method>(...) to fulfill a supervisor request and must finish with "
    "apis.supervisor.complete_task(answer=...)."
)

APPWORLD_DIAGNOSIS_RULES = (
    "If the agent clearly could not do the task correctly even "
    "with more turns (wrong multi-step data logic), that is W6 with WHERE=none (capability ceiling). "
    "If pass/fail looks random with no clear cause, use W7 / none. "
    "The ATTEMPTS line (when present) summarizes ALL runs of this task: pass/fail flips across "
    "attempts are the W7 signature; repeated failures on the SAME check indicate a stable pathology "
    "(not W7)."
)


def diagnose_appworld(
    call_fn: Callable[[list], str],
    trajectories,
    *,
    taxonomy: TaxonomySpec = DEFAULT_APPWORLD_TAXONOMY,
    max_workers: int = 8,
    retries: int = 2,
) -> dict:
    """Judge failing trajectories into a multi-label failure_map over ``taxonomy``.

    ``trajectories`` = list of ``(trajectory_id, task_description, transcript)``.
    Each trajectory can contribute several modes; every hit increments its WHY.
    Thin AppWorld binding over the bench-neutral :func:`classify_failures`.
    """
    return classify_failures(
        call_fn,
        trajectories,
        taxonomy,
        bench_intro=APPWORLD_BENCH_INTRO,
        extra_rules=APPWORLD_DIAGNOSIS_RULES,
        max_workers=max_workers,
        retries=retries,
    )


def induce_taxonomy(
    call_fn: Callable[[list], str],
    trajectories,
    *,
    max_workers: int = 8,
    retries: int = 2,
    target_min: int = 5,
    target_max: int = 9,
) -> tuple[TaxonomySpec, dict]:
    """AppWorld-bound wrapper over the generic induction (see nodes/taxonomy)."""
    return _generic_induce_taxonomy(
        call_fn,
        trajectories,
        bench_desc=APPWORLD_BENCH_DESC,
        max_workers=max_workers,
        retries=retries,
        target_min=target_min,
        target_max=target_max,
    )


def ensure_taxonomy(
    call_fn: Callable[[list], str],
    trajectories,
    path: str | Path,
    *,
    mode: str = "hardcoded",
    default: Optional[TaxonomySpec] = DEFAULT_APPWORLD_TAXONOMY,
    max_workers: int = 8,
    seed_path: Optional[str | Path] = None,
) -> TaxonomySpec:
    """AppWorld-bound wrapper: W1-W7 as the hardcoded default, AppWorld bench
    description for induction. Induction failure raises (no silent fallback)."""
    return _generic_ensure_taxonomy(
        call_fn,
        trajectories,
        path,
        mode=mode,
        default=default,
        bench_desc=APPWORLD_BENCH_DESC,
        max_workers=max_workers,
        seed_path=seed_path,
    )


def make_appworld_diagnose_fn(
    call_fn: Callable[[list], str],
    trajectory_source: Callable[[int, HarnessNode], list],
    *,
    taxonomy: TaxonomySpec = DEFAULT_APPWORLD_TAXONOMY,
    max_workers: int = 8,
) -> Callable[[int, HarnessNode], dict]:
    """Bind the driver + a trajectory source into the loop's ``diagnose_fn``."""

    def diagnose_fn(round_index: int, parent: HarnessNode) -> dict:
        trajs = trajectory_source(round_index, parent)
        return diagnose_appworld(call_fn, trajs, taxonomy=taxonomy, max_workers=max_workers)

    return diagnose_fn


__all__ = [
    "diagnose_appworld",
    "make_appworld_diagnose_fn",
    "TaxonomySpec",
    "DEFAULT_APPWORLD_TAXONOMY",
    "APPWORLD_BENCH_DESC",
    "induce_taxonomy",
    "ensure_taxonomy",
    "WHY_CLASSES",
    "WHERE_CLASSES",
]
