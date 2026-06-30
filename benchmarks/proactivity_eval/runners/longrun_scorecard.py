#!/usr/bin/env python3
"""Score a longrun trajectory against its outcome rubric.

Usage:
  # Score one persona×agent trajectory
  uv run python runners/longrun_scorecard.py \
      --persona dev-01 --agent raven

  # Score all personas × agents found in output/longrun/ and produce
  # cross-agent comparison.md per persona:
  uv run python runners/longrun_scorecard.py --all --compare

  # Aggregate every existing *-scorecard.json into the README-style
  # cross-persona × cross-agent capability table:
  uv run python runners/longrun_scorecard.py --aggregate

Input:
  proactivity-eval/output/longrun/longrun-<persona>-<agent>-trajectory.jsonl

Output (next to trajectory):
  longrun-<persona>-<agent>-scorecard.json
  comparison-<persona>.md (when --compare + multiple agents present)
  aggregate-scorecard.md (when --aggregate)

Scoring: mostly deterministic regex + time/count detectors. Falls back
to LLM judge for semantic match when regex misses + for quality rubric
on each agent-initiated nudge. Memory accuracy uses one LLM call per
trajectory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

import yaml

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR))

from _common.env_loader import load_dotenvs  # noqa: E402
from _common.hermes_home import load_config_from_hermes_home, load_env_from_hermes_home  # noqa: E402
from _common.provider import make_provider  # noqa: E402

_DATA_DIR = _THIS_DIR.parent / "data" / "longrun"
_OUTPUT_DIR = _THIS_DIR.parent / "output" / "longrun"


# ─────────────────────────────────────────────────────────────────────────────
# Trajectory + rubric loaders


def _load_trajectory(path: Path) -> list[dict[str, Any]]:
    events = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def _iso(e: dict) -> datetime:
    return datetime.fromisoformat(e["fake_now"])


def _agent_initiated_events(events: list[dict]) -> list[dict]:
    """Events where agent took unprompted action. sentinel_tick with
    delivered=True is the canonical signal for Raven. For Hermes we'd
    look at cron_fire events (future adapter)."""
    out = []
    for e in events:
        if e.get("kind") == "sentinel_tick" and e.get("delivered"):
            out.append(e)
        # Future: Hermes cron_fire, OpenClaw ... (none today)
    return out


def _user_send_events(events: list[dict]) -> list[dict]:
    return [
        e
        for e in events
        if e.get("kind") in ("user_send", "sim_action") and (e.get("content") or e.get("kind") == "user_send")
    ]


def _load_outcomes(persona_id: str) -> dict[str, Any]:
    path = _DATA_DIR / f"persona-{persona_id}-outcomes.yaml"
    if not path.exists():
        raise FileNotFoundError(f"outcomes fixture missing: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ─────────────────────────────────────────────────────────────────────────────
# Type A: proactive-only outcomes


def _detect_type_a(
    outcome: dict,
    agent_events: list[dict],
    user_sends: list[dict],
) -> dict:
    """Detect agent-initiated coverage of a proactive outcome."""
    window = outcome.get("window") or []
    if len(window) == 2:
        start = _parse_date_bound(window[0])
        end = _parse_date_bound(window[1], end_of_day=True)
    else:
        start, end = None, None

    regex_str = outcome.get("topic_match_regex", "")
    try:
        pattern = re.compile(regex_str) if regex_str else None
    except re.error:
        pattern = None

    novelty_hours = int(outcome.get("novelty_window_hours") or 48)

    # Find in-window agent-initiated events matching topic
    candidates = []
    for e in agent_events:
        t = _iso(e)
        if start and t < start:
            continue
        if end and t > end:
            continue
        content = e.get("content") or e.get("nudge_message") or ""
        if pattern and pattern.search(content):
            candidates.append((t, e))

    # Check novelty for each candidate
    for t, ev in candidates:
        cutoff = t - timedelta(hours=novelty_hours)
        # User-initiated mentions in the prior window
        prior = [u for u in user_sends if cutoff <= _iso(u) < t and pattern and pattern.search(u.get("content") or "")]
        if not prior:
            return {
                "id": outcome["id"],
                "pass": True,
                "evidence": {
                    "fake_now": ev.get("fake_now"),
                    "content_preview": (ev.get("content") or ev.get("nudge_message") or "")[:200],
                },
                "points_earned": outcome.get("points", 0),
                "points_max": outcome.get("points", 0),
            }

    return {
        "id": outcome["id"],
        "pass": False,
        "reason": "no_agent_initiated_match" if not candidates else "all_matches_not_novel",
        "points_earned": 0,
        "points_max": outcome.get("points", 0),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Type B: reactive-achievable


def _detect_type_b(
    outcome: dict,
    events: list[dict],
) -> dict:
    """Detect user asked → agent replied correctly."""
    trigger_re = outcome.get("trigger_regex_in_user_send", "")
    must_mention_re = outcome.get("reply_must_mention", "")
    try:
        t_pat = re.compile(trigger_re) if trigger_re else None
        r_pat = re.compile(must_mention_re) if must_mention_re else None
    except re.error:
        t_pat = r_pat = None

    # Scan for user_send that triggers, followed by agent_reply that matches
    for i, ev in enumerate(events):
        if ev.get("kind") not in ("user_send", "sim_action"):
            continue
        content = ev.get("content") or ""
        if not (t_pat and t_pat.search(content)):
            continue
        # Find next agent_reply
        for j in range(i + 1, min(i + 5, len(events))):
            if events[j].get("kind") == "agent_reply":
                reply = events[j].get("content") or ""
                if r_pat and r_pat.search(reply):
                    return {
                        "id": outcome["id"],
                        "pass": True,
                        "evidence": {
                            "user_at": ev.get("fake_now"),
                            "agent_reply_preview": reply[:200],
                        },
                        "points_earned": outcome.get("points", 0),
                        "points_max": outcome.get("points", 0),
                    }
                break
    return {
        "id": outcome["id"],
        "pass": False,
        "reason": "user_triggered_but_reply_missed" if t_pat else "no_pattern",
        "points_earned": 0,
        "points_max": outcome.get("points", 0),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Type C: restraint (anti-outcomes)


def _detect_type_c(outcome: dict, agent_events: list[dict]) -> dict:
    """Detect respect of restraint constraints."""
    constraint = outcome.get("constraint", "").strip()
    window_daily = outcome.get("window_daily") or []
    points = outcome.get("points", 0)

    if constraint.startswith("nudge_count_in_window == 0") and len(window_daily) == 2:
        start_t = _parse_hhmm(window_daily[0])
        end_t = _parse_hhmm(window_daily[1])
        in_window = [e for e in agent_events if _in_daily_window(_iso(e).time(), start_t, end_t)]
        passed = len(in_window) == 0
        return {
            "id": outcome["id"],
            "pass": passed,
            "violations": len(in_window),
            "points_earned": points if passed else 0,
            "points_max": points,
        }

    if "max_nudges_in_any_1h" in constraint or "max(nudges_in_any_1h_window)" in constraint:
        limit = int(re.search(r"<=\s*(\d+)", constraint).group(1)) if re.search(r"<=\s*(\d+)", constraint) else 3
        max_in_hour = _max_count_in_any_window(agent_events, timedelta(hours=1))
        passed = max_in_hour <= limit
        return {
            "id": outcome["id"],
            "pass": passed,
            "max_observed": max_in_hour,
            "limit": limit,
            "points_earned": points if passed else 0,
            "points_max": points,
        }

    if "weekend" in constraint.lower() or "周末" in constraint:
        ratio_limit_m = re.search(r"<=\s*([0-9.]+)", constraint)
        limit = float(ratio_limit_m.group(1)) if ratio_limit_m else 0.3
        weekend = [e for e in agent_events if _iso(e).weekday() >= 5]
        weekday = [e for e in agent_events if _iso(e).weekday() < 5]
        # Count per-day averages
        weekend_days = max(1, len(set(_iso(e).date() for e in weekend)) or 0)
        weekday_days = max(1, len(set(_iso(e).date() for e in weekday)) or 0)
        we_avg = len(weekend) / weekend_days if weekend else 0.0
        wk_avg = len(weekday) / weekday_days if weekday else 1.0
        ratio = (we_avg / wk_avg) if wk_avg > 0 else 0.0
        passed = ratio <= limit
        return {
            "id": outcome["id"],
            "pass": passed,
            "weekend_avg": round(we_avg, 2),
            "weekday_avg": round(wk_avg, 2),
            "ratio": round(ratio, 3),
            "limit": limit,
            "points_earned": points if passed else 0,
            "points_max": points,
        }

    # Fallback: unknown constraint, mark as unscored
    return {
        "id": outcome["id"],
        "pass": None,
        "reason": f"unsupported_constraint:{constraint[:100]}",
        "points_earned": 0,
        "points_max": points,
    }


# ─────────────────────────────────────────────────────────────────────────────
# LLM-based: memory accuracy


async def _score_memory_accuracy(
    ws_memory: str,
    persona: dict,
    intents: list[dict],
    provider,
    model: str,
) -> dict:
    """Single LLM call: given final MEMORY.md + ground-truth facts from
    persona + intents.reveals_new_fact, score coverage + hallucinations."""
    ground_truth = []
    # (a) Facts from initial_memory_md
    init_mem = persona.get("initial_memory_md") or ""
    ground_truth.append(f"## Initial persona facts:\n{init_mem.strip()}")
    # (b) Facts revealed during the month
    revealed = [i for i in intents if i.get("reveals_new_fact")]
    if revealed:
        lines = []
        for i in revealed:
            lines.append(f"- {i.get('at', '?')}: {i['reveals_new_fact']}")
        ground_truth.append("## Facts revealed during sim:\n" + "\n".join(lines))
    gt = "\n\n".join(ground_truth)

    prompt = f"""你是 benchmark memory accuracy 评估员。

