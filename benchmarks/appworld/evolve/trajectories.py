"""AppWorld trajectory rendering + the diagnosis trajectory source.

``render_trajectory`` turns a real session.jsonl + result.json into transcript
text for the judge (task prompt, each assistant turn's narrative + Python, tool
stdout, final response, and the benchmark's own verdict/oracle) — no operator
interpretation added. ``build_out_dir_trajectory_source`` wraps it into the
loop's trajectory source: for a parent's baseline out-dir, pick each failing
task's first non-infra attempt and render it (infra attempts are skipped — SOP
§0: infra failure != agent failure, don't diagnose it).

The infra rerun ladder (SOP §0) re-scores infra-contaminated tasks into sibling
``<exp>_infra_rerun{i}`` out-dirs, and the measurement with the fewest infra
trials wins (see ``scoring.eval_with_infra_rerun``). Diagnosis mirrors that
choice here: for each task the dir that provided the kept measurement is the
one whose failing attempt gets rendered — a task salvaged by a rerun is
diagnosed from its rerun trajectory, not invisible because the base dir only
holds its infra trials.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

from benchmarks.appworld.evolve.adapter import read_out_dir
from raven.evolver.orchestrator.scoring import TaskEval
from raven.evolver.tree.node import HarnessNode


def _tool_code(assistant_msg: dict) -> str:
    out = []
    for tc in assistant_msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                pass
        code = args.get("code") if isinstance(args, dict) else args
        out.append(str(code))
    return "\n".join(out)


def _instruction_of(first_user_msg: str) -> str:
    """The real task instruction from AppWorld's first user message.

    That message is the full APPWORLD_PROMPT: ~1.3k chars of static boilerplate
    (API rules, formatting) with the actual instruction appended after the
    ``"Your task:"`` marker. A prefix cap here used to keep the boilerplate and
    cut the instruction (review round-2 P1-12); slicing at the bench's own
    marker is the fix — trimming is bench knowledge, so it lives here, and the
    generic layers apply no cap at all.
    """
    marker = "Your task:"
    i = first_user_msg.find(marker)
    return first_user_msg[i + len(marker) :].strip() if i >= 0 else first_user_msg


def _clip(s: str, cap: int) -> str:
    """Head+tail clip: keep both ends of an over-long block.

    A head-only cap amputates exactly where the evidence usually is — a long
    stdout ends with the traceback's last frame / the assertion message — so
    over-cap text keeps ~60% head + ~40% tail around a snip marker.
    """
    if len(s) <= cap:
        return s
    head = int(cap * 0.6)
    tail = cap - head
    return s[:head] + " …[snip]… " + s[-tail:]


def render_trajectory(session_path: str | Path, result: dict, *, cap: int = 600) -> tuple[str, str]:
    """Return ``(task_description, transcript_text)`` from a real session + result."""
    lines = [ln for ln in Path(session_path).read_text().splitlines() if ln.strip()]
    msgs = [json.loads(ln) for ln in lines if json.loads(ln).get("_type") != "metadata"]
    task_desc = ""
    turns = []
    for m in msgs:
        role = m.get("role")
        content = m.get("content")
        if role == "user" and not task_desc:
            task_desc = _instruction_of(content or "")
            continue
        if role == "assistant":
            narr = (m.get("content") or "").strip()
            code = _tool_code(m).strip()
            block = ""
            if narr:
                block += f"[assistant] {_clip(narr, cap)}\n"
            if code:
                block += f"[ran python]\n{_clip(code, cap)}\n"
            if block:
                turns.append(block)
        elif role == "tool":
            turns.append(f"[stdout] {_clip(content or '', cap)}")
    ev = result.get("evaluation") or {}
    passes = ev.get("passes") or []
    failures = ev.get("failures") or []
    verdict = (
        f"[benchmark verdict] success={result.get('success')} "
        f"tests_passed={len(passes)}/{result.get('num_tests')} "
        f"final_response={_clip(result.get('response') or '', cap)!r}"
    )
    oracle = ""
    if failures:
        oracle = "\n[benchmark oracle] failed checks: " + _clip(json.dumps(failures), 2000)
    return task_desc, "\n".join(turns) + "\n" + verdict + oracle


def default_sess_path(runs_root: Path, ws_root: Path, tid: str, exp: str, k: int) -> Path | None:
    """Locate a session.jsonl for one attempt (per-attempt isolated workspace,
    with the shared-sessions dir as a legacy fallback). Returns None if absent."""
    p = ws_root / f"att/{tid}_{exp}_k{k}/sessions/{tid}_{exp}_k{k}.jsonl"
    if p.exists():
        return p
    legacy = ws_root / "sessions" / f"{tid}_{exp}_k{k}.jsonl"
    if legacy.exists():
        return legacy
    # Raven SessionManager layout: conversation key "appworld:<tid>_<exp>_k<k>"
    # persists to sessions/<channel>/<chat_id>.jsonl.
    raven = ws_root / "sessions" / "appworld" / f"{tid}_{exp}_k{k}.jsonl"
    return raven if raven.exists() else None


def _ladder_exps(exp: str) -> list[str]:
    """The base experiment plus its SOP §0 infra-rerun ladder dirs, in the
    order ``eval_with_infra_rerun`` produced them."""
    return [exp, f"{exp}_infra_rerun1", f"{exp}_infra_rerun2"]


def _ladder_evals(runs_root: Path, exp: str) -> list[tuple[str, dict[str, TaskEval]]]:
    """``(experiment, evals)`` for every existing dir in the rerun ladder."""
    out = []
    for name in _ladder_exps(exp):
        d = runs_root / name
        if d.exists() and list(d.glob("*_k*.json")):
            out.append((name, read_out_dir(d)))
    return out


def _kept_measurement(
    ladder: list[tuple[str, dict[str, TaskEval]]],
) -> dict[str, tuple[str, TaskEval]]:
    """Per task, the (experiment, eval) whose measurement was kept — fewest
    infra trials wins, earlier dir wins ties (mirrors ``eval_with_infra_rerun``)."""
    chosen: dict[str, tuple[str, TaskEval]] = {}
    for name, evals in ladder:
        for tid, ev in evals.items():
            cur = chosen.get(tid)
            if cur is None or ev.infra_attempts < cur[1].infra_attempts:
                chosen[tid] = (name, ev)
    return chosen


def _failing_attempt(runs_root: Path, ws_root: Path, exp: str, tid: str, k: int) -> Optional[tuple[Path, dict]]:
    """First non-infra FAILING attempt of ``tid`` in ``exp``'s out-dir, as
    ``(session_path, result)``; None when the task has none there."""
    bdir = runs_root / exp
    for kk in range(k):
        rp = bdir / f"{tid}_k{kk}.json"
        sp = default_sess_path(runs_root, ws_root, tid, exp, kk)
        if sp is None or not rp.exists():
            continue
        r = json.load(open(rp))
        if r.get("infra_error"):  # infra != agent failure — don't diagnose
            continue
        if r.get("success"):  # want a FAILING attempt (borderline tasks have both)
            continue
        return sp, r
    return None


def _attempts_line(runs_root: Path, exp: str, tid: str, k: int) -> str:
    """One-line per-attempt outcome summary prepended to the diagnosed
    transcript. Cross-attempt patterns are diagnostic signal the single
    rendered attempt can't carry: pass/fail flips are the noise-class
    signature, same-check repeat failures indicate a stable pathology."""
    parts = []
    for kk in range(k):
        rp = runs_root / exp / f"{tid}_k{kk}.json"
        if not rp.exists():
            continue
        r = json.load(open(rp))
        if r.get("infra_error"):
            parts.append(f"k{kk}=INFRA")
        elif r.get("success"):
            parts.append(f"k{kk}=PASS")
        else:
            failures = (r.get("evaluation") or {}).get("failures") or []
            req = str((failures[0] or {}).get("requirement", ""))[:60] if failures else ""
            parts.append(f'k{kk}=FAIL("{req}")' if req else f"k{kk}=FAIL")
    return f"ATTEMPTS (k={k}): " + ", ".join(parts) if parts else ""


def _passing_attempt(runs_root: Path, ws_root: Path, exp: str, tid: str, k: int) -> Optional[tuple[Path, dict]]:
    """First non-infra PASSING attempt of ``tid`` in ``exp``'s out-dir — the
    contrast material for the design step's over-trigger self-check."""
    bdir = runs_root / exp
    for kk in range(k):
        rp = bdir / f"{tid}_k{kk}.json"
        sp = default_sess_path(runs_root, ws_root, tid, exp, kk)
        if sp is None or not rp.exists():
            continue
        r = json.load(open(rp))
        if r.get("infra_error") or not r.get("success"):
            continue
        return sp, r
    return None


