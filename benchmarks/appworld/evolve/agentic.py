"""AppWorld agentic analysis front-end (analysis_mode="agentic").

Replaces the map-reduce diagnosis (one shallow LLM read per failing
trajectory) with ONE read-only Claude Code session that investigates the run
like an engineer — pre-aggregated ledger first, then deep-reads of
representative transcripts per failure signature, then the harness source —
and emits a taxonomy-constrained diagnosis. The output converts into the same
``failure_map`` shape as :func:`classify_failures`, so WHY selection, briefs,
history, and gates downstream are untouched (SOP funnel unchanged; only HOW
the analyst reads changed).

The session workspace is assembled per round under the orchestrator work dir:

    ledger_digest.md   pre-aggregated outcomes (spares the session Bash-less
                       aggregation of 270 result files)
    runs/              symlink -> the parent baseline out-dir (raw result JSONs)
    sessions/, att/    symlinks -> the agent transcript trees
    harness/           git worktree PINNED AT THE PARENT COMMIT (the exact code
                       a fix would patch — the live repo may have drifted)
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Callable, Optional

from benchmarks.appworld.evolve.diagnose import (
    APPWORLD_BENCH_INTRO,
    APPWORLD_DIAGNOSIS_RULES,
)
from raven.evolver.orchestrator.nodes.taxonomy import (
    TaxonomySpec,
    add_failure_mode,
    coerce_mode,
    empty_failure_map,
    strip_code_fence,
)
from raven.evolver.orchestrator.providers.claude_agentic import run_agentic_session
from raven.evolver.tree import git_ops
from raven.evolver.tree.node import HarnessNode


def _task_states(runs_root: Path, exp: str, k: int) -> dict[str, dict]:
    """Per-task attempt outcomes + first oracle signature from the ledger."""
    out: dict[str, dict] = {}
    for p in sorted((runs_root / exp).glob("*_k*.json")):
        r = json.loads(p.read_text())
        tid = r.get("task_id")
        if not tid:
            continue
        st = out.setdefault(tid, {"marks": [], "sig": ""})
        if r.get("infra_error"):
            st["marks"].append("I")
            continue
        st["marks"].append("P" if r.get("success") else "F")
        if not r.get("success") and not st["sig"]:
            fails = (r.get("evaluation") or {}).get("failures") or []
            if fails:
                req = str(fails[0].get("requirement", ""))[:80]
                # The trace tail carries the class-defining detail the
                # requirement lacks (e.g. '<<not_given>>' = nothing submitted)
                trace = str(fails[0].get("trace", "")).strip().splitlines()
                tail = trace[-1][:80] if trace else ""
                st["sig"] = f"{req} | {tail}" if tail else req
    return out


def build_analysis_workspace(
    work_dir: Path,
    runs_root: Path,
    ws_root: Path,
    exp: str,
    k: int,
) -> tuple[Path, list[str]]:
    """Assemble the read-only workspace; returns ``(ws, failing_task_ids)``."""
    ws = work_dir / "agentic_analysis" / exp
    ws.mkdir(parents=True, exist_ok=True)
    states = _task_states(runs_root, exp, k)
    failing = sorted(t for t, s in states.items() if "F" in s["marks"])
    sig_hist = Counter(s["sig"] for t, s in states.items() if t in set(failing) and s["sig"])

    lines = [
        "# Ledger digest (pre-aggregated — start here)",
        "",
        f"experiment: {exp} | tasks: {len(states)} | failing (need labels): {len(failing)}",
        "",
        "## Failure signature histogram (oracle first-failed requirement)",
    ]
    lines += [f"- {c:3d}x  {s}" for s, c in sig_hist.most_common()]
    lines += ["", "## Per-task outcomes (P=pass F=fail I=infra, one mark per attempt)"]
    for tid in sorted(states):
        s = states[tid]
        sig = f'  sig="{s["sig"]}"' if s["sig"] else ""
        lines.append(f"- {tid}: {''.join(s['marks'])}{sig}")
    lines += [
        "",
        "## Where to look deeper",
        "- raw results: runs/<task>_k<n>.json (evaluation.failures = oracle)",
        f"- transcripts: sessions/appworld/<task>_{exp}_k<n>.jsonl (Raven layout) or att/<task>_{exp}_k<n>/sessions/",
        "- harness source (the code a fix would patch): harness/benchmarks/appworld/, harness/raven/agent/",
    ]
    (ws / "ledger_digest.md").write_text("\n".join(lines))

    for name, target in (
        ("runs", runs_root / exp),
        ("sessions", ws_root / "sessions"),
        ("att", ws_root / "att"),
    ):
        link = ws / name
        if link.is_symlink() or link.exists():
            continue
        if Path(target).exists():
            link.symlink_to(Path(target).resolve())
    return ws, failing


def _salvage_truncated(blob: str):
    """Parse a truncation-damaged JSON blob by cutting back to the last
    complete object and balance-closing the open brackets. Long agentic
    sessions can hit the final-message length cap mid-array; the assignments
    generated before the cut are still good data."""
    ends = [i for i, ch in enumerate(blob) if ch == "}"]
    for end in reversed(ends[-60:]):
        cand = blob[: end + 1]
        stack = []
        in_str = esc = False
        for ch in cand:
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
            elif ch == '"':
                in_str = not in_str
            elif not in_str and ch in "[{":
                stack.append("]" if ch == "[" else "}")
            elif not in_str and ch in "]}":
                if stack and stack[-1] == ch:
                    stack.pop()
        if in_str:
            continue
        try:
            return json.loads(cand + "".join(reversed(stack)))
        except json.JSONDecodeError:
            continue
    return None


def _agentic_system(taxonomy: TaxonomySpec) -> str:
    why = "\n".join(f"  - {k}: {v}" for k, v in taxonomy.why_classes.items())
    where = "\n".join(f"  - {k}: {v}" for k, v in taxonomy.where_classes.items())
    return (
        f"{APPWORLD_BENCH_INTRO} You are the ANALYST for one evolution round: "
        "investigate WHY the parent harness fails, agentically, then emit one "
        "structured diagnosis.\n\n"
        "Method (funnel — cheap first, deep second):\n"
        "1) Read ledger_digest.md; group failing tasks by oracle signature.\n"
        "2) Deep-read at least ONE representative transcript per signature group "
        "(cross-reference turns: e.g. compare an argument the agent used against "
        "the data it had retrieved — transcription slips, missed pages, wrong "
        "tokens hide there).\n"
        "3) Read the harness source under harness/ BEFORE naming a WHERE.\n"
        "Coverage duty: EVERY failing task gets >=1 label; state in 'coverage' "
        "which tasks you deep-read and which you labeled by signature analogy.\n\n"
        f"WHY classes:\n{why}\n\nWHERE classes:\n{where}\n\n"
        f"Rules: {APPWORLD_DIAGNOSIS_RULES} Mark EXACTLY ONE mode per trajectory "
        '"dominant": true — the failure that directly explains the verdict.\n\n'
        "Final message: ONLY this JSON object, no prose around it. GROUP tasks "
        "sharing the same mode into one entry via trajectory_ids (keeps the "
        "output compact — it must not truncate); keep reasoning/fix_hint terse:\n"
        '{"assignments":[{"trajectory_ids":["id1","id2"],"why":"<WHY key>",'
        '"where":"<WHERE key>","dominant":true|false,"reasoning":"<=12 words",'
        '"fix_hint":"<=12 words"}],"coverage":"<deep-read vs analogy>"}'
    )


def make_agentic_diagnose_fn(
    *,
    repo_root: str | Path,
    runs_root: str | Path,
    ws_root: str | Path,
    exp_of: Callable[[HarnessNode], str],
    work_dir: str | Path,
    taxonomy: TaxonomySpec,
    k: int = 3,
    model: str = "claude-opus-4-8",
    claude_bin: str = "claude",
    timeout: float = 1800.0,
    run: Optional[Callable] = None,
) -> Callable[[int, HarnessNode], dict]:
    """Build the loop's ``diagnose_fn`` backed by one agentic session per round."""
    repo_root = Path(repo_root)
    runs_root = Path(runs_root)
    ws_root = Path(ws_root)
    work_dir = Path(work_dir)

    def diagnose_fn(round_index: int, parent: HarnessNode) -> dict:
        exp = exp_of(parent)
        ws, failing = build_analysis_workspace(work_dir, runs_root, ws_root, exp, k)
        if not failing:
            return empty_failure_map()

        harness = ws / "harness"
        made_worktree = False
        if not harness.exists() and parent.git_commit_sha not in ("", "unknown"):
            try:
                git_ops.create_worktree(repo_root, harness, parent.git_commit_sha)
                made_worktree = True
            except Exception:  # noqa: BLE001 — analysis degrades, doesn't die
                pass
        try:
            user = (
                f"Round {round_index}, parent {parent.node_id}. "
                f"{len(failing)} failing tasks need labels: {failing}\n"
                "Start with ledger_digest.md. Produce the diagnosis JSON."
            )
            raw = run_agentic_session(
                user,
                system_prompt=_agentic_system(taxonomy),
                cwd=ws,
                model=model,
                claude_bin=claude_bin,
                timeout=timeout,
                run=run,
                add_dirs=(runs_root, ws_root),
            )
        finally:
            if made_worktree:
                try:
                    git_ops.remove_worktree(repo_root, harness, force=True)
                except Exception:  # noqa: BLE001
                    pass

        (ws / "last_response.txt").write_text(raw)
        s = strip_code_fence(raw)
        i, j = s.find("{"), s.rfind("}")
        if i < 0 or j <= i:
            raise RuntimeError(f"agentic diagnosis returned no JSON: {raw[:300]}")
        blob = s[i : j + 1]
        try:
            obj = json.loads(blob)
        except json.JSONDecodeError:
            # Two observed damage modes: a Python-style dict (single quotes),
            # and a final message cut mid-array by the output length cap —
            # salvage what was generated rather than discarding the session.
            import ast

            try:
                obj = ast.literal_eval(blob)
            except (ValueError, SyntaxError):
                obj = _salvage_truncated(blob)
            if obj is None:
                raise RuntimeError(f"agentic diagnosis unparseable (saved to last_response.txt): {blob[:200]}")
        fm = empty_failure_map()
        failing_set = set(failing)
        seen: set[str] = set()
        for a in obj.get("assignments", []):
            if not isinstance(a, dict):
                continue
            tids = a.get("trajectory_ids") or ([a["trajectory_id"]] if a.get("trajectory_id") else [])
            for tid in tids:
                if tid not in failing_set:
                    continue
                seen.add(tid)
                add_failure_mode(fm, tid, coerce_mode(a, taxonomy))
        fm["_n_judged"] = len(seen)
        fm["_coverage"] = str(obj.get("coverage", ""))[:500]
        missed = failing_set - seen
        if missed:
            print(f"[agentic] {len(missed)} failing tasks unlabeled: {sorted(missed)[:5]}", flush=True)
        return fm

    return diagnose_fn


__all__ = ["make_agentic_diagnose_fn", "build_analysis_workspace"]
