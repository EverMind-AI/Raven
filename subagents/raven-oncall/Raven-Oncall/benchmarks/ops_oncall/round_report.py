#!/usr/bin/env python3
"""Read one on-call round's trail and print the pre-registered numbers.

The round-2 design reuses round 1's A arm as the counterfactual instead of
re-running it. That is only valid if the training side is bit-deterministic, so
the first thing this prints is the three-point check -- and a mismatch is a hard
failure, because it means the reused A curve is not this run's counterfactual and
every A-vs-B difference below it is void.

Two rules this script follows, both from the contamination protocol:

  - **raw readings only.** It prints values, positions and differences. It does
    not say whether a curve peaked, whether the loop should have stopped, or
    which arm won. Deciding that is the reader's job, and a script that decided
    it would be deciding it for the agent as well.
  - **every proportion carries its denominator.** Any rate is printed as
    ``n/total`` with the count it could not classify next to it. A bare
    percentage here would hide exactly the gaps that matter.

Usage:
    python round_report.py --events ~/.raven/ops/armb-embed-r2/events.jsonl \\
                           --progress ./progress-r2.jsonl \\
                           [--spans ~/.raven/traces/logs/audit-spans.log] \\
                           [--official official_eval.json]

Self-test against round 1's A arm, where the three-point check must pass and the
intervention list must be empty:

    python round_report.py --events <armA events> --progress <armA progress> --self-test
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Round 1's A arm: the sealed counterfactual (raven-armA-SEALED-2026-08-04.md
# section 4). Both columns are needed -- the value to compare against, and the
# cumulative GPU minutes that say where in the budget a step sits.
ARM_A_CURVE: tuple[tuple[int, float, float], ...] = (
    (200, 0.3527, 4.00),
    (400, 0.3583, 7.97),
    (600, 0.3538, 11.92),
    (800, 0.3558, 15.87),
    (1000, 0.3523, 19.85),
    (1200, 0.3606, 23.81),
    (1400, 0.3557, 27.78),
    (1600, 0.3531, 31.75),
    (1800, 0.3505, 35.72),
    (2000, 0.3483, 39.68),
    (2200, 0.3523, 43.64),
    (2400, 0.3518, 47.61),
    (2600, 0.3497, 51.57),
    (2800, 0.3538, 55.53),
    (3000, 0.3509, 59.49),
    (3200, 0.3454, 63.47),
    (3400, 0.3548, 67.44),
    (3600, 0.3432, 71.41),
    (3800, 0.3447, 75.38),
    (4000, 0.3450, 79.33),
    (4200, 0.3472, 83.30),
    (4400, 0.3507, 87.27),
    (4548, 0.3421, 90.27),
)

# The determinism gate. Three points spread across the run: if the training side
# is bit-identical these are exact, so equality is checked to the four decimals
# the feed carries rather than with a tolerance.
DETERMINISM_POINTS: tuple[tuple[int, float], ...] = ((200, 0.3527), (1200, 0.3606), (2400, 0.3518))

# The caliber for the two headline metrics, fixed by the handoff. Stated in the
# output every time because three different baselines exist for this task
# (0.3674 at --max-len 256, 0.3663 at the official default, 0.3571 with the
# model's own prompt) and a number reported against the wrong one is meaningless.
OFFICIAL_CALIBER = "--prompt kalm --max-len 256"
OFFICIAL_BASELINE = 0.3674
OTHER_BASELINES = "0.3663 (--prompt kalm, official default --max-len 512), 0.3571 (model's own prompt)"

# Event kinds that change the world the training job runs in. An intervention is
# read off the trail, never inferred from the curve.
INTERVENTION_KINDS = ("kill", "submit", "config_change", "restart")


class DeterminismFailure(RuntimeError):
    """The reused A-arm curve is not this run's counterfactual."""


@dataclass
class Curve:
    """In-flight eval points, as the training job wrote them."""

    points: list[tuple[int, float, float]]  # (step, eval_ndcg, elapsed_s)

    def at(self, step: int) -> float | None:
        for s, value, _ in self.points:
            if s == step:
                return value
        return None

    def step_at_or_before(self, elapsed_s: float) -> tuple[int, float] | None:
        best = None
        for s, value, el in self.points:
            if el <= elapsed_s:
                best = (s, value)
        return best


