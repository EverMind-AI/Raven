"""Bash-editor candidate design: WHY selection + an agentic file-edit loop.

The design step (SOP §2 ②) for AppWorld. Deterministic WHY selection off the
failure map (count x fixability x history-decay, so the budget flows to fixable
under-explored levers, not the ever-common W6/W7 capability ceilings), then a
per-candidate agentic loop where the driver runs ``bash`` in a :class:`Sandbox`
(a worktree at the parent commit) to edit the harness. Output is a
:class:`Candidate` (the edited file bytes vs the parent) that
``make_git_commit_apply_fn`` commits.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Callable, Optional

from benchmarks.appworld.evolve.eval import Candidate
from benchmarks.appworld.evolve.sandbox import Sandbox
from raven.evolver.orchestrator.config import Budget
from raven.evolver.tree.node import HarnessNode

# WHY fixability weight: the taxonomy itself flags W6/W7 as capability-ceiling /
# noise, yet they're usually the MOST common failures, so a raw-count WHY
# selection wastes every round on them. Down-weight so the budget flows to the
# fixable levers (W2/W3/W4/W5) once the obvious ones are explored.
WHY_FIX_WEIGHT = {
    "W6_action_state_mismatch": 0.25,
    "W7_borderline_flaky": 0.1,
    "W1_empty_response_stall": 0.7,
}

_BASH_SYSTEM = """You improve an agent harness to fix a diagnosed failure mode. You are in a git \
worktree of the repo (your cwd). The harness that runs the benchmark is \
benchmarks/appworld/agent_cli.py (it builds the agent prompt APPWORLD_PROMPT and \
runs one AppWorld task through a minimal AgentLoop). You MAY edit or \
create files ONLY under: benchmarks/appworld/ and raven/agent/ (prompts, \
tools, the agent loop, and hooks under raven/agent/hook/) — EXCEPT \
benchmarks/appworld/evolve/ (the evolution machinery that scores you: any edit \
there is rejected at commit time). Anything you change outside the allowed trees is \
reverted. Work ONLY inside your cwd: this worktree holds the exact code version \
you are patching. Never cd elsewhere or touch other checkouts of this repo — they are \
different versions and out of bounds.

Work in a loop, ONE JSON object per message, no prose, no code fences:
  inspect a real run:          {"action":"read_trajectory","task_id":"<id from the lists>"}
  run a shell command:         {"action":"bash","command":"<cmd>"}   (read files with cat/grep/sed; \
fine for small sed edits)
  write/replace a whole file:  {"action":"write_file","path":"<repo-relative path>","content":"<full \
new file content>"}   (PREFERRED for multi-line edits and new files — no shell quoting to get wrong)
  finish:                      {"action":"done","summary":"<what you changed and why>"}

You MUST make at least one concrete file edit before you say done — reading files only \
is not enough; the point is to CHANGE the harness. Rules: make a SMALL, targeted change \
that fixes the failure mode.

ACTIVATION (critical): your change MUST take effect with NO special environment variable \
set — the candidate's code is committed and run as-is, so any fix hidden behind \
`if os.environ.get(...)` is DEAD CODE that scores identical to vanilla. Do NOT gate your \
fix behind an env var / feature flag. If you add a hook under raven/agent/hook/, wire \
it in UNCONDITIONALLY (agent_cli.py builds the AgentLoop with hooks=None — construct \
your hook there and pass it in). A prompt-string change in APPWORLD_PROMPT is \
unconditional by nature. Verify edits compile before done; keep vanilla behavior intact \
except your fix.

