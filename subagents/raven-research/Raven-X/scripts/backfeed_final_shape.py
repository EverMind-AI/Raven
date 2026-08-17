#!/usr/bin/env python3
"""Back-feed the dr@2.5 shaping hop over already-recorded trajectories.

Why this script exists rather than a test fixture. A shaping stage is exactly
where a framework silently loses answers: the upstream build we benchmarked
carried gold on 71/120 questions into its final summary and 58/120 into its
boxed field - it produced nothing new and dropped 10.83pp. A unit test proves
the guard fires on inputs we thought of. Running the hop over a real batch's
recorded answers proves it on inputs nobody designed.

The acceptance criterion is one number: **shorter_lossy must be 0.** Anything
else is a reproduction of that failure class and the change must not merge.

Usage
    <venv>/python scripts/backfeed_final_shape.py <arm_dir> [<arm_dir> ...]
        [--closing-tag-required] [--only-answerless] [--json out.json]

``<arm_dir>`` is a directory holding ``traj_raw.jsonl`` (or the file itself).
Pass ``--closing-tag-required`` for arms that ran with
``drFlow.think_closing_tag_required`` on; the hop must read the same visible
answer its arm was judged on, and the anchor runs with it off.

Read-only. Never writes into the batch directory.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from raven.agent.flow.answer_text import visible_answer  # noqa: E402
from raven.agent.flow.final_shape import shape_final_answer  # noqa: E402

_WS = re.compile(r"\s+")
# Any marker anywhere in the RAW text, including inside a think block. The
# conservative hop deliberately does NOT read these - it only shapes what is
# already visible. Counting them here prices the ceiling of an aggressive
# variant without building one.
_ANY_MARKER = re.compile(
    r"<answer>|\\boxed\s*\{|(?:\A|\n)[ \t>*_]*(?:\*\*)?\s*"
    r"(?:final\s+answer|answer|最终答案|答案)\s*(?:\*\*)?\s*[:：]",
    re.I,
)


def _content(s: str | None) -> int:
    return len(_WS.sub("", s or ""))


def rows(path: Path):
    f = path / "traj_raw.jsonl" if path.is_dir() else path
    if not f.exists():
        raise SystemExit(f"!! no traj_raw.jsonl at {path} (this is an apparatus error, not a verdict)")
    for line in f.open():
        line = line.strip()
        if line:
            yield json.loads(line)


def run(paths: list[Path], *, closing_tag_required: bool, only_answerless: bool) -> dict:
    buckets = Counter()
    forms = Counter()
    reasons = Counter()
    causes = Counter()
    lossy_examples: list[dict] = []
    rescued_examples: list[dict] = []
    n = 0
    raw_has_text = 0
    raw_has_marker = 0

    for p in paths:
        for r in rows(p):
            raw = r.get("final_answer")
            vis_before = visible_answer(raw, closing_tag_required=closing_tag_required)
            answerless = not vis_before
            if only_answerless and not answerless:
                continue
            n += 1
            if answerless:
                causes[r.get("answerless_cause") or "(none recorded)"] += 1
                if _content(raw):
                    raw_has_text += 1
                if raw and _ANY_MARKER.search(raw):
                    raw_has_marker += 1

            res = shape_final_answer(raw, closing_tag_required=closing_tag_required)
            forms[res.form] += 1
            reasons[res.reason] += 1

            # The three buckets the acceptance asks for.
            if not res.text.strip():
                buckets["still_empty"] += 1
            if _content(res.text) < _content(vis_before) or res.reason == "refused_shorter":
                buckets["shorter_lossy"] += 1
                if len(lossy_examples) < 5:
                    lossy_examples.append(
                        {
                            "qid": r.get("qid"),
                            "reason": res.reason,
                            "visible_chars": _content(vis_before),
                            "shaped_chars": _content(res.text),
                        }
                    )
            if answerless and res.text.strip():
                buckets["rescued"] += 1
                if len(rescued_examples) < 5:
                    rescued_examples.append({"qid": r.get("qid"), "form": res.form, "text_head": res.text[:160]})
            if res.marked:
                buckets["marked"] += 1
            if res.shaped:
                buckets["shaped"] += 1

    return {
        "n_rows": n,
        "closing_tag_required": closing_tag_required,
        "only_answerless": only_answerless,
        "buckets": dict(buckets),
        "forms": dict(forms),
        "reasons": dict(reasons),
        "answerless_causes": dict(causes),
        "answerless_raw_has_text": raw_has_text,
        "answerless_raw_has_marker_somewhere": raw_has_marker,
        "lossy_examples": lossy_examples,
        "rescued_examples": rescued_examples,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("arms", nargs="+", type=Path)
    ap.add_argument("--closing-tag-required", action="store_true")
    ap.add_argument("--only-answerless", action="store_true")
    ap.add_argument("--json", default="")
    a = ap.parse_args()

    out = run(a.arms, closing_tag_required=a.closing_tag_required, only_answerless=a.only_answerless)
    b = out["buckets"]
    n = out["n_rows"]

    print(
        f"=== back-feed dr@2.5 final-shape  rows={n} "
        f"closing_tag_required={out['closing_tag_required']} "
        f"only_answerless={out['only_answerless']} ==="
    )
    print(f"  still_empty    {b.get('still_empty', 0):5d}   shaping produced nothing (input had nothing)")
    print(f"  shorter_lossy  {b.get('shorter_lossy', 0):5d}   <-- MUST BE 0")
    print(f"  rescued        {b.get('rescued', 0):5d}   was answerless, now has text")
    print(
        f"  marked         {b.get('marked', 0):5d}   model emitted an explicit marker"
        + (f"  ({100 * b.get('marked', 0) / n:.1f}%)" if n else "")
    )
    print(f"  shaped         {b.get('shaped', 0):5d}   canonical answer line appended")
    print("  forms          " + "  ".join(f"{k}={v}" for k, v in sorted(out["forms"].items())))
    print("  reasons        " + "  ".join(f"{k}={v}" for k, v in sorted(out["reasons"].items())))
    if out["answerless_causes"]:
        print("  answerless     " + "  ".join(f"{k}={v}" for k, v in sorted(out["answerless_causes"].items())))
        print(
            f"  ceiling probe  raw text present on {out['answerless_raw_has_text']}, "
            f"marker somewhere in raw on {out['answerless_raw_has_marker_somewhere']}"
            "  (the conservative hop reads neither - this prices an aggressive variant)"
        )
    for e in out["lossy_examples"]:
        print(f"  !! LOSSY {e}")

    if a.json:
        Path(a.json).write_text(json.dumps(out, ensure_ascii=False, indent=2))
        print(f"  wrote {a.json}")

    rc = 1 if b.get("shorter_lossy", 0) else 0
    print(f"=== rc={rc} " + ("(shorter_lossy != 0 -> do not merge)" if rc else "(no information loss)") + " ===")
    sys.exit(rc)


if __name__ == "__main__":
    main()
