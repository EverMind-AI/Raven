"""Bench-neutral WHY/WHERE taxonomy: the spec + open-ended induction (map-reduce).

A benchmark's diagnosis step classifies failing trajectories into a
:class:`TaxonomySpec` — WHY (failure-mode) x WHERE (patch-lever) classes. A
bench that has a hand-derived table (AppWorld's W1-W7) passes it as a frozen
constant; a brand-new bench can *discover* one from its vanilla failures via
:func:`induce_taxonomy`: stage-1 writes one open-ended failure report per
trajectory (parallel, no preset table), stage-2 clusters all reports into
classes and assigns each report to its WHY(s).

Induction failure raises :class:`TaxonomyInductionError` — there is no silent
fallback here, because the only universally-wrong answer for "a brand-new
benchmark's taxonomy" is some *other* benchmark's table. The bench wiring
decides what its safe default is (or lets the run stop loudly).
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

DEFAULT_BENCH_DESC = "an agent harness benchmark"


class TaxonomyInductionError(RuntimeError):
    """Raised when open-ended taxonomy induction produced no usable taxonomy."""


@dataclass(frozen=True)
class TaxonomySpec:
    """A benchmark's WHY (failure-mode) x WHERE (patch-lever) classification.

    ``why_classes`` / ``where_classes`` map a stable key to a one-line
    description. ``other`` (why) and ``none`` (where) are the escape hatches and
    are always present (added on construction if the source omitted them).
    """

    why_classes: dict[str, str]
    where_classes: dict[str, str]

    def __post_init__(self) -> None:
        if "other" not in self.why_classes:
            self.why_classes["other"] = "None of the above — provide a short sub-name."
        if "none" not in self.where_classes:
            self.where_classes["none"] = "No harness lever applies."

    def to_dict(self) -> dict[str, Any]:
        return {"why_classes": dict(self.why_classes), "where_classes": dict(self.where_classes)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaxonomySpec":
        return cls(dict(d["why_classes"]), dict(d["where_classes"]))


def strip_code_fence(raw: str) -> str:
    s = raw.strip()
    if "```" in s:
        s = s.split("```")[1] if s.count("```") >= 2 else s
        s = s.split("\n", 1)[-1] if s.lstrip().startswith(("json", "JSON")) else s
    return s


def _why_prefix_match(why: str, key: str) -> bool:
    """True when ``why`` carries ``key``'s leading code with a clean boundary
    (``W1_x`` matches ``W1_...`` but ``W10_x`` must not match ``W1_...``)."""
    prefix = key.split("_")[0].upper()
    w = str(why).upper()
    if not w.startswith(prefix):
        return False
    rest = w[len(prefix):len(prefix) + 1]
    return not rest.isalnum()


def coerce_mode(obj: dict, taxonomy: TaxonomySpec) -> dict:
    """Normalise one diagnosed failure mode onto the taxonomy's keys."""
    why = obj.get("why")
    if why not in taxonomy.why_classes:
        cand = next(
            (k for k in taxonomy.why_classes if why and _why_prefix_match(why, k)),
            None,
        )
        why = cand or "other"
    where = obj.get("where") if obj.get("where") in taxonomy.where_classes else "none"
    return {
        "why": why, "where": where,
        "dominant": bool(obj.get("dominant", False)),
        "reasoning": str(obj.get("reasoning", ""))[:400],
        "fix_hint": str(obj.get("fix_hint", ""))[:300],
    }


def empty_failure_map() -> dict:
    return {"why_distribution": {}, "cells": {}, "_n_judged": 0}


def add_failure_mode(fm: dict, trajectory_id: str, mode: dict) -> None:
    why, where = mode["why"], mode["where"]
    # Dominant-weighted: co-occurring secondary symptoms (near-universal on
    # failing runs) at full weight would drown the causal mode in the
    # distribution the WHY selection ranks on. Modes without the flag (legacy
    # callers) keep the old full weight.
    weight = 1.0 if mode.get("dominant", True) else 0.5
    fm["why_distribution"][why] = fm["why_distribution"].get(why, 0) + weight
    cell = fm["cells"].setdefault(f"{where}::{why}", {"candidates": []})
    cell["candidates"].append({
        "trajectory_id": trajectory_id,
        "reasoning": mode["reasoning"],
        "components": [{"summary": mode["fix_hint"]}] if mode["fix_hint"] else [],
    })