def build_out_dir_trajectory_source(
    *,
    runs_root: str | Path,
    ws_root: str | Path,
    exp_of: Callable[[HarnessNode], str],
    k: int = 3,
    cap: int = 600,
) -> Callable[[int, HarnessNode], list]:
    """Build the loop's trajectory source over a parent's baseline out-dir.

    ``exp_of(parent)`` returns the experiment name whose out-dir + sessions hold
    that parent's baseline run (e.g. ``"van0"`` for the root). Diagnoses every
    task the baseline does NOT fully pass — both all-fail (passes==0) and
    **borderline** (0 < passes < attempts, the most fixable) — by rendering its
    first non-infra **failing** attempt (SOP §2 step 2: read every failing task, borderline and all-fail),
    read from whichever infra-rerun ladder dir provided the kept measurement.
    """
    runs_root = Path(runs_root)
    ws_root = Path(ws_root)

    def trajectory_source(round_index: int, parent: HarnessNode) -> list:
        exp = exp_of(parent)
        ladder = _ladder_evals(runs_root, exp)
        if not ladder:
            return []
        chosen = _kept_measurement(ladder)
        trajs = []
        n_failing = 0
        for tid in sorted(chosen):
            src_exp, ev = chosen[tid]
            if ev.passes >= ev.attempts:  # fully passing -> not a failure, skip
                continue
            n_failing += 1
            picked = _failing_attempt(runs_root, ws_root, src_exp, tid, k)
            if picked is None and src_exp != exp:
                picked = _failing_attempt(runs_root, ws_root, exp, tid, k)
            if picked is None:
                continue
            desc, transcript = render_trajectory(picked[0], picked[1], cap=cap)
            attempts = _attempts_line(runs_root, src_exp, tid, k)
            if attempts:
                transcript = f"{attempts}\n\n{transcript}"
            trajs.append((tid, desc, transcript))
        if n_failing and not trajs:
            # Results exist but not one failing trajectory could be rendered:
            # the session transcripts are gone (e.g. a results-only cold start
            # where runs/ was copied but ws/sessions/ was not). Diagnosis would
            # otherwise silently judge 0 failures and every round spins empty.
            raise RuntimeError(
                f"diagnosis found {n_failing} failing task(s) in '{exp}' but "
                f"could render 0 trajectories — the session transcripts under "
                f"{ws_root}/ are missing. Re-run cold start so trajectories are "
                "written (or restore them); results alone cannot be diagnosed."
            )
        return trajs

    return trajectory_source