GENERALIZATION (critical): the TRAIN failures shown are only to understand the general \
MECHANISM. The harness is scored on a HELD-OUT test set you never see, so a change that \
only helps these specific train tasks is worthless. Do NOT hardcode any task id, expected \
answer, or task-specific value, or special-case the inspected tasks. Make a PRINCIPLED, \
general change that would help the whole distribution."""


def _loose_json(raw: str) -> Optional[dict]:
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s[:4].lower() == "json":
            s = s[4:]
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j < 0:
        return None
    try:
        return json.loads(s[i : j + 1])
    except json.JSONDecodeError:
        return None


def why_all_tids(failure_map: dict, why: str) -> list[str]:
    """All train task_ids diagnosed with this WHY -> the focused eval subset."""
    tids: list[str] = []
    for key, cell in (failure_map.get("cells") or {}).items():
        if key.split("::", 1)[-1] != why:
            continue
        for c in cell.get("candidates", []):
            t = c.get("trajectory_id")
            if t and t not in tids:
                tids.append(t)
    return tids


def why_brief(failure_map: dict, why: str) -> tuple[str, list[str]]:
    """A concrete diagnosis brief for one WHY (WHERE-hints + judge reasoning +
    fix-hints + evidence task_ids) — feeds the driver the diagnosis, not a label."""
    cells = failure_map.get("cells", {})
    lines: list[str] = []
    wheres: set[str] = set()
    tids: list[str] = []
    for key, cell in cells.items():
        w, _, cw = key.partition("::")
        if cw != why:
            continue
        wheres.add(w)
        for c in cell.get("candidates", []):
            tid = c.get("trajectory_id")
            if tid:
                tids.append(tid)
            r = (c.get("reasoning") or "").strip()[:300]
            if r:
                lines.append(f"- [{tid}] {r}")
            for comp in (c.get("components") or [])[:1]:
                s = (comp.get("summary") or "").strip()[:160]
                if s:
                    lines.append(f"    judge fix-hint: {s}")
    brief = (
        f"DIAGNOSIS\nfailure mode (WHY): {why}\n"
        f"suggested levers (WHERE-hints): {sorted(wheres)}\n"
        f"judge's per-trajectory reasoning (sample):\n" + "\n".join(lines[:8])
    )
    return brief, [t for t in tids if t][:8]


def _attempt_counts(hist: list[dict]) -> tuple[int, int, int]:
    """(n_fail, n_inert, n_win) for one WHY's attempt history.

    Inert deaths (preflight: trigger never fired on any historical trajectory)
    are split out of ``n_fail``: they indict the trigger's reachability, not
    the WHY's fixability, so the decay must not punish the WHY as if the
    mechanism had been tried and measured.
    """
    n_inert = sum(1 for h in hist if h.get("outcome") == "pruned_inert")
    n_fail = sum(1 for h in hist if h.get("promoted") is False) - n_inert
    n_win = sum(1 for h in hist if h.get("promoted") is True)
    return n_fail, n_inert, n_win


def rerank_whys(
    failure_map: dict,
    k: int,
    history: dict[str, list[dict]],
    *,
    why_fix_weight: dict[str, float] = WHY_FIX_WEIGHT,
) -> list[str]:
    """Top-k WHYs by count x fixability x history-decay (not raw count).

    Decay WHYs whose prior attempts were all pruned (0.55^n_fail), reset on a win,
    so exhausted / capability-ceiling WHYs stop soaking the budget. Inert deaths
    decay gently (0.85^n_inert): the mechanism was never exercised, but repeated
    unreachable triggers still argue for spending the budget elsewhere.
    """
    why_dist = failure_map.get("why_distribution", {})
    if not why_dist:
        return []
    scored = []
    for why, count in why_dist.items():
        fw = why_fix_weight.get(why, 1.0)
        n_fail, n_inert, n_win = _attempt_counts(history.get(why, []))
        penalty = (0.55 ** n_fail) * (0.85 ** n_inert) if n_win == 0 else 1.0
        scored.append((why, count * fw * penalty))
    scored.sort(key=lambda x: (-x[1], x[0]))
    return [w for w, s in scored[:k] if s > 0]


def _strip_hints(brief: str) -> str:
    """Drop the judge fix-hint lines from a diagnosis brief.

    On attempt >=1 the hints are withheld: attempt 0 already followed them, and
    an anchor that strong makes "take a DISTINCT approach" a dead letter — the
    driver must re-derive the mechanism from the trajectories instead."""
    return "\n".join(l for l in brief.splitlines() if "judge fix-hint:" not in l)


_WHY_SELECT_SYS = (
    "You allocate one evolution round's design budget across the diagnosed "
    "failure modes of an agent harness. Each mode comes with its definition, "
    "weighted diagnosis count, evidence task count, the judge's WHERE-hint "
    "distribution (which harness surface a fix would target; WHERE=none means "
    "the judge saw NO harness lever for that trajectory — a capability "
    "ceiling), and prior attempt history. Pick the K modes MOST worth "
    "attacking NOW: prefer modes with concrete WHERE-hints where one "
    "principled mechanism could plausibly rescue many tasks; avoid modes "
    "dominated by WHERE=none, run-to-run noise, or exhausted by repeated "
    "failed attempts. A large count does NOT outrank fixability. Respond ONLY "
    'a JSON array of exactly K items: [{"why":"<key from the list>","reason":"<=1 line"}]'
)


def _why_where_hints(failure_map: dict, why: str) -> dict[str, int]:
    """Per-WHY WHERE-hint counts from the ``<WHERE>::<WHY>`` cells."""
    hints: dict[str, int] = {}
    for key, cell in (failure_map.get("cells") or {}).items():
        w, _, cw = key.partition("::")
        if cw == why:
            hints[w] = hints.get(w, 0) + len(cell.get("candidates") or [])
    return hints


def driver_select_whys(
    call_fn: Callable[[list], str],
    failure_map: dict,
    k: int,
    history: dict[str, list[dict]],
    *,
    why_defs: Optional[dict[str, str]] = None,
) -> tuple[list[str], dict[str, str]]:
    """Let the driver pick the round's target WHYs (selection is a PROPOSAL,
    the gates still judge every candidate). Returns ``([], {})`` on any parse
    or validity failure — the caller falls back to the formula ranking.

    ``why_defs`` (taxonomy key -> definition text) grounds each mode; the
    WHERE-hint distribution is computed from the map's cells so the driver
    sees the judge's per-instance fixability evidence (WHERE=none share),
    not just bucket sizes.
    """
    dist = failure_map.get("why_distribution", {})
    if not dist:
        return [], {}
    lines = []
    for why, cnt in sorted(dist.items(), key=lambda x: (-x[1], x[0])):
        _, tids = why_brief(failure_map, why)
        n_fail, n_inert, n_win = _attempt_counts(history.get(why, []))
        hints = _why_where_hints(failure_map, why)
        total = sum(hints.values())
        none_pct = round(100 * hints.get("none", 0) / total) if total else 0
        hint_str = ", ".join(
            f"{w}={c}" for w, c in sorted(hints.items(), key=lambda x: -x[1])
        ) or "(no WHERE data)"
        attempts = f"prior attempts: {n_fail} failed / {n_win} promoted"
        if n_inert:
            attempts += f" / {n_inert} never-fired"
        lines.append(
            f"- {why}: weighted_count={cnt:.1f}, evidence_tasks={len(tids)}, "
            f"where_hints: {hint_str} (judged unfixable: {none_pct}%), "
            f"{attempts}"
        )
        d = (why_defs or {}).get(why, "").strip()
        if d:
            lines.append(f"    def: {d[:200]}")
    user = (
        f"K={k}\nDiagnosed failure modes this round:\n" + "\n".join(lines)
        + "\n\nPick the K modes to attack. JSON array only."
    )
    try:
        raw = call_fn([
            {"role": "system", "content": _WHY_SELECT_SYS},
            {"role": "user", "content": user},
        ])
    except Exception:  # noqa: BLE001 — selection must never kill the round
        return [], {}
    s = raw.strip()
    if "```" in s:
        s = s.split("```")[1] if s.count("```") >= 2 else s
        s = s.split("\n", 1)[-1] if s.lstrip().startswith(("json", "JSON")) else s
    i, j = s.find("["), s.rfind("]")
    if i < 0 or j <= i:
        return [], {}
    try:
        arr = json.loads(s[i:j + 1])
    except json.JSONDecodeError:
        return [], {}
    whys, reasons = [], {}
    for item in arr:
        w = item.get("why") if isinstance(item, dict) else None
        if w in dist and w not in whys:
            whys.append(w)
            reasons[w] = str(item.get("reason", ""))[:200]
    return whys[:k], reasons


def _fmt_history(history: dict[str, list[dict]], why: str) -> str:
    hist = history.get(why, [])
    if not hist:
        return ""
    lines = [
        "PRIOR ATTEMPTS for this failure mode (LEARN — do NOT repeat a failed approach; if one "
        "helped its focused subset but REGRESSED full-train, that lever OVER-TRIGGERS on healthy "
        "tasks, so narrow it sharply or try a different lever; a pruned_inert attempt never "
        "fired at all — its TRIGGER was unreachable, so redesign the trigger/reachability, "
        "not the mechanism body):"
    ]
    for h in hist[-8:]:
        flip = ""
        if "rescued" in h:
            ids = ",".join(h.get("regressed_ids", []))
            flip = (
                f" (rescued {h['rescued']}, regressed {h['regressed']}"
                + (f": {ids}" if ids else "") + ")"
            )
        reason = f" ({h['reason']})" if h.get("reason") else ""
        lines.append(
            f"- {h['node_id']} [{','.join(h.get('files', []))}]: {h.get('summary', '')[:160]} "
            f"-> {h['outcome']}{flip}{reason}"
        )
        if h.get("harm"):
            lines.append(
                "    HOW it broke a healthy task (that candidate's own run — "
                "your fix must not reproduce this):\n    | "
                + h["harm"][:450].replace("\n", "\n    | ")
            )
    return "\n".join(lines)


_BEACON_REQUIREMENT = (
    "\nINSTRUMENTATION (required for python edits): the new code path MUST call\n"
    "    from raven.evolver.activation.ledger import activation_beacon\n"
    "    activation_beacon('<your tag>', '<site>')\n"
    "at the exact place your mechanism fires (INSIDE its trigger condition, NOT at "
    "import/module level — a beacon that fires on every task carries no attribution "
    "signal). It no-ops outside evaluation, so it never changes behavior. A python "
    "CODE edit without a beacon is REJECTED; edits that only change prompt/string "
    "constants (e.g. APPWORLD_PROMPT text) are exempt — do not add dead code just to "
    "carry a beacon. Optionally end your done-summary with a line "
    "'TRIGGER_REGEX: <python regex>' matching the trajectory text your mechanism "
    "reacts to (used to pre-skip candidates whose trigger never occurs).\n"
)

_TRIGGER_RE = re.compile(r"TRIGGER_REGEX:\s*(.+?)\s*$", re.M)


def _has_beacon(changed: dict[str, bytes]) -> bool:
    return any(
        p.endswith(".py") and b"activation_beacon(" in b for p, b in changed.items()
    )


def _code_changed(old: Optional[bytes], new: bytes) -> bool:
    """True when a .py edit changes executable structure, not just string
    constants. AppWorld's agent prompt lives INSIDE agent_cli.py, so a
    prompt-only candidate is a .py edit with an identical code shape — it has
    no execution point to carry a beacon and must not be held to one (its
    Gate-b attribution runs on trajectory presence, like .md/.yaml edits).
    A new file or unparseable content counts as a code change (fail-closed;
    the compile check screens unparseable content anyway)."""
    if old is None:
        return True

    def norm(src: bytes) -> str:
        tree = ast.parse(src.decode())
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                n.value = "_"
        return ast.dump(tree)

    try:
        return norm(old) != norm(new)
    except (SyntaxError, UnicodeDecodeError):
        return True


def _parse_trigger_spec(summary: str) -> Optional[dict]:
    """The driver's optional self-declared trigger predicate, or None.

    Malformed regexes are dropped (fail-open): a bad predicate must cost the
    candidate nothing — preflight simply has no signal to prune on.
    """
    m = _TRIGGER_RE.search(summary or "")
    if not m:
        return None
    pattern = m.group(1).strip()
    try:
        re.compile(pattern)
    except re.error:
        return None
    return {"kind": "trajectory_regex", "pattern": pattern}


def bash_edit_candidate(
    call_fn: Callable[[list], str],
    sandbox: Sandbox,
    diagnosis: str,
    ev_ids: list[str],
    tag: str,
    *,
    render_failed: Optional[Callable[[str], str]] = None,
    passing_ids: Optional[list[str]] = None,
    max_turns: int = 22,
    force_after_readonly: int = 4,
    attempt: int = 0,
    history_text: str = "",
    guard_text: str = "",
    import_smoke_module: Optional[str] = "benchmarks.appworld.agent_cli",
    import_smoke_python: Optional[str] = None,
    system_prompt: Optional[str] = None,
    require_beacon: bool = False,
) -> tuple[Optional[dict[str, bytes]], list[str], str]:
    """Run the agentic bash edit loop for one candidate in ``sandbox``.

    Returns ``(changed_files | None, deleted_paths, summary)``: changed_files
    is the whitelist bytes the driver changed vs the parent commit,
    deleted_paths the whitelist files it removed (both halves of the edit —
    a rename is one change + one deletion). ``None`` changed_files = no edit /
    compile failure / import-smoke failure -> skip this candidate.

    ``import_smoke_module`` is the SOP §2 ③ free prune: after the AST compile
    check, import the harness entry module inside the sandbox worktree (with
    ``import_smoke_python``, ideally the same interpreter the eval's batch uses).
    AST parsing passes bad imports / module-level NameErrors that would turn the
    candidate's whole eval into infra noise; the import smoke drops those
    candidates before any eval budget is spent. ``None`` disables it.

    ``system_prompt`` overrides the bash-editor system prompt (default = the
    AppWorld one); the editable-paths line in the user prompt always follows
    ``sandbox.whitelist``, so another bench only supplies its prompt + whitelist.
    """
    user = (
        f"{diagnosis}\n\n"
        + (history_text + "\n\n" if history_text else "")
        + (guard_text + "\n\n" if guard_text else "")
        + f"Real failed task_ids you can inspect (read_trajectory): {ev_ids[:20]}\n"
        + (
            f"PASSING task_ids for CONTRAST (read_trajectory works on these too): "
            f"{passing_ids[:4]} — healthy runs your fix must NOT change.\n"
            if passing_ids else ""
        )
        + f"Editable files: under {' and '.join(sandbox.whitelist)}.\n\n"
        "PROTOCOL — do NOT skip straight to editing:\n"
        "1) Read the DIAGNOSIS above.\n"
        "2) Inspect 1-2 evidence trajectories (read_trajectory) to CONFIRM the concrete "
        "mechanism — what exactly the agent does wrong and at which step.\n"
        "3) Read the relevant harness code (agent_cli.py / the loop / hooks).\n"
        "4) Then make ONE minimal edit targeting THAT mechanism, grounded in what you saw. "
        "Over-broad prompt nagging regresses passing tasks, so be surgical.\n"
        + (
            "5) BEFORE done: check your trigger condition against BOTH sides — it must fire "
            "on the failing evidence and NOT on a passing trajectory (read one, or test the "
            "condition with bash/python on its text). Report hits vs false-positives in your "
            "done summary.\n"
            if passing_ids else
            "5) BEFORE done: sanity-check your trigger condition against the failing evidence "
            "text (test it with bash/python) and say in your done summary when it fires.\n"
        )
        + (
            f"\nThis is attempt #{attempt + 1} for this failure mode — take a DISTINCT approach "
            "from earlier attempts: pick a DIFFERENT lever (prompt vs hook vs loop vs tool). "
            "Judge fix-hints are withheld this attempt on purpose: derive the mechanism yourself "
            "from the evidence trajectories.\n"
            if attempt else ""
        )
        + (_BEACON_REQUIREMENT if require_beacon else "")
        + "Begin."
    )
    convo = [
        {"role": "system", "content": system_prompt or _BASH_SYSTEM},
        {"role": "user", "content": user},
    ]
    _FORCE = (
        "\n\nSTOP READING — you have changed NOTHING so far. Your NEXT message MUST be a bash "
        "action that WRITES an edit (not another read). Make the concrete edit now."
    )
    readonly = 0
    last_summary = ""

    def _has_edit() -> bool:
        return bool(sandbox.changed_whitelist() or sandbox.deleted_whitelist())

    for _turn in range(max_turns):
        turns_left = max_turns - _turn - 1
        try:
            raw = call_fn(convo)
        except Exception as exc:  # noqa: BLE001 — a driver error skips this candidate
            # Surface WHY in the summary slot: a silent empty return made a
            # dead driver indistinguishable from "designed nothing" (an entire
            # smoke round produced zero candidates with no trace, 2026-07-09).
            return None, [], f"driver error: {exc}"[:300]
        obj = _loose_json(raw)
        act = (obj or {}).get("action")
        if act == "done":
            if _has_edit():
                last_summary = str((obj or {}).get("summary", ""))[:300]
                break
            fb = (
                "You have NOT edited any file — reading alone does not fix it. Run a bash "
                "command that WRITES a change now, then reply done."
            )
        elif act == "read_trajectory":
            rendered = render_failed(str(obj.get("task_id", ""))) if render_failed else "(unavailable)"
            fb = f"Trajectory:\n{rendered}\n\nContinue (bash) or done."
        elif act == "bash":
            fb = f"$ output:\n{sandbox.bash(str(obj.get('command', '')))}\n\nContinue (bash) or done."
        elif act == "write_file":
            fb = (
                sandbox.write_text(str(obj.get("path", "")), str(obj.get("content", "")))
                + "\n\nContinue (bash) or done."
            )
        else:
            fb = 'Respond with ONE JSON action: {"action":"bash"|"write_file"|"read_trajectory"|"done", ...}'
        edited = _has_edit()
        # Only bash read-only turns count toward the force: read_trajectory is
        # evidence-gathering the PROTOCOL itself mandates (2-3 reads), so
        # counting it punished drivers for following the protocol.
        if edited:
            readonly = 0
        elif act == "bash":
            readonly += 1
        if not edited and (readonly >= force_after_readonly or turns_left <= 2):
            fb += _FORCE
        if edited and turns_left <= 2:
            fb += (
                "\n\nAlmost out of turns — if your change is complete, reply "
                '{"action":"done","summary":"..."} NOW (an unfinished loop loses your summary).'
            )
        fb += f"\n[turns left: {turns_left}]"
        convo += [{"role": "assistant", "content": raw}, {"role": "user", "content": fb}]

    sandbox.scope_restore()
    changed = sandbox.changed_whitelist()
    deleted = sandbox.deleted_whitelist()
    if not changed and not deleted:
        return None, [], ""
    ok, err = sandbox.compile_check(changed)
    if not ok:
        return None, [], f"compile FAIL: {err}"
    if import_smoke_module:
        ok, err = sandbox.import_check(import_smoke_module, python_exe=import_smoke_python)
        if not ok:
            return None, [], f"import smoke FAIL: {err}"
    if require_beacon and not _has_beacon(changed) and any(
        p.endswith(".py") and _code_changed(sandbox.original(p), b)
        for p, b in changed.items()
    ):
        # A code edit without a beacon is unmeasurable by Gate-b (its firing
        # leaves no per-task record), so it is rejected before any eval spend.
        # Prompt/config edits are exempt — .md/.yaml by extension, and .py
        # edits that only touch string constants (the AppWorld prompt lives
        # inside agent_cli.py); their attribution runs on trajectory presence.
        return None, [], "missing activation_beacon in python edit"
    default = f"edited {sorted(changed)}" + (f", deleted {deleted}" if deleted else "")
    return changed or {}, deleted, (last_summary or default)


def make_bash_editor_design_fn(
    call_fn: Callable[[list], str],
    *,
    repo_root: str | Path,
    worktree_root: str | Path,
    sha_of: Callable[[HarnessNode], str],
    budget: Budget,
    render_failed: Optional[Callable[[str], str]] = None,
    render_failed_of: Optional[Callable[[HarnessNode], Callable[[str], str]]] = None,
    passing_ids_of: Optional[Callable[[HarnessNode], list[str]]] = None,
    history: Optional[dict[str, list[dict]]] = None,
    archive_summary_of: Optional[Callable[[], str]] = None,
    guard_text_of: Optional[Callable[[], str]] = None,
    max_turns: int = 22,
    force_after_readonly: int = 4,
    import_smoke_module: Optional[str] = "benchmarks.appworld.agent_cli",
    import_smoke_python: Optional[str] = None,
    system_prompt: Optional[str] = None,
    whitelist_prefixes: Optional[tuple[str, ...]] = None,
    require_beacon: bool = False,
    why_selection: str = "driver",
    why_defs_of: Optional[Callable[[], Optional[dict[str, str]]]] = None,
) -> Callable[[int, dict, HarnessNode], list[Candidate]]:
    """Build the loop's ``design_fn``: select WHYs, run the bash-editor per
    candidate off the parent commit, return :class:`Candidate` objects.

    ``render_failed_of(parent)`` (preferred) binds the trajectory renderer to
    the round's parent, so ``read_trajectory`` shows the CURRENT parent's
    failures; ``render_failed`` is the parent-agnostic fallback.
    ``archive_summary_of`` renders the GSME elite bank into the editor's
    context so the driver neither re-invents an already-banked mechanism nor
    proposes one already tried and rejected in another cell.
    """
    repo_root = Path(repo_root)
    worktree_root = Path(worktree_root)
    history = history if history is not None else {}

    def design_fn(round_index: int, failure_map: dict, parent: HarnessNode) -> list[Candidate]:
        base_sha = sha_of(parent)
        rf = render_failed_of(parent) if render_failed_of else render_failed
        p_ids = passing_ids_of(parent) if passing_ids_of else []
        arch_text = archive_summary_of() if archive_summary_of else ""
        formula = rerank_whys(failure_map, budget.max_why_per_round, history)
        why_reasons: dict[str, str] = {}
        if why_selection == "driver":
            whys, why_reasons = driver_select_whys(
                call_fn, failure_map, budget.max_why_per_round, history,
                why_defs=why_defs_of() if why_defs_of else None,
            )
            if not whys:
                whys = formula
            # Shadow log: driver choice vs formula choice, so a few rounds of
            # data can arbitrate which selector earns the default.
            print(f"[why-select] driver={whys} formula={formula} "
                  f"reasons={why_reasons}", flush=True)
        else:
            whys = formula
        cands: list[Candidate] = []
        for why in whys:
            brief, ev_ids = why_brief(failure_map, why)
            if why_reasons.get(why):
                brief += f"\nTARGET RATIONALE (why this mode was picked): {why_reasons[why]}"
            focused = why_all_tids(failure_map, why)
            for attempt in range(budget.candidates_per_why):
                tag = f"r{round_index}-{why.split('_')[0]}-{attempt}"
                sb = (
                    Sandbox(repo_root, worktree_root / tag, base_sha,
                            whitelist_prefixes=whitelist_prefixes)
                    if whitelist_prefixes is not None
                    else Sandbox(repo_root, worktree_root / tag, base_sha)
                )
                try:
                    changed, deleted, summary = bash_edit_candidate(
                        call_fn, sb, brief if attempt == 0 else _strip_hints(brief),
                        ev_ids, tag,
                        render_failed=rf, passing_ids=p_ids,
                        max_turns=max_turns,
                        force_after_readonly=force_after_readonly,
                        attempt=attempt,
                        history_text="\n\n".join(
                            s for s in (arch_text, _fmt_history(history, why)) if s
                        ),
                        guard_text=guard_text_of() if guard_text_of else "",
                        import_smoke_module=import_smoke_module,
                        import_smoke_python=import_smoke_python,
                        system_prompt=system_prompt,
                        require_beacon=require_beacon,
                    )
                finally:
                    sb.close()
                if changed is not None:
                    cands.append(
                        Candidate(files=changed, why=why, focused_task_ids=focused,
                                  summary=summary, deletions=deleted,
                                  has_beacon=_has_beacon(changed),
                                  activation_spec=_parse_trigger_spec(summary))
                    )
        return cands

    return design_fn


__all__ = [
    "make_bash_editor_design_fn",
    "bash_edit_candidate",
    "driver_select_whys",
    "rerank_whys",
    "why_brief",
    "why_all_tids",
    "WHY_FIX_WEIGHT",
]