def read_curve(path: Path) -> Curve:
    """Eval points out of a progress feed. Also accepts a result.json, whose
    ``eval_points`` is the same series in the job's own words."""
    text = path.read_text(encoding="utf-8")
    points: list[tuple[int, float, float]] = []
    if path.name == "result.json" or text.lstrip().startswith('{\n  "status"'):
        payload = json.loads(text)
        for entry in payload.get("eval_points") or []:
            points.append((int(entry[0]), float(entry[1]), 0.0))
        return Curve(points)
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if "step" in row and "eval_ndcg" in row:
            points.append((int(row["step"]), float(row["eval_ndcg"]), float(row.get("elapsed_s", 0.0))))
    return Curve(points)


def read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def check_determinism(curve: Curve) -> list[str]:
    """Empty when every gate point matches. Non-empty means the A curve is void."""
    problems = []
    for step, expected in DETERMINISM_POINTS:
        actual = curve.at(step)
        if actual is None:
            problems.append(f"step {step}: expected {expected}, the feed has no eval point at that step")
        elif abs(actual - expected) > 1e-9:
            problems.append(f"step {step}: expected {expected}, got {actual}")
    return problems


def interventions(events: list[dict[str, Any]], curve: Curve, arm_a: Curve) -> list[dict[str, Any]]:
    """Each world-changing event, where it landed, and the A-arm value there.

    The difference is printed; whether it is an improvement is not said, because
    two in-flight points from one seed do not settle that and the reader has the
    whole curve.
    """
    rows = []
    for event in events:
        kind = event.get("kind")
        if kind not in INTERVENTION_KINDS:
            continue
        row: dict[str, Any] = {
            "ts": event.get("ts"),
            "kind": kind,
            "detail": {k: v for k, v in event.items() if k not in ("ts", "kind")},
        }
        # The trail timestamps wall-clock, the curve counts elapsed seconds, so a
        # step can only be attached when the event carries one. Left unattached
        # rather than guessed from timestamps.
        step = event.get("step")
        if isinstance(step, int):
            row["step"] = step
            row["in_flight"] = curve.at(step)
            row["arm_a_at_step"] = arm_a.at(step)
            if row["in_flight"] is not None and row["arm_a_at_step"] is not None:
                row["delta_vs_arm_a"] = round(row["in_flight"] - row["arm_a_at_step"], 6)
        else:
            row["step"] = None
            row["note"] = "no step on the event; not attached to a curve position"
        rows.append(row)
    return rows


def contract_readings(events: list[dict[str, Any]]) -> dict[str, Any]:
    """ops_ask_owner and ops_finish as the trail recorded them.

    Every count is absolute. ``unestimated`` is the number of asks whose cost the
    loop could not put a number on, and it is printed beside the totals rather
    than folded into a compliance rate.
    """
    asks = [e for e in events if e.get("kind") == "ask_owner"]
    deliveries = [e for e in events if e.get("kind") == "ask_owner_delivery"]
    refused_asks = [e for e in events if e.get("kind") == "ask_owner_refused"]
    losses = [e.get("expected_loss_minutes") for e in asks]
    numeric = [v for v in losses if isinstance(v, (int, float)) and v >= 0]
    unestimated = sum(1 for v in losses if not isinstance(v, (int, float)) or v < 0)

    report_refused = [e for e in events if e.get("kind") == "report_refused"]
    reasons: dict[str, int] = {}
    missing: dict[str, int] = {}
    for e in report_refused:
        reasons[str(e.get("reason"))] = reasons.get(str(e.get("reason")), 0) + 1
        for field in e.get("missing") or []:
            missing[str(field)] = missing.get(str(field), 0) + 1

    return {
        "asks_attempted": len(asks),
        "asks_delivered": sum(1 for e in deliveries if e.get("sent")),
        "asks_delivery_failed": sum(1 for e in deliveries if not e.get("sent")),
        "asks_refused_by_contract": len(refused_asks),
        "expected_loss_minutes": sorted(numeric),
        "expected_loss_unestimated": unestimated,
        "expected_loss_denominator": len(asks),
        "reports_accepted": sum(1 for e in events if e.get("kind") == "report_accepted"),
        "reports_refused": len(report_refused),
        "report_refusal_reasons": reasons,
        "report_missing_fields": missing,
    }