def _parse_modes(raw: str, taxonomy: TaxonomySpec) -> list[dict] | None:
    """Parse a multi-label diagnosis response into a list of modes.

    Accepts a JSON array (preferred) or a single JSON object (back-compat with
    single-label callers) -> normalised to a one-element list.
    """
    s = strip_code_fence(raw)
    obj = None
    i, j = s.find("["), s.rfind("]")
    if i >= 0 and j > i:
        try:
            obj = json.loads(s[i:j + 1])
        except json.JSONDecodeError:
            obj = None
    if obj is None:
        i, j = s.find("{"), s.rfind("}")
        if i >= 0 and j > i:
            try:
                obj = json.loads(s[i:j + 1])
            except json.JSONDecodeError:
                obj = None
    if obj is None:
        return None
    items = obj if isinstance(obj, list) else [obj]
    modes = [coerce_mode(x, taxonomy) for x in items if isinstance(x, dict)]
    if not modes:
        return None
    # Exactly one dominant mode: keep the first flagged one; when the model
    # marked none, the ordering rule ("dominant first") makes index 0 it.
    first = next((i for i, m in enumerate(modes) if m["dominant"]), 0)
    for i, m in enumerate(modes):
        m["dominant"] = i == first
    return modes


def classify_failures(
    call_fn: Callable[[list], str],
    trajectories,
    taxonomy: TaxonomySpec,
    *,
    bench_intro: str,
    extra_rules: str = "",
    max_workers: int = 8,
    retries: int = 2,
) -> dict:
    """Judge failing trajectories into a multi-label failure_map over ``taxonomy``.

    The bench-neutral diagnosis core: ``bench_intro`` describes the harness and
    task shape (one or two sentences); ``extra_rules`` optionally appends
    taxonomy-specific guidance (e.g. which WHYs are capability ceilings).
    ``trajectories`` = ``(trajectory_id, task_description, transcript)`` tuples;
    each trajectory can contribute several modes and every hit increments its
    WHY. Output shape matches ``failure_map_builder.build_failure_map`` so
    ``select_target_whys`` / the design step consume it unchanged.
    """
    sys = (
        f"{bench_intro} You are given ONE failing trajectory. Classify "
        "ALL failure modes it exhibits (usually 1-3, occasionally more) — a trajectory can fail in "
        "several ways at once. For EACH mode name its WHY class, the best patch location (WHERE), a "
        "one-line reasoning and a concrete fix hint.\n\n"
        "WHY classes:\n" + "\n".join(f"  - {k}: {v}" for k, v in taxonomy.why_classes.items()) + "\n\n"
        "WHERE classes:\n" + "\n".join(f"  - {k}: {v}" for k, v in taxonomy.where_classes.items()) + "\n\n"
        "Rules: mark EXACTLY ONE mode \"dominant\": true — the failure that directly explains the "
        "benchmark's verdict — and list it first; other modes are secondary symptoms "
        "(\"dominant\": false). " + (extra_rules + "\n" if extra_rules else "") +
        "Respond with ONLY a JSON ARRAY, no prose, no code fences; each element is one mode:\n"
        '[{"why":"<one WHY key>","where":"<one WHERE key>","dominant":true|false,'
        '"reasoning":"<=1 line","fix_hint":"<=1 line concrete lever>"}]'
    )

    def _one(t):
        tid, desc, transcript = t
        user = (
            f"TASK: {desc}\n\nFAILING TRAJECTORY (trajectory_id={tid}):\n{transcript}\n\n"
            "Classify ALL failure modes. JSON array only."
        )
        msgs = [{"role": "system", "content": sys}, {"role": "user", "content": user}]
        for _ in range(retries + 1):
            try:
                modes = _parse_modes(call_fn(msgs), taxonomy)
            except Exception:  # noqa: BLE001
                modes = None
            if modes:
                return tid, modes
            msgs = msgs + [
                {"role": "user", "content": "Return ONLY a JSON array of {why,where,...} with valid keys."}
            ]
        return None

    fm = empty_failure_map()
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for r in ex.map(_one, list(trajectories)):
            if not r:
                continue
            tid, modes = r
            fm["_n_judged"] += 1
            for mode in modes:
                add_failure_mode(fm, tid, mode)
    return fm