def build_failed_attempt_renderer(
    *,
    runs_root: str | Path,
    ws_root: str | Path,
    exp_of: Callable[[HarnessNode], str],
    k: int = 3,
    cap: int = 600,
) -> Callable[[HarnessNode], Callable[[str], str]]:
    """Per-parent renderer of one failing attempt — the design step's
    ``read_trajectory`` backend. ``renderer_of(parent)`` binds the parent's
    out-dir (rerun-ladder aware); the returned callable renders ``task_id``'s
    first non-infra failing attempt as evidence text."""
    runs_root = Path(runs_root)
    ws_root = Path(ws_root)

    def renderer_of(parent: HarnessNode) -> Callable[[str], str]:
        exp = exp_of(parent)

        def render(task_id: str) -> str:
            for name in _ladder_exps(exp):
                picked = _failing_attempt(runs_root, ws_root, name, task_id, k)
                if picked is not None:
                    desc, transcript = render_trajectory(picked[0], picked[1], cap=cap)
                    return f"TASK: {desc}\n\n{transcript}"
            # No failing attempt -> a fully-passing task: render it as contrast
            # material, so the driver can check its trigger does NOT fire here.
            for name in _ladder_exps(exp):
                picked = _passing_attempt(runs_root, ws_root, name, task_id, k)
                if picked is not None:
                    desc, transcript = render_trajectory(picked[0], picked[1], cap=cap)
                    return (
                        f"PASSING TRAJECTORY (healthy run, for contrast — your fix "
                        f"must not change its behavior):\nTASK: {desc}\n\n{transcript}"
                    )
            return "(no non-infra attempt on record for this task)"

        return render

    return renderer_of


def render_candidate_failure(
    runs_root: str | Path,
    ws_root: str | Path,
    exp: str,
    tid: str,
    *,
    k: int = 3,
    tail: int = 450,
) -> Optional[str]:
    """Compact excerpt of HOW a candidate broke a task (harm replay).

    Reads the candidate's own out-dir (``exp`` = its confirm job) for ``tid``'s
    failing attempt and returns the trajectory tail + verdict. Fed back into
    the next round's PRIOR ATTEMPTS: designers predict fixes well but harms
    poorly, so 'narrow it sharply' needs the actual wound, not just a count."""
    picked = _failing_attempt(Path(runs_root), Path(ws_root), exp, tid, k)
    if picked is None:
        return None
    _, transcript = render_trajectory(picked[0], picked[1], cap=300)
    t = transcript.strip()
    return t[-tail:] if len(t) > tail else t


def build_passing_ids_source(
    *,
    runs_root: str | Path,
    ws_root: str | Path,
    exp_of: Callable[[HarnessNode], str],
    k: int = 3,
    limit: int = 6,
) -> Callable[[HarnessNode], list[str]]:
    """Per-parent list of fully-passing train tasks with a renderable session —
    the design step's contrast set for the over-trigger self-check."""
    runs_root = Path(runs_root)
    ws_root = Path(ws_root)

    def passing_ids_of(parent: HarnessNode) -> list[str]:
        exp = exp_of(parent)
        ladder = _ladder_evals(runs_root, exp)
        if not ladder:
            return []
        out: list[str] = []
        for tid, (src_exp, ev) in sorted(_kept_measurement(ladder).items()):
            if ev.passes < ev.attempts:
                continue
            if _passing_attempt(runs_root, ws_root, src_exp, tid, k) is not None:
                out.append(tid)
            if len(out) >= limit:
                break
        return out

    return passing_ids_of


__all__ = [
    "render_trajectory",
    "default_sess_path",
    "build_out_dir_trajectory_source",
    "build_failed_attempt_renderer",
    "build_passing_ids_source",
    "render_candidate_failure",
]