def unguarded_paths(spans_path: Path | None, since: str | None, session_prefix: str | None) -> dict[str, Any]:
    """Whether MessageTool / ask_user ran. Those reach a person without passing
    the interruption contract, so the trail cannot show them and the audit spans
    are the only place they appear after the fact.

    Broken down by session, never totalled: this log holds every session on the
    machine, so an unqualified count mixes a chat window and a REPL in with the
    on-call wakes. On the box this was written on, 25 such spans in one window
    were 12 from a CLI session, 9 from a TUI session, 3 with no session at all,
    and 1 from a cron wake -- a bare "25" would have been a fabricated finding.

    ``session_prefix`` restricts the count (the on-call wakes run as
    ``cron:<job_id>``). Which job id belongs to which campaign is not derivable
    from a span, so mapping it is the reader's step, not this script's.
    """
    if spans_path is None:
        return {"checked": False, "reason": "no spans file given"}
    if not spans_path.exists():
        return {"checked": False, "reason": f"{spans_path} does not exist"}

    per_session: dict[str, dict[str, int]] = {}
    unparsable = 0
    considered = 0
    no_session = 0
    for line in spans_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            span = json.loads(line)
        except ValueError:
            unparsable += 1
            continue
        if since and (span.get("startTime") or "") < since:
            continue
        considered += 1
        attrs = span.get("attributes") or {}
        tool = str(attrs.get("tool.name") or attrs.get("gen_ai.tool.name") or "")
        if (span.get("name") or "") != "tool.call" or tool not in ("message", "ask_user"):
            continue
        session = attrs.get("session.key")
        if session is None:
            no_session += 1
            session = "(no session.key on the span)"
        session = str(session)
        if session_prefix and not session.startswith(session_prefix):
            continue
        per_session.setdefault(session, {})
        per_session[session][tool] = per_session[session].get(tool, 0) + 1

    return {
        "checked": True,
        "spans_considered": considered,
        "spans_unparsable": unparsable,
        "session_prefix": session_prefix,
        "per_session": per_session,
        "spans_with_no_session_key": no_session,
        "note": (
            "a tool name outside {message, ask_user} is not counted; a cron:<job_id> "
            "session is an on-call wake only if that job id is this campaign's"
        ),
    }


def headline_metrics(curve: Curve, official: dict[str, Any] | None) -> dict[str, Any]:
    """The two pre-registered metrics.

    Both come from the official evaluator, not from the in-flight feed: the
    in-flight numbers are a different caliber. Without an official file this
    prints what the in-flight feed says and labels it as the wrong caliber rather
    than substituting it.
    """
    out: dict[str, Any] = {
        "caliber": OFFICIAL_CALIBER,
        "baseline": OFFICIAL_BASELINE,
        "other_baselines_do_not_mix": OTHER_BASELINES,
    }
    if official:
        scored = {str(k): float(v) for k, v in (official.get("checkpoints") or {}).items()}
        out["official_scored"] = scored
        if scored:
            best_name = max(scored, key=lambda k: scored[k])
            out["best_surviving_checkpoint"] = {"name": best_name, "ndcg": scored[best_name]}
            last = official.get("last_checkpoint")
            if last in scored:
                out["last_checkpoint"] = {"name": last, "ndcg": scored[last]}
            else:
                out["last_checkpoint"] = {"name": last, "ndcg": None, "note": "not present in the scored set"}
    else:
        out["official_scored"] = None
        out["note"] = (
            "no official eval file given; the in-flight feed below is a DIFFERENT caliber "
            "(max_len from the training config) and is not comparable to the baseline"
        )
        out["in_flight_last"] = curve.points[-1][1] if curve.points else None
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--events", type=Path, required=True, help="campaign events.jsonl")
    ap.add_argument("--progress", type=Path, required=True, help="job progress.jsonl (or result.json)")
    ap.add_argument("--spans", type=Path, default=None, help="audit-spans.log, for the unguarded paths")
    ap.add_argument("--since", default=None, help="ISO timestamp; only spans at or after it are counted")
    ap.add_argument(
        "--session-prefix", default=None, help="restrict the span scan to sessions with this prefix, e.g. cron:"
    )
    ap.add_argument(
        "--official",
        type=Path,
        default=None,
        help='JSON: {"checkpoints": {"step-1200": 0.36}, "last_checkpoint": "step-4554"}',
    )
    ap.add_argument(
        "--self-test", action="store_true", help="expect the three-point check to pass and no interventions"
    )
    ap.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = ap.parse_args(argv)

    curve = read_curve(args.progress)
    events = read_events(args.events)
    arm_a = Curve([(s, v, 0.0) for s, v, _ in ARM_A_CURVE])

    problems = check_determinism(curve)
    report = {
        "determinism": {
            "points": [{"step": s, "expected": e, "actual": curve.at(s)} for s, e in DETERMINISM_POINTS],
            "passed": not problems,
            "problems": problems,
        },
        "curve_points": len(curve.points),
        "interventions": interventions(events, curve, arm_a),
        "contract": contract_readings(events),
        "unguarded_paths": unguarded_paths(args.spans, args.since, args.session_prefix),
        "headline": headline_metrics(curve, json.loads(args.official.read_text()) if args.official else None),
    }

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        _print_human(report)

    if problems:
        print(
            "\n*** DETERMINISM CHECK FAILED ***\n"
            "The reused round-1 A arm is not this run's counterfactual. Every A-vs-B\n"
            "difference above is void. Mismatches:\n" + "\n".join(f"  - {p}" for p in problems),
            file=sys.stderr,
        )
        return 2
    if args.self_test and report["interventions"]:
        print(
            f"\n*** SELF-TEST FAILED: expected no interventions, found {len(report['interventions'])} ***",
            file=sys.stderr,
        )
        return 3
    return 0