# ---- open-ended taxonomy induction (map-reduce; only for a new benchmark) ----


def _parse_reports(raw: str) -> list[dict] | None:
    """Stage-1 report parse: list of {failure_point, evidence, fixes:[...]}."""
    s = strip_code_fence(raw)
    i, j = s.find("["), s.rfind("]")
    if i < 0 or j <= i:
        i, j = s.find("{"), s.rfind("}")
        if i < 0 or j <= i:
            return None
        s = "[" + s[i:j + 1] + "]"
    else:
        s = s[i:j + 1]
    try:
        arr = json.loads(s)
    except json.JSONDecodeError:
        return None
    return [x for x in arr if isinstance(x, dict)] or None


def _induce_reports(call_fn, trajectories, *, bench_desc, max_workers, retries) -> list[dict]:
    """Stage 1 (map): one open-ended failure report per trajectory (parallel)."""
    sys = (
        f"You analyse ONE failing agent trajectory from {bench_desc} with NO preset failure "
        "taxonomy. Describe every distinct way it failed (usually 1-3). For each: the concrete "
        "failure_point (what went wrong, at which step), evidence (quote one line from the "
        "trajectory), and harness_fixes (1-2 concrete ways an agent-harness change — prompt / a "
        "runtime hook / the exec tool / the loop — could prevent it; NOT model retraining). Keep "
        "each field short.\n"
        'Respond ONLY a JSON array: [{"failure_point":"...","evidence":"...","harness_fixes":["...","..."]}]'
    )

    def _one(t):
        tid, desc, transcript = t
        user = f"TASK: {desc}\n\nFAILING TRAJECTORY ({tid}):\n{transcript}\n\nReport its failure modes. JSON array only."
        msgs = [{"role": "system", "content": sys}, {"role": "user", "content": user}]
        for _ in range(retries + 1):
            try:
                reps = _parse_reports(call_fn(msgs))
            except Exception:  # noqa: BLE001
                reps = None
            if reps:
                return {"trajectory_id": tid, "modes": reps}
            msgs = msgs + [{"role": "user", "content": "Return ONLY the JSON array."}]
        return None

    out = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for r in ex.map(_one, list(trajectories)):
            if r:
                out.append(r)
    return out


def _parse_taxonomy(raw: str) -> tuple[TaxonomySpec, list[dict]]:
    """Stage-2 reduce parse: TaxonomySpec + per-report assignments (multi-label)."""
    s = strip_code_fence(raw)
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("no JSON object in taxonomy reduce output")
    obj = json.loads(s[i:j + 1])
    why = {c["key"]: str(c.get("desc", "")) for c in obj.get("why_classes", []) if c.get("key")}
    where = {c["key"]: str(c.get("desc", "")) for c in obj.get("where_classes", []) if c.get("key")}
    if not why:
        raise ValueError("reduce produced no why_classes")
    assignments = [a for a in obj.get("assignments", []) if isinstance(a, dict)]
    return TaxonomySpec(why, where), assignments


def _pack_reports(reports: list[dict], *, budget: int) -> str:
    """JSON-array payload of WHOLE reports within ``budget`` chars.

    A raw ``json.dumps(reports)[:budget]`` cut mid-record, leaving invalid
    JSON and silently biasing the taxonomy toward early trajectories; packing
    whole records keeps the payload parseable and makes any drop explicit
    (the trailing marker names how many were left out).
    """
    parts: list[str] = []
    size = 2  # brackets
    dropped = 0
    for r in reports:
        s = json.dumps(r)
        if parts and size + len(s) + 2 > budget:
            dropped += 1
            continue
        parts.append(s)
        size += len(s) + 2
    payload = "[" + ", ".join(parts) + "]"
    if dropped:
        payload += f"\n({dropped} more reports omitted for length)"
    return payload


