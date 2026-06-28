#!/usr/bin/env python3
"""
pa_scorecard.py — Aggregate Planner / Raven Agent / Hermes Agent results
into one markdown scorecard.

Each input is a JSON file emitted by the corresponding adapter (same row
schema: category, predicted_help, help_match, agent.parse_ok, etc.).
Handles missing inputs gracefully — omits the column with a dash.

Usage:
    uv run python proactivity-eval/runners/pa_scorecard.py \\
        --planner-cold /tmp/pa-cold.json \\
        --planner-warm /tmp/pa-warm.json \\
        --ec-agent-cold proactivity-eval/output/pa-ec-agent-cold-smoke.json \\
        --ec-agent-warm proactivity-eval/output/pa-ec-agent-warm-smoke.json \\
        --hermes-cold proactivity-eval/output/pa-hermes-agent-cold-smoke.json \\
        --hermes-warm proactivity-eval/output/pa-hermes-agent-warm-smoke.json \\
        --output proactivity-eval/output/pa-scorecard-smoke.md
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

CATEGORIES = [
    "Correct-Detection (CD)",
    "Correct-Rejection (CR)",
    "Missed-Need (MN)",
    "False-Alarm (FA)",
]

# Deltas are shown as cold→warm for these system bases; any key that exists
# in `data` is rendered in the main tables, but only (base)_cold / (base)_warm
# pairs appear in the deltas section.
DEFAULT_COLUMN_LABELS: dict[str, str] = {
    "planner_cold": "Planner cold",
    "planner_warm": "Planner warm",
    "ec_agent_cold": "EC Agent cold",
    "ec_agent_warm": "EC Agent warm",
    "ec_sentinel_cold": "EC Sentinel cold",
    "ec_sentinel_warm": "EC Sentinel warm",
    "hermes_cold": "Hermes cold",
    "hermes_warm": "Hermes warm",
    "openclaw_cold": "OpenClaw cold",
    "openclaw_warm": "OpenClaw warm",
}


def _load(path: str | None) -> list[dict] | None:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except json.JSONDecodeError:
        return None


def _help_match_stats(rows: list[dict]) -> dict[str, Any]:
    """Compute help_match rate overall and per category.

    Also computes confusion-matrix quantities (TP/FP/TN/FN) treating
    help_needed as ground truth.
    """
    total = len(rows)
    total_match = sum(1 for r in rows if r.get("help_match"))

    by_cat = defaultdict(list)
    for r in rows:
        by_cat[r.get("category", "?")].append(r)

    per_cat: dict[str, tuple[int, int]] = {}
    for cat in CATEGORIES:
        cat_rows = by_cat.get(cat, [])
        if cat_rows:
            per_cat[cat] = (sum(1 for r in cat_rows if r.get("help_match")), len(cat_rows))
        else:
            per_cat[cat] = (0, 0)

    TP = FP = TN = FN = 0
    parse_failures = 0
    for r in rows:
        pred = r.get("predicted_help")
        truth = r.get("truth_help_needed")
        if r.get("agent", {}).get("parse_ok") is False:
            parse_failures += 1
        if pred and truth:
            TP += 1
        elif pred and not truth:
            FP += 1
        elif not pred and not truth:
            TN += 1
        elif not pred and truth:
            FN += 1

    eps = 1e-8
    precision = TP / (TP + FP + eps)
    recall = TP / (TP + FN + eps)
    accuracy = (TP + TN) / (TP + TN + FP + FN + eps)
    f1 = 2 * precision * recall / (precision + recall + eps)
    false_alarm = FP / (TP + FP + eps)

    return {
        "total": total,
        "match": total_match,
        "per_cat": per_cat,
        "TP": TP, "FP": FP, "TN": TN, "FN": FN,
        "precision": precision,
        "recall": recall,
        "accuracy": accuracy,
        "f1": f1,
        "false_alarm": false_alarm,
        "parse_failures": parse_failures,
    }


def _fmt_rate(num: int, den: int) -> str:
    if den == 0:
        return "—"
    return f"{num}/{den}"


def _fmt_pct(num: int, den: int) -> str:
    if den == 0:
        return "—"
    return f"{num/den*100:.0f}%"


def _fmt_f1(f1: float, n: int) -> str:
    if n == 0:
        return "—"
    return f"{f1:.3f}"


def _column_label(key: str, labels: dict[str, str]) -> str:
    if key in labels:
        return labels[key]
    # Fall back to a Title Cased version of the key ("openclaw_warm" → "OpenClaw warm").
    base, _, mode = key.rpartition("_")
    if mode in ("cold", "warm") and base:
        return f"{base.replace('_', ' ').title()} {mode}"
    return key.replace("_", " ").title()


def build_scorecard(
    data: dict[str, list[dict] | None],
    column_labels: dict[str, str] | None = None,
) -> str:
    """Return a markdown scorecard given a dict of column_key → rows.

    column_labels: optional override for the pretty name of each key.
    Keys present in ``data`` but absent from labels use a Title Cased fallback.
    """
    labels = {**DEFAULT_COLUMN_LABELS, **(column_labels or {})}
    stats = {k: (_help_match_stats(v) if v else None) for k, v in data.items()}

    out: list[str] = []
    out.append("# ProactiveBench Scorecard (reward_data S1 protocol)")
    out.append("")
    out.append(f"Dataset: `data/pbench/test_data.jsonl` (stratified CD/CR/MN/FA)")
    first_present = next((s for s in stats.values() if s), None)
    if first_present:
        out.append(f"Sample size: N = {first_present['total']}")
    out.append("")

    # Header row — one column per input key that actually has rows, preserving
    # insertion order so the caller controls column layout via dict ordering.
    out.append("## Overall metrics")
    out.append("")
    cols_present = [(k, _column_label(k, labels)) for k in data if stats.get(k)]
    if not cols_present:
        out.append("*No data provided — all columns empty.*")
        return "\n".join(out)

    headers = ["Metric"] + [label for _, label in cols_present]
    out.append("| " + " | ".join(headers) + " |")
    out.append("|" + "|".join(["---"] * len(headers)) + "|")

    # Rows
    def row(label: str, values: list[str]) -> str:
        return "| " + " | ".join([label, *values]) + " |"

    out.append(row("Accuracy (help_match)",
                   [_fmt_pct(stats[k]["match"], stats[k]["total"])
                    for k, _ in cols_present]))
    out.append(row("F1",
                   [_fmt_f1(stats[k]["f1"], stats[k]["total"])
                    for k, _ in cols_present]))
    out.append(row("Precision",
                   [f"{stats[k]['precision']:.3f}" if stats[k]["total"] else "—"
                    for k, _ in cols_present]))
    out.append(row("Recall",
                   [f"{stats[k]['recall']:.3f}" if stats[k]["total"] else "—"
                    for k, _ in cols_present]))
    out.append(row("False-alarm rate",
                   [f"{stats[k]['false_alarm']:.3f}" if stats[k]["total"] else "—"
                    for k, _ in cols_present]))
    out.append(row("Parse failures",
                   [str(stats[k]["parse_failures"])
                    for k, _ in cols_present]))

    out.append("")
    out.append("## Per-category help_match (match / total)")
    out.append("")
    out.append("| Category | " + " | ".join(label for _, label in cols_present) + " |")
    out.append("|" + "|".join(["---"] * (len(cols_present) + 1)) + "|")
    for cat in CATEGORIES:
        cells = []
        for k, _ in cols_present:
            num, den = stats[k]["per_cat"][cat]
            cells.append(_fmt_rate(num, den))
        out.append("| " + cat + " | " + " | ".join(cells) + " |")

    out.append("")
    out.append("## Confusion matrix (TP / FP / TN / FN)")
    out.append("")
    out.append("| System | TP | FP | TN | FN |")
    out.append("|---|---|---|---|---|")
    for k, label in cols_present:
        s = stats[k]
        out.append(f"| {label} | {s['TP']} | {s['FP']} | {s['TN']} | {s['FN']} |")

    # Error-mode deltas — one row per system base that has BOTH cold + warm.
    out.append("")
    out.append("## Cold → Warm deltas")
    out.append("")
    # Discover system bases: "<base>_cold" / "<base>_warm" pairs in data.
    bases = sorted({
        k.rsplit("_", 1)[0]
        for k in data
        if k.endswith("_cold") or k.endswith("_warm")
    })
    out.append("| System | Δ accuracy | Δ F1 | Δ false-alarm |")
    out.append("|---|---|---|---|")
    for base in bases:
        cold_k, warm_k = f"{base}_cold", f"{base}_warm"
        display = _column_label(cold_k, labels).removesuffix(" cold").removesuffix(" Cold")
        cs, ws = stats.get(cold_k), stats.get(warm_k)
        if not cs or not ws or cs["total"] == 0 or ws["total"] == 0:
            out.append(f"| {display} | — | — | — |")
            continue
        d_acc = ws["match"]/ws["total"] - cs["match"]/cs["total"]
        d_f1 = ws["f1"] - cs["f1"]
        d_fa = ws["false_alarm"] - cs["false_alarm"]
        out.append(f"| {display} | {d_acc:+.1%} | {d_f1:+.3f} | {d_fa:+.3f} |")

    # Timing / parse if available (agent fields only)
    out.append("")
    out.append("## Per-system timing & parse health")
    out.append("")
    out.append("| System | records | mean elapsed | parse failures |")
    out.append("|---|---|---|---|")
    for k, label in cols_present:
        rows_k = data[k] or []
        elapsed = [r.get("agent", {}).get("elapsed_s")
                   for r in rows_k if r.get("agent", {}).get("elapsed_s") is not None]
        mean_e = f"{sum(elapsed)/len(elapsed):.1f}s" if elapsed else "—"
        pf = stats[k]["parse_failures"] if stats[k] else "—"
        out.append(f"| {label} | {len(rows_k)} | {mean_e} | {pf} |")

    return "\n".join(out)


def _parse_input_spec(specs: list[str]) -> dict[str, str]:
    """Parse ``NAME=PATH`` strings into a dict, preserving CLI order."""
    out: dict[str, str] = {}
    for s in specs:
        if "=" not in s:
            raise SystemExit(f"--input expects NAME=PATH, got: {s}")
        name, path = s.split("=", 1)
        name = name.strip()
        path = path.strip()
        if not name or not path:
            raise SystemExit(f"--input NAME=PATH needs both parts: {s}")
        out[name] = path
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Aggregate reward_data adapter outputs into a markdown scorecard."
    )
    ap.add_argument(
        "--input", action="append", default=[], metavar="NAME=PATH",
        help=("one per system/mode, e.g. "
              "--input ec_agent_warm=output/pa-ec-agent-warm.json. "
              "Repeat for each system. Names ending in _cold or _warm are "
              "grouped in the cold→warm deltas table."),
    )
    # Legacy shorthands — kept so old scripts keep working. Each expands into
    # an entry in the `data` dict with the corresponding key.
    ap.add_argument("--planner-cold", default=None)
    ap.add_argument("--planner-warm", default=None)
    ap.add_argument("--ec-agent-cold", default=None)
    ap.add_argument("--ec-agent-warm", default=None)
    ap.add_argument("--ec-sentinel-cold", default=None)
    ap.add_argument("--ec-sentinel-warm", default=None)
    ap.add_argument("--hermes-cold", default=None)
    ap.add_argument("--hermes-warm", default=None)
    ap.add_argument("--openclaw-cold", default=None)
    ap.add_argument("--openclaw-warm", default=None)
    ap.add_argument("--output", default=None)
    args = ap.parse_args()

    specs = _parse_input_spec(args.input)
    for flag_name in (
        "planner_cold", "planner_warm", "ec_agent_cold", "ec_agent_warm",
        "ec_sentinel_cold", "ec_sentinel_warm",
        "hermes_cold", "hermes_warm", "openclaw_cold", "openclaw_warm",
    ):
        val = getattr(args, flag_name)
        if val:
            specs.setdefault(flag_name, val)

    data = {k: _load(v) for k, v in specs.items()}

    md = build_scorecard(data)
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding='utf-8')
        print(f"PA scorecard saved to {out}")
    else:
        print(md)


if __name__ == "__main__":
    main()