## 真相（Ground Truth）
这个 persona 应该在 agent 的 memory 里保留的事实：

{gt}

## Agent 30 天后 MEMORY.md

```markdown
{ws_memory[:8000]}
```

## 你的任务

输出一个结构化评估：
1. `covered_facts_count`: 上面真相里被 memory 覆盖的 fact 数（估算整数）
2. `total_facts_count`: 真相 facts 总数（估算整数）
3. `hallucinations`: memory 里有哪些内容是真相里没有的、看起来像胡编的（list of short strings）
4. `coverage_score`: 0-1 之间浮点，= covered / total
5. `rationale`: 2-3 句话概括 agent 的 memory 质量

只输出 JSON："""
    try:
        resp = await provider.chat_with_retry(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            max_tokens=800,
            temperature=0.2,
        )
        raw = (resp.content or "").strip()
        # Strip ```json fences
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except Exception as exc:
        return {
            "error": f"{type(exc).__name__}: {exc}",
            "coverage_score": 0.0,
            "covered_facts_count": 0,
            "total_facts_count": 0,
            "hallucinations": [],
            "rationale": "memory accuracy scoring failed",
        }
    return {"error": "no_json_in_output", "coverage_score": 0.0}


# ─────────────────────────────────────────────────────────────────────────────
# Scoring orchestrator


async def score_trajectory(
    persona_id: str,
    agent: str,
    *,
    provider=None,
    judge_model="qwen3.5-27B",
    skip_memory_accuracy: bool = False,
) -> dict:
    traj_path = _OUTPUT_DIR / f"longrun-{persona_id}-{agent}-trajectory.jsonl"
    if not traj_path.exists():
        raise FileNotFoundError(f"trajectory not found: {traj_path}")
    events = _load_trajectory(traj_path)
    outcomes = _load_outcomes(persona_id)
    persona = yaml.safe_load((_DATA_DIR / f"persona-{persona_id}.yaml").read_text(encoding="utf-8"))
    intents_path = _DATA_DIR / f"persona-{persona_id}-intents.yaml"
    intents = (
        (yaml.safe_load(intents_path.read_text(encoding="utf-8")) or {}).get("events", [])
        if intents_path.exists()
        else []
    )

    agent_events = _agent_initiated_events(events)
    user_sends = _user_send_events(events)

    type_a_results = [_detect_type_a(o, agent_events, user_sends) for o in outcomes.get("type_a_proactive_only") or []]
    type_b_results = [_detect_type_b(o, events) for o in outcomes.get("type_b_reactive_achievable") or []]
    type_c_results = [_detect_type_c(o, agent_events) for o in outcomes.get("type_c_restraint") or []]

    def _sum(rs, key="points_earned"):
        return sum(r.get(key, 0) or 0 for r in rs)

    totals = {
        "type_a": {
            "earned": _sum(type_a_results),
            "max": _sum(type_a_results, "points_max"),
            "count_pass": sum(1 for r in type_a_results if r.get("pass")),
            "count": len(type_a_results),
        },
        "type_b": {
            "earned": _sum(type_b_results),
            "max": _sum(type_b_results, "points_max"),
            "count_pass": sum(1 for r in type_b_results if r.get("pass")),
            "count": len(type_b_results),
        },
        "type_c": {
            "earned": _sum(type_c_results),
            "max": _sum(type_c_results, "points_max"),
            "count_pass": sum(1 for r in type_c_results if r.get("pass")),
            "count": len(type_c_results),
        },
    }
    # Cross-axis sum intentionally omitted: Type A (rewards firing) and
    # Type C (rewards NOT firing) cancel under naïve summation. Headline
    # metric is ``proactive_lift`` (Type A earned); read per-axis numbers
    # for the rest.

    # Memory accuracy (needs LLM)
    mem_acc: dict[str, Any] = {"skipped": skip_memory_accuracy}
    if not skip_memory_accuracy:
        if provider is None:
            load_env_from_hermes_home()
            hcfg = load_config_from_hermes_home()
            provider, judge_model = make_provider(hcfg)
        # Memory.md lives inside the workspace which is already cleaned up;
        # but we can reconstruct what ended up persisted from trajectory-adjacent artifacts.
        # For now, rely on trajectory last memory_tail events or skip.
        # MVP: we check state.json captured in ckpt; final_memory is harder post-cleanup.
        # Fallback: skip with note if not found.
        final_mem = _try_find_final_memory(persona_id, agent)
        if final_mem:
            mem_acc = await _score_memory_accuracy(
                final_mem,
                persona,
                intents,
                provider,
                judge_model,
            )
        else:
            mem_acc = {"skipped": True, "reason": "no final MEMORY.md found; need to enable dump"}

    scorecard = {
        "persona_id": persona_id,
        "agent": agent,
        "trajectory": str(traj_path),
        "event_count": len(events),
        "agent_initiated_count": len(agent_events),
        "user_send_count": len(user_sends),
        "totals": totals,
        "proactive_lift": totals["type_a"]["earned"],  # the magic number
        "type_a": type_a_results,
        "type_b": type_b_results,
        "type_c": type_c_results,
        "memory_accuracy": mem_acc,
        "scored_at": datetime.now().isoformat(),
    }
    out_path = _OUTPUT_DIR / f"longrun-{persona_id}-{agent}-scorecard.json"
    out_path.write_text(json.dumps(scorecard, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"[done] {out_path} — "
        f"A {totals['type_a']['count_pass']}/{totals['type_a']['count']} · "
        f"B {totals['type_b']['count_pass']}/{totals['type_b']['count']} · "
        f"C {totals['type_c']['count_pass']}/{totals['type_c']['count']} · "
        f"lift={totals['type_a']['earned']}",
        file=sys.stderr,
    )
    return scorecard


def _try_find_final_memory(persona_id: str, agent: str) -> str | None:
    """Extract final MEMORY.md from the latest checkpoint tar if present."""
    import tarfile

    ckpt_dir = _OUTPUT_DIR / f"ckpt-{persona_id}-{agent}"
    if not ckpt_dir.exists():
        return None
    tars = sorted(ckpt_dir.glob("day*.tar"))
    if not tars:
        return None
    latest = tars[-1]
    try:
        with tarfile.open(latest, "r") as tar:
            for name in ("workspace/memory/MEMORY.md", "memory/MEMORY.md"):
                try:
                    f = tar.extractfile(name)
                    if f:
                        return f.read().decode("utf-8")
                except KeyError:
                    continue
    except Exception:
        return None
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Cross-agent comparison markdown report


def render_comparison(persona_id: str, scorecards: dict[str, dict]) -> str:
    """Render cross-agent comparison markdown."""
    header = f"# {persona_id} × 3-way proactivity comparison\n\n"
    header += f"_generated {datetime.now().isoformat()}_\n\n"

    # Summary table
    lines = [
        "## Summary",
        "",
        "| | " + " | ".join(scorecards.keys()) + " |",
        "|---|" + "|".join(["---"] * len(scorecards)) + "|",
    ]
    for label, key in (
        ("Type A (proactive-only)", "type_a"),
        ("Type B (reactive-achievable)", "type_b"),
        ("Type C (restraint)", "type_c"),
    ):
        row = [label]
        for agent, sc in scorecards.items():
            t = sc["totals"][key]
            row.append(f"{t['earned']}/{t['max']} ({t['count_pass']}/{t['count']})")
        lines.append("| " + " | ".join(row) + " |")
    # Proactive lift — single headline metric. Cross-axis total omitted on
    # purpose: Type A and Type C reward opposing behaviors, summing cancels.
    row = ["🚀 Proactive lift (Type A net)"]
    for agent, sc in scorecards.items():
        row.append(str(sc.get("proactive_lift", sc["totals"]["type_a"]["earned"])))
    lines.append("| " + " | ".join(row) + " |")

    # Per-outcome breakdown for Type A (the differentiator)
    lines.append("\n## Type A — Proactive-only outcomes (the proactivity differentiator)\n")
    first_sc = next(iter(scorecards.values()))
    outcome_ids = [r["id"] for r in first_sc["type_a"]]
    for oid in outcome_ids:
        lines.append(f"\n### `{oid}`")
        for agent, sc in scorecards.items():
            matches = [r for r in sc["type_a"] if r["id"] == oid]
            if not matches:
                continue
            r = matches[0]
            status = "✓" if r.get("pass") else "✗"
            pts = f"{r['points_earned']}/{r['points_max']}"
            evidence = ""
            if r.get("pass") and r.get("evidence"):
                ev = r["evidence"]
                evidence = f" — {ev.get('fake_now', '?')} `{ev.get('content_preview', '')[:80]}`"
            else:
                evidence = f" — {r.get('reason', '')}"
            lines.append(f"- **{agent}**: {status} {pts}{evidence}")

    # Memory accuracy
    lines.append("\n## Memory accuracy\n")
    lines.append("| agent | coverage_score | covered | total | hallucinations |")
    lines.append("|---|---|---|---|---|")
    for agent, sc in scorecards.items():
        ma = sc.get("memory_accuracy") or {}
        if ma.get("skipped"):
            lines.append(f"| {agent} | (skipped) | - | - | - |")
            continue
        cs = ma.get("coverage_score", 0.0)
        covered = ma.get("covered_facts_count", "?")
        total = ma.get("total_facts_count", "?")
        hall = len(ma.get("hallucinations") or [])
        lines.append(f"| {agent} | {cs:.2f} | {covered} | {total} | {hall} |")

    return header + "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# Cross-persona × cross-agent aggregate (README-style capability table)


_AGENT_ORDER = ("raven", "hermes", "openclaw")


def _count_delivered_fires(traj_path: Path) -> dict[str, int]:
    """Count delivered fires in a trajectory, split by surface.

    - ``sentinel`` = ``sentinel_tick`` events with ``delivered=true``
      (Raven's L3 Sentinel path)
    - ``cron`` = ``cron_fire`` events (Hermes native cron OR OpenClaw
      MCP-gateway cron — both adapters now emit this unified kind).
      Also accepts the legacy ``hermes_cron_fire`` kind for back-compat
      with trajectories captured before the unification.
    - ``total`` = ``cron`` only. Scheduled execution measures delivered
      *scheduled* reminders (user said "remind me at X" -> registered ->
      fired), which is the cron surface. Sentinel anticipatory fires are
      tracked separately and belong to the Anticipatory dimension, not
      here; counting them would double-count L3 activity across two rows.
      (Hermes/OpenClaw have no sentinel fires, so this only affects
      Raven's number.)
    Returns zeros if the trajectory is missing.
    """
    out = {"sentinel": 0, "cron": 0, "total": 0}
    if not traj_path.exists():
        return out
    with traj_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = e.get("kind")
            if kind == "sentinel_tick" and e.get("delivered"):
                out["sentinel"] += 1
            elif kind in ("cron_fire", "hermes_cron_fire"):
                out["cron"] += 1
    out["total"] = out["cron"]
    return out


def _discover_scorecards() -> dict[str, list[dict]]:
    """Group existing *-scorecard.json files by agent."""
    by_agent: dict[str, list[dict]] = defaultdict(list)
    for path in sorted(_OUTPUT_DIR.glob("longrun-*-*-scorecard.json")):
        try:
            sc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[warn] skipping unreadable scorecard {path.name}: {exc}", file=sys.stderr)
            continue
        agent = sc.get("agent")
        if agent:
            by_agent[agent].append(sc)
    return by_agent


def _pct(num: int, den: int) -> str:
    if den == 0:
        return "—"
    return f"{(100 * num / den):.0f}%"


def render_aggregate(by_agent: dict[str, list[dict]]) -> str:
    """README-style cross-persona × cross-agent capability table.

    Aggregates Type A (anticipatory), Type B (reactive Q&A), Type C
    (restraint), and a separately-computed Scheduled-execution fire
    count derived directly from each trajectory.
    """
    agents = [a for a in _AGENT_ORDER if a in by_agent] + [a for a in sorted(by_agent) if a not in _AGENT_ORDER]
    if not agents:
        return "(no scorecards found; run `--all` first)\n"

    # Collect every (persona, agent) pair we have a scorecard for, so we
    # can also re-scan trajectories for delivered-fire counts.
    pair_personas: dict[str, set[str]] = {a: set() for a in agents}
    for agent, scs in by_agent.items():
        for sc in scs:
            pair_personas.setdefault(agent, set()).add(sc["persona_id"])

    # Sum Type A / B / C across personas, per agent.
    agg: dict[str, dict[str, dict[str, int]]] = {a: {} for a in agents}
    for agent in agents:
        for key in ("type_a", "type_b", "type_c"):
            agg[agent][key] = {"earned": 0, "max": 0, "count_pass": 0, "count": 0}
        for sc in by_agent[agent]:
            for key in ("type_a", "type_b", "type_c"):
                t = sc.get("totals", {}).get(key) or {}
                for field in ("earned", "max", "count_pass", "count"):
                    agg[agent][key][field] += int(t.get(field) or 0)

    # Re-scan trajectories for delivered-fire counts (sentinel vs cron).
    fires: dict[str, dict[str, int]] = {}
    for agent in agents:
        totals = {"sentinel": 0, "cron": 0, "total": 0}
        for persona in sorted(pair_personas[agent]):
            traj = _OUTPUT_DIR / f"longrun-{persona}-{agent}-trajectory.jsonl"
            sub = _count_delivered_fires(traj)
            for k, v in sub.items():
                totals[k] += v
        fires[agent] = totals

    # Persona footprint summary (top of doc).
    all_personas = sorted({p for ps in pair_personas.values() for p in ps})

    lines: list[str] = []
    lines.append("# longrun aggregate scorecard")
    lines.append("")
    lines.append(f"_generated {datetime.now().isoformat()}_")
    lines.append("")
    lines.append(f"**Personas scored:** {len(all_personas)} ({', '.join(all_personas) or '—'})")
    lines.append(f"**Agents:** {', '.join(agents)}")
    lines.append("")
    lines.append("## Capability table")
    lines.append("")
    header = ["能力维度"] + agents + ["含义"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")

    # Row 1: Anticipatory proactivity (rubric Type A).
    # Labels intentionally drop the A/B/C/D letters: aggregate's old "B"
    # = Scheduled execution clashed with rubric Type B = Reactive Q&A.
    # Semantic names + parenthetical rubric pointer remove the collision.
    # Count-only — per-outcome ``points`` weighting (formerly surfaced as
    # ``lift=N``) was noisy and not intuitive; case_pass/count is the
    # primary number callers ask for.
    row_a = ["**Anticipatory**<br>(rubric Type A 命中)"]
    for agent in agents:
        t = agg[agent]["type_a"]
        row_a.append(f"**{t['count_pass']}/{t['count']}** ({_pct(t['count_pass'], t['count'])})")
    row_a.append("agent 没被告知就想到该做 — 只有 L3 Sentinel 能做")
    lines.append("| " + " | ".join(row_a) + " |")

    # Row 2: Scheduled execution — trajectory-derived, NOT from rubric.
    # Counts ONLY cron fires (delivered scheduled reminders). Sentinel
    # anticipatory fires belong to the Anticipatory row, so they are shown
    # here as an aside (not added to the headline) to keep Raven's L3
    # activity visible without inflating scheduled execution.
    row_b = ["**Scheduled execution**<br>(delivered cron fires, trajectory-derived)"]
    for agent in agents:
        f = fires[agent]
        aside = f"<br>(+{f['sentinel']} sentinel anticipatory)" if f["sentinel"] else ""
        row_b.append(f"**{f['cron']} fires**{aside}")
    row_b.append('user 显式说 "X 时提醒" 后 agent 真的注册并 fire')
    lines.append("| " + " | ".join(row_b) + " |")

    # Row 3: Reactive Q&A (rubric Type B).
    row_c = ["**Reactive Q&A**<br>(rubric Type B 命中)"]
    for agent in agents:
        t = agg[agent]["type_b"]
        row_c.append(f"{t['count_pass']}/{t['count']} ({_pct(t['count_pass'], t['count'])})")
    row_c.append("user 问问题时 agent 答对率")
    lines.append("| " + " | ".join(row_c) + " |")

    # Row 4: Restraint (rubric Type C) — count-only. Per-outcome `points`
    # weighting doesn't really tier the cases (most outcomes carry 2-3
    # pts and the weights aren't tuned to severity), so reporting points
    # was noisy.
    row_d = ["**Restraint**<br>(rubric Type C 命中)"]
    for agent in agents:
        t = agg[agent]["type_c"]
        row_d.append(f"{t['count_pass']}/{t['count']} ({_pct(t['count_pass'], t['count'])})")
    row_d.append("DND / 频率 / 周末 constraint 是否被破坏")
    lines.append("| " + " | ".join(row_d) + " |")

    # Per-persona breakdown — three axes side-by-side so readers see the
    # A↔C trade-off persona by persona rather than a single number.
    # No cross-axis sum: Type A (firing) and Type C (not firing) cancel
    # under naïve summation; readers should rank by the four-axis row.
    lines.append("")
    lines.append("## Per-persona breakdown (A · B · C count_pass)")
    lines.append("")
    lines.append("| persona | " + " | ".join(agents) + " |")
    lines.append("|---|" + "|".join(["---"] * len(agents)) + "|")
    for persona in all_personas:
        row = [persona]
        for agent in agents:
            match = next((sc for sc in by_agent[agent] if sc.get("persona_id") == persona), None)
            if not match:
                row.append("—")
                continue
            tot = match.get("totals") or {}
            ta = tot.get("type_a") or {}
            tb = tot.get("type_b") or {}
            tc = tot.get("type_c") or {}
            row.append(
                f"A {ta.get('count_pass', 0)}/{ta.get('count', 0)} · "
                f"B {tb.get('count_pass', 0)}/{tb.get('count', 0)} · "
                f"C {tc.get('count_pass', 0)}/{tc.get('count', 0)}"
            )
        lines.append("| " + " | ".join(row) + " |")

    return "\n".join(lines) + "\n"


def _parse_date_bound(s: Any, *, end_of_day: bool = False) -> datetime:
    if isinstance(s, datetime):
        return s
    s = str(s)
    if "T" in s:
        return datetime.fromisoformat(s)
    d = datetime.fromisoformat(s)
    if end_of_day:
        return d.replace(hour=23, minute=59, second=59)
    return d


def _parse_hhmm(s: str) -> time:
    hh, mm = s.split(":")
    return time(int(hh), int(mm))


def _in_daily_window(t: time, start: time, end: time) -> bool:
    # Exclusive end: a fire exactly at the window end (e.g. 09:00 for a
    # 01:00-09:00 window) is OUTSIDE it, matching DndWindow.matches and the
    # conventional [start, end) reading. Avoids false-positive C violations
    # at the boundary minute.
    if start <= end:
        return start <= t < end
    # Wraps midnight
    return t >= start or t < end


def _max_count_in_any_window(events: list[dict], window: timedelta) -> int:
    if not events:
        return 0
    times = sorted(_iso(e) for e in events)
    best = 0
    for i, t in enumerate(times):
        end = t + window
        j = i
        while j < len(times) and times[j] <= end:
            j += 1
        best = max(best, j - i)
    return best


# ─────────────────────────────────────────────────────────────────────────────
# CLI


def main() -> None:
    global _OUTPUT_DIR
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--output-dir",
        default=None,
        help="Directory holding longrun-*-trajectory.jsonl + "
        "*-scorecard.json (default: output/longrun/). Point it "
        "at a snapshot dir (e.g. output/post-fix10-d30/) to "
        "score / aggregate that run without moving files.",
    )
    ap.add_argument("--persona", help="Score one persona only")
    ap.add_argument("--agent", default="raven", help="Score a specific agent system (raven/hermes/openclaw)")
    ap.add_argument("--all", action="store_true", help="Scan output/longrun/ for all trajectory files and score each")
    ap.add_argument("--compare", action="store_true", help="With --all: produce cross-agent comparison-<persona>.md")
    ap.add_argument(
        "--aggregate",
        action="store_true",
        help="Aggregate every existing *-scorecard.json into a "
        "README-style cross-persona × cross-agent capability "
        "table. Re-runs scoring first when combined with --all.",
    )
    ap.add_argument(
        "--aggregate-out",
        default=None,
        help="With --aggregate: path for the aggregate markdown (default: output/longrun/aggregate-scorecard.md)",
    )
    ap.add_argument("--skip-memory", action="store_true", help="Skip LLM-backed memory accuracy scoring")
    args = ap.parse_args()

    if args.output_dir:
        _OUTPUT_DIR = Path(args.output_dir).expanduser().resolve()
        if not _OUTPUT_DIR.is_dir():
            ap.error(f"--output-dir not a directory: {_OUTPUT_DIR}")

    load_dotenvs()

    async def run() -> None:
        if args.all:
            # Discover all (persona, agent) pairs
            pairs = set()
            for p in _OUTPUT_DIR.glob("longrun-*-*-trajectory.jsonl"):
                stem = p.stem  # longrun-<persona>-<agent>-trajectory
                parts = stem.split("-")
                # persona id can contain a hyphen (dev-01), agent is the penultimate
                # actually: longrun-<persona_parts...>-<agent>-trajectory
                # persona always ends in "-01" per our naming; agent is raven/hermes/openclaw
                for ag in ("raven", "hermes", "openclaw"):
                    if f"-{ag}-trajectory" in p.name:
                        persona_id = p.name[len("longrun-") : -len(f"-{ag}-trajectory.jsonl")]
                        pairs.add((persona_id, ag))
                        break
            by_persona: dict[str, dict[str, dict]] = defaultdict(dict)
            for persona, ag in sorted(pairs):
                print(f"[score] {persona} × {ag}", file=sys.stderr)
                sc = await score_trajectory(persona, ag, skip_memory_accuracy=args.skip_memory)
                by_persona[persona][ag] = sc
            if args.compare:
                for persona, scs in by_persona.items():
                    md = render_comparison(persona, scs)
                    out = _OUTPUT_DIR / f"comparison-{persona}.md"
                    out.write_text(md, encoding="utf-8")
                    print(f"[done] {out}", file=sys.stderr)
        elif args.persona:
            await score_trajectory(args.persona, args.agent, skip_memory_accuracy=args.skip_memory)
        elif args.aggregate:
            pass  # handled after run()
        else:
            ap.error("specify --persona, --all, or --aggregate")

        if args.aggregate:
            by_agent = _discover_scorecards()
            md = render_aggregate(by_agent)
            out_path = Path(args.aggregate_out) if args.aggregate_out else _OUTPUT_DIR / "aggregate-scorecard.md"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(md, encoding="utf-8")
            print(f"[done] {out_path}", file=sys.stderr)

    asyncio.run(run())


if __name__ == "__main__":
    main()
