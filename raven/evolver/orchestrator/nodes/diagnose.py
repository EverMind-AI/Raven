"""Step ① — diagnose failing trajectories into a failure map (semantic).

This is the first semantic step, and it reuses the canonical judge stack rather
than re-inventing it: the judge already has a system prompt
(:func:`build_judge_messages`), a defect-tolerant parser
(:func:`parse_judge_output`, which strips code fences and enforces the L1/L2/L3
schema invariants), and a failure-map aggregator
(:func:`build_failure_map`). All this node adds is the bounded repair-retry from
:class:`SemanticNode`, so a weak driver that emits malformed judge JSON gets the
parse error fed back and retries instead of derailing the round.

``diagnose_trajectory`` judges one trajectory into a :class:`JudgeResult`;
``diagnose_round`` judges a batch and folds them into the cross-round failure
map. The result is a plain dict — the loop persists and re-reads it, so no model
holds diagnosis state across rounds.
"""

from __future__ import annotations

from typing import Any, Sequence

from raven.evolver.analysis.failure_map_builder import build_failure_map
from raven.evolver.judge.parser import JudgeParseError, parse_judge_output
from raven.evolver.judge.prompts import build_judge_messages
from raven.evolver.judge.schema import JudgeResult
from raven.evolver.orchestrator.nodes.semantic import CallFn, SemanticNode


def diagnose_trajectory(
    call_fn: CallFn,
    *,
    trajectory_id: str,
    task_description: str,
    trajectory_text: str,
    max_retries: int = 3,
) -> JudgeResult:
    """Judge one trajectory into a validated :class:`JudgeResult`."""
    messages = build_judge_messages(
        trajectory_id=trajectory_id,
        task_description=task_description,
        trajectory_text=trajectory_text,
    )
    node: SemanticNode[JudgeResult] = SemanticNode(
        name=f"diagnose:{trajectory_id}",
        call_fn=call_fn,
        parse_fn=lambda raw: parse_judge_output(raw, expected_trajectory_id=trajectory_id),
        parse_error_types=(JudgeParseError,),
        max_retries=max_retries,
    )
    return node.run(messages)


def diagnose_round(
    call_fn: CallFn,
    trajectories: Sequence[tuple[str, str, str]],
    *,
    min_why_classes: int = 7,
    max_retries: int = 3,
) -> dict[str, Any]:
    """Judge a batch of ``(trajectory_id, task_description, trajectory_text)`` and
    aggregate into a failure map.

    A trajectory whose diagnosis fails to parse after all retries is skipped
    (its id collected under ``_diagnose_failures``) rather than aborting the
    round — one unparseable trajectory should not sink a round's diagnosis.
    """
    results: list[JudgeResult] = []
    failures: list[str] = []
    for trajectory_id, task_description, trajectory_text in trajectories:
        try:
            results.append(
                diagnose_trajectory(
                    call_fn,
                    trajectory_id=trajectory_id,
                    task_description=task_description,
                    trajectory_text=trajectory_text,
                    max_retries=max_retries,
                )
            )
        except Exception:  # noqa: BLE001 — record and continue; see docstring
            failures.append(trajectory_id)

    failure_map = build_failure_map(results, min_why_classes=min_why_classes)
    if failures:
        failure_map["_diagnose_failures"] = failures
    return failure_map


def merge_failure_maps(acc: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Append ``new`` into the accumulated failure map (cross-round live map).

    The failure map is meant to accumulate across rounds (SOP §2 ①: a live map
    that accumulates across rounds), so WHY-distribution shifts are auditable
    over the evolution. Cells
    are merged by ``WHERE::WHY`` key (trajectory ids / candidates concatenated,
    counts summed); distributions and totals are summed; covered WHY classes are
    unioned. ``acc`` empty returns a copy of ``new``.
    """
    if not acc:
        return dict(new)
    m = dict(acc)
    for k in ("n_total_judged", "n_l1", "n_l2", "n_l3"):
        m[k] = acc.get(k, 0) + new.get(k, 0)
    if "_n_judged" in acc or "_n_judged" in new:
        m["_n_judged"] = acc.get("_n_judged", 0) + new.get("_n_judged", 0)
    diag_failures = list(acc.get("_diagnose_failures", [])) + list(
        new.get("_diagnose_failures", [])
    )
    if diag_failures:
        m["_diagnose_failures"] = diag_failures

    cells = {k: dict(v) for k, v in acc.get("cells", {}).items()}
    for key, cell in new.get("cells", {}).items():
        if key in cells:
            base = cells[key]
            base["n_candidates"] = base.get("n_candidates", 0) + cell.get("n_candidates", 0)
            base["trajectory_ids"] = list(base.get("trajectory_ids", [])) + list(
                cell.get("trajectory_ids", [])
            )
            base["candidates"] = list(base.get("candidates", [])) + list(
                cell.get("candidates", [])
            )
        else:
            cells[key] = dict(cell)
    m["cells"] = cells

    for dist in ("where_distribution", "why_distribution"):
        d = dict(acc.get(dist, {}))
        for k, v in new.get(dist, {}).items():
            d[k] = d.get(k, 0) + v
        m[dist] = d

    covered = sorted(
        set(acc.get("covered_why_classes", [])) | set(new.get("covered_why_classes", []))
    )
    m["covered_why_classes"] = covered
    m["covered_why_count"] = len(covered)
    m["l1_alerts"] = list(acc.get("l1_alerts", [])) + list(new.get("l1_alerts", []))
    return m


__all__ = ["diagnose_trajectory", "diagnose_round", "merge_failure_maps"]