def _print_human(report: dict[str, Any]) -> None:
    det = report["determinism"]
    print("== determinism gate (three points vs the sealed A arm) ==")
    for row in det["points"]:
        mark = "ok" if row["actual"] is not None and abs(row["actual"] - row["expected"]) <= 1e-9 else "MISMATCH"
        print(f"  step {row['step']:>5}: expected {row['expected']}  actual {row['actual']}  [{mark}]")
    print(f"  passed: {det['passed']}   (in-flight eval points read: {report['curve_points']})")

    print("\n== interventions (from the trail, not inferred from the curve) ==")
    if not report["interventions"]:
        print("  none recorded")
    for row in report["interventions"]:
        print(f"  {row['ts']}  {row['kind']}  step={row['step']}")
        if row.get("in_flight") is not None:
            print(
                f"      in-flight {row['in_flight']}   arm A at same step {row['arm_a_at_step']}"
                f"   difference {row.get('delta_vs_arm_a')}"
            )
        if row.get("note"):
            print(f"      {row['note']}")
        if row["detail"]:
            print(f"      {json.dumps(row['detail'], ensure_ascii=False)[:200]}")

    c = report["contract"]
    print("\n== interruption contract ==")
    print(
        f"  asks attempted {c['asks_attempted']} | delivered {c['asks_delivered']}"
        f" | delivery failed {c['asks_delivery_failed']} | refused by contract {c['asks_refused_by_contract']}"
    )
    print(f"  expected_loss_minutes: {c['expected_loss_minutes']}")
    print(
        f"    estimated {len(c['expected_loss_minutes'])} / {c['expected_loss_denominator']} asks;"
        f" unestimated {c['expected_loss_unestimated']}"
    )
    print("\n== ops_finish ==")
    print(f"  accepted {c['reports_accepted']} | refused {c['reports_refused']}")
    print(f"  refusal reasons: {c['report_refusal_reasons'] or '(none)'}")
    print(f"  missing fields: {c['report_missing_fields'] or '(none)'}")

    u = report["unguarded_paths"]
    print("\n== unguarded paths to a person (MessageTool / ask_user) ==")
    if not u["checked"]:
        print(f"  not checked: {u['reason']}")
    else:
        print(f"  session filter: {u['session_prefix'] or '(none -- every session on this machine)'}")
        if not u["per_session"]:
            print("  no message / ask_user tool.call spans matched")
        for session, tools in sorted(u["per_session"].items()):
            print(f"    {session}: {tools}")
        print(
            f"  spans considered {u['spans_considered']}, unparsable {u['spans_unparsable']},"
            f" with no session.key {u['spans_with_no_session_key']}"
        )
        print(f"  {u['note']}")

    h = report["headline"]
    print("\n== headline metrics ==")
    print(f"  caliber {h['caliber']}, baseline {h['baseline']}")
    print(f"  do not mix with: {h['other_baselines_do_not_mix']}")
    if h.get("official_scored"):
        print(f"  best surviving checkpoint: {h.get('best_surviving_checkpoint')}")
        print(f"  last checkpoint:           {h.get('last_checkpoint')}")
    else:
        print(f"  {h.get('note')}")
        print(f"  in-flight last value (WRONG CALIBER, not comparable): {h.get('in_flight_last')}")


if __name__ == "__main__":
    raise SystemExit(main())