def induce_taxonomy(
    call_fn: Callable[[list], str],
    trajectories,
    *,
    bench_desc: str = DEFAULT_BENCH_DESC,
    max_workers: int = 8,
    retries: int = 2,
    target_min: int = 5,
    target_max: int = 9,
) -> tuple[TaxonomySpec, dict]:
    """Discover a WHY/WHERE taxonomy from vanilla failures (open-ended map-reduce).

    Stage 1 (map): one compact failure report per trajectory, no preset table.
    Stage 2 (reduce): cluster all reports into ``target_min..target_max`` WHY
    classes + WHERE lever classes, and assign each report to its WHY(s). Returns
    the :class:`TaxonomySpec` plus a seed multi-label failure_map from the
    assignments. Raises :class:`TaxonomyInductionError` when no report parses or
    the reduce never yields a taxonomy — never silently substitutes another
    bench's table.
    """
    reports = _induce_reports(
        call_fn, trajectories, bench_desc=bench_desc, max_workers=max_workers, retries=retries
    )
    if not reports:
        raise TaxonomyInductionError(
            "taxonomy induction produced no parseable per-trajectory reports"
        )

    sys = (
        "You are consolidating many per-trajectory failure reports into a REUSABLE failure "
        f"taxonomy for {bench_desc}. Cluster the reports into "
        f"{target_min}-{target_max} WHY classes (abstract failure MODES, each a stable key like "
        "'W1_...' + a one-line description) and a small set of WHERE classes (harness patch LEVERS "
        "abstracted from the fixes: prompt / runtime hook / exec tool / loop / config / none). Then "
        "assign each report (by trajectory_id) to its WHY key(s) — a report may map to several.\n"
        'Respond ONLY JSON: {"why_classes":[{"key":"W1_...","desc":"..."}],'
        '"where_classes":[{"key":"...","desc":"..."}],'
        '"assignments":[{"trajectory_id":"...","whys":["W1_..."],"wheres":["..."]}]}'
    )
    payload = _pack_reports(reports, budget=60000)
    msgs = [
        {"role": "system", "content": sys},
        {"role": "user", "content": f"Reports:\n{payload}\n\nProduce the taxonomy JSON."},
    ]
    taxonomy: TaxonomySpec | None = None
    assignments: list[dict] = []
    last_exc: Exception | None = None
    for _ in range(retries + 1):
        try:
            taxonomy, assignments = _parse_taxonomy(call_fn(msgs))
            break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            msgs = msgs + [{"role": "user", "content": f"Invalid ({exc}). Return ONLY the JSON object."}]
    if taxonomy is None:
        raise TaxonomyInductionError(
            f"taxonomy reduce failed after {retries + 1} attempts; last error: {last_exc!r}"
        )

    # The seed carries the stage-1 report content (failure_point / harness_fixes)
    # into its cells, so a round-1 design step can consume it directly — the
    # trajectories were just judged, re-judging them would only re-spend the driver.
    seed = empty_failure_map()
    modes_by_tid = {r["trajectory_id"]: r["modes"] for r in reports}
    for a in assignments:
        tid = a.get("trajectory_id")
        if tid not in modes_by_tid:
            continue
        seed["_n_judged"] += 1
        whys = a.get("whys") or []
        wheres = a.get("wheres") or ["none"]
        reps = modes_by_tid[tid]
        for i, w in enumerate(whys):
            rep = reps[i] if i < len(reps) else reps[0]
            fixes = rep.get("harness_fixes") or []
            add_failure_mode(seed, tid, coerce_mode(
                {
                    "why": w, "where": wheres[0], "dominant": i == 0,
                    "reasoning": str(rep.get("failure_point", "")),
                    "fix_hint": str(fixes[0]) if fixes else "",
                },
                taxonomy,
            ))
    return taxonomy, seed


