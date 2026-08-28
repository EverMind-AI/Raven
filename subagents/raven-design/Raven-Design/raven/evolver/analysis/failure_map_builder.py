"""Aggregate judge results into a ``failure_map.json``.

Consumes a list of :class:`JudgeResult` objects (the output of the
cold-start coverage bandit's ``claude_judge(trial)`` calls) and produces
a single structured aggregation file. This file is the bridge between
spec §14 step ③ (cold-start coverage) and step ④ (first evolution round):

- **Coverage check** — does the judge output cover at least
  ``min_why_classes=7`` of the WHY pathology axes (spec §14 step ③
  acceptance gate)?
- **L1 routing** — surface L1 alerts as ``human_review_needed`` items
  (evolver pauses, engineer fixes infrastructure).
- **L2 / L3 cell aggregation** — group patch proposals by
  ``(PatchWhere, PatchWhy)`` cell so the evolver can scan a single
  dict to find candidate patches for a chosen pathology.
- **WHERE / WHY marginals** — counts per axis, useful for paper
  §15 Must-nail #1 diversity plots.

JSON layout (``schema_version = "1.0"``)::

    {
      "schema_version": "1.0",
      "n_total_judged": 25,
      "n_l1": 3,
      "n_l2": 12,
      "n_l3": 10,
      "covered_why_classes": ["budget_awareness", ...],
      "covered_why_count": 7,
      "coverage_satisfied": true,
      "min_why_classes_target": 7,
      "l1_alerts": [
        {
          "trajectory_id": "...",
          "signal_description": "...",
          "reasoning": "...",
          "confidence": 0.9
        }
      ],
      "cells": {
        "hook_new::budget_awareness": {
          "n_candidates": 3,
          "trajectory_ids": ["...", ...],
          "candidates": [
            {
              "trajectory_id": "...",
              "issue_type": "L3",
              "confidence": 0.85,
              "components": [
                {"component_id": "comp_1", "target_file": "...",
                 "summary": "...", "depends_on": []}
              ],
              "reasoning": "..."
            }
          ]
        },
        ...
      },
      "where_distribution": {"hook_new": 5, "skill": 3, ...},
      "why_distribution": {"budget_awareness": 4, ...}
    }

Cell keys use ``"<WHERE>::<WHY>"`` (double-colon separator, no nesting)
so the file remains valid JSON and grep-able from shell.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from raven.evolver.judge.schema import (
    IssueType,
    JudgeResult,
    PatchWhere,
    PatchWhy,
)

SCHEMA_VERSION = "1.0"
DEFAULT_MIN_WHY_CLASSES = 7  # spec §14 step ③ acceptance gate


def build_failure_map(
    judge_results: Iterable[JudgeResult],
    *,
    min_why_classes: int = DEFAULT_MIN_WHY_CLASSES,
) -> dict[str, Any]:
    """Aggregate a list of ``JudgeResult`` into the failure_map dict.

    Parameters
    ----------
    judge_results
        Iterable of JudgeResult — usually from claude judge call on
        cold-start bandit's sampled trials.
    min_why_classes
        Target WHY class coverage. Default 7 (spec §14 ③). The
        resulting ``coverage_satisfied`` reflects whether the judge
        output reached this bar.

    Returns
    -------
    dict
        Structured ``failure_map`` ready for ``json.dump``.
    """
    results = list(judge_results)

    cells: dict[str, dict[str, Any]] = defaultdict(lambda: {"n_candidates": 0, "trajectory_ids": [], "candidates": []})
    l1_alerts: list[dict[str, Any]] = []
    where_distribution: dict[str, int] = defaultdict(int)
    why_distribution: dict[str, int] = defaultdict(int)
    covered_why: set[str] = set()

    n_l1 = n_l2 = n_l3 = 0
    for r in results:
        if r.issue_type == IssueType.L1:
            n_l1 += 1
            l1_alerts.append(
                {
                    "trajectory_id": r.trajectory_id,
                    "signal_description": r.signal_description,
                    "reasoning": r.proposed_action.reasoning,
                    "confidence": r.confidence,
                }
            )
            continue

        # L2 / L3: must have patch_proposal (schema invariant enforces this)
        if r.issue_type == IssueType.L2:
            n_l2 += 1
        else:
            n_l3 += 1

        action = r.proposed_action
        where = action.patch_where
        why = action.patch_why
        if where is None or why is None:
            # Defensive: schema __post_init__ should have caught this
            continue

        where_key = where.value
        # patch_why_extra carries the full sub-name including the
        # "other:" prefix per schema convention (see PatchWhy.other docstring).
        why_key = why.value if why != PatchWhy.other else (action.patch_why_extra or "other:unknown")
        where_distribution[where_key] += 1
        why_distribution[why_key] += 1
        covered_why.add(why_key)

        cell_key = f"{where_key}::{why_key}"
        cells[cell_key]["n_candidates"] += 1
        cells[cell_key]["trajectory_ids"].append(r.trajectory_id)
        cells[cell_key]["candidates"].append(
            {
                "trajectory_id": r.trajectory_id,
                "issue_type": r.issue_type.value,
                "confidence": r.confidence,
                "reasoning": action.reasoning,
                "components": [c.to_dict() for c in action.components],
            }
        )

    coverage_count = len(covered_why)
    return {
        "schema_version": SCHEMA_VERSION,
        "n_total_judged": len(results),
        "n_l1": n_l1,
        "n_l2": n_l2,
        "n_l3": n_l3,
        "covered_why_classes": sorted(covered_why),
        "covered_why_count": coverage_count,
        "min_why_classes_target": min_why_classes,
        "coverage_satisfied": coverage_count >= min_why_classes,
        "l1_alerts": l1_alerts,
        "cells": dict(cells),
        "where_distribution": dict(where_distribution),
        "why_distribution": dict(why_distribution),
    }


def write_failure_map(
    failure_map: dict[str, Any],
    out_path: str | Path,
    *,
    indent: int = 2,
) -> None:
    """Atomically write the failure_map dict to ``out_path``.

    Uses temp file + rename for crash safety (same pattern as
    ``evolver/tree/store.py``).
    """
    out_path = Path(out_path)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(failure_map, indent=indent, sort_keys=True))
    tmp.replace(out_path)


def coverage_gap(failure_map: dict[str, Any]) -> list[str]:
    """Return WHY enum values not yet covered by the judge output.

    Useful for diagnostic / decision logic in the loop: if returns
    non-empty, the cold-start bandit hasn't satisfied spec §14 step
    ③ — either rerun bandit with bigger budget or accept partial
    coverage and proceed.

    Note: this checks against the **first-class** ``PatchWhy`` enum
    values (excluding ``other``). ``other:*`` sub-names in the judge
    output do contribute to coverage_count but don't fill in the
    canonical WHY axis — the gap result names which canonical
    classes are still missing.
    """
    covered = set(failure_map.get("covered_why_classes", []))
    canonical = {w.value for w in PatchWhy if w != PatchWhy.other}
    return sorted(canonical - covered)


def candidates_for_cell(
    failure_map: dict[str, Any],
    where: PatchWhere | str,
    why: PatchWhy | str,
) -> list[dict[str, Any]]:
    """Return the ``candidates`` list for a given ``(WHERE, WHY)`` cell.

    Returns ``[]`` if the cell is empty or absent. Accepts either enum
    instances or the underlying string values.
    """
    where_key = where.value if isinstance(where, PatchWhere) else where
    why_key = why.value if isinstance(why, PatchWhy) else why
    cell_key = f"{where_key}::{why_key}"
    cell = failure_map.get("cells", {}).get(cell_key)
    if cell is None:
        return []
    return list(cell.get("candidates", []))


__all__ = [
    "SCHEMA_VERSION",
    "DEFAULT_MIN_WHY_CLASSES",
    "build_failure_map",
    "write_failure_map",
    "coverage_gap",
    "candidates_for_cell",
]