def ensure_taxonomy(
    call_fn: Callable[[list], str],
    trajectories,
    path: str | Path,
    *,
    mode: str = "hardcoded",
    default: Optional[TaxonomySpec] = None,
    bench_desc: str = DEFAULT_BENCH_DESC,
    max_workers: int = 8,
    seed_path: Optional[str | Path] = None,
) -> TaxonomySpec:
    """Resolve the taxonomy for a bench: hardcoded default, or induce-and-cache.

    ``mode="hardcoded"`` returns ``default`` (required — the bench's own table).
    ``mode="induce"`` loads ``path`` if it exists, else runs
    :func:`induce_taxonomy` over ``trajectories`` and persists the result to
    ``path`` — induction runs once and every later call reuses the frozen
    taxonomy. Induction failure raises; it never falls back to ``default``.

    ``seed_path`` (optional) persists the induction's seed failure map next to
    the taxonomy, so the caller can feed it to round 1 instead of re-judging the
    very trajectories induction just judged (round-0 is genuinely free). It is
    written only when induction actually runs; a cached taxonomy leaves any
    previously written seed in place.
    """
    if mode == "hardcoded":
        if default is None:
            raise ValueError('mode="hardcoded" requires a default TaxonomySpec')
        return default
    p = Path(path)
    if p.exists():
        return TaxonomySpec.from_dict(json.loads(p.read_text()))
    taxonomy, seed = induce_taxonomy(
        call_fn, trajectories, bench_desc=bench_desc, max_workers=max_workers
    )
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(taxonomy.to_dict(), indent=2))
    if seed_path is not None:
        sp = Path(seed_path)
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(seed, indent=2))
    return taxonomy


def resolve_taxonomy(
    call_fn: Callable[[list], str],
    trajectory_source: Callable[[int, Any], list],
    vanilla_node: Any,
    *,
    mode: str,
    work_dir: str | Path,
    hardcoded: Optional[TaxonomySpec] = None,
    taxonomy_path: Optional[str | Path] = None,
) -> tuple[TaxonomySpec, Optional[dict]]:
    """Resolve the round's taxonomy + (for induce) a round-1 seed failure map.

    The shared front half of both benches' diagnose wiring. In ``"hardcoded"``
    mode returns the caller-supplied ``hardcoded`` table and no seed. In
    ``"induce"`` mode discovers the table once from the vanilla failures (cached
    to ``taxonomy_path`` / ``work_dir/taxonomy.json``) and, because induction
    judges those failures already, returns its seed failure map marked with the
    root as diagnosed — so round 1 reuses it instead of re-judging the same
    trajectories (round-0 free). Requires the vanilla ledger to already exist
    (cold start run) so ``trajectory_source`` has trajectories to read.
    """
    if mode == "hardcoded":
        if hardcoded is None:
            raise ValueError('resolve_taxonomy mode="hardcoded" requires a table')
        return hardcoded, None
    tax_path = Path(taxonomy_path) if taxonomy_path else (Path(work_dir) / "taxonomy.json")
    seed_path = tax_path.with_name(tax_path.stem + "_seed.json")
    taxonomy = ensure_taxonomy(
        call_fn, trajectory_source(1, vanilla_node),
        tax_path, mode="induce", seed_path=seed_path,
    )
    seed_failure_map: Optional[dict] = None
    if seed_path.exists():
        seed = json.loads(seed_path.read_text())
        if seed.get("_n_judged"):
            seed["_diagnosed_parents"] = [vanilla_node.node_id]
            seed_failure_map = seed
    return taxonomy, seed_failure_map


__all__ = [
    "TaxonomySpec",
    "TaxonomyInductionError",
    "DEFAULT_BENCH_DESC",
    "strip_code_fence",
    "coerce_mode",
    "empty_failure_map",
    "add_failure_mode",
    "classify_failures",
    "induce_taxonomy",
    "ensure_taxonomy",
    "resolve_taxonomy",
]
