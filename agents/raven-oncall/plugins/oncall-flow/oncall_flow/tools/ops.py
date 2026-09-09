"""The ops tool faces: drive and inspect a long-running campaign from chat.

The fork's tool file, said through the plugin seams. ``ops_tune_status`` reads
a campaign's ledger (the on-disk source of truth) to report progress and every
recorded fact; ``ops_submit`` runs one round and arranges the wake that reads
it; the rest are the listing, the note, the kill and the re-check faces. What
changed in the port is the wiring, never the face: the campaign root comes
from the factory (``tools.base.ops_home``) instead of host config, scheduling
rides the namespaced wake grant instead of the CronService (``wakes.py``, with
``wake_route`` recorded in the campaign's meta as the D9 rebuilt addressing),
and the decision-basis gate is the 2a mechanism layer (``policy.py``).
``ops_tune_launch`` lives in ``tune_launch.py`` and is deliberately not a
manifest contribution -- the fork kept it off the agent's menu so the model
steers round by round; full-auto stays reachable via ``python -m
oncall_flow.tune`` (D5).
"""

from __future__ import annotations

import dataclasses as _dataclasses
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from oncall_flow import wakes
from oncall_flow.actions import RUN as ACTION_RUN
from oncall_flow.budget import from_meta as budget_from_meta
from oncall_flow.escalation import append_note as _append_note
from oncall_flow.policy import BASIS_HELP as _BASIS_HELP
from oncall_flow.policy import basis_refusal as _basis_refusal
from oncall_flow.policy import record_basis as _record_basis
from oncall_flow.tools.base import _OpsScheduler
from oncall_flow.tools.base import _slug as _base_slug
from oncall_flow.tools.base import ops_home as _tools_home
from raven.contracts.tool import Tool


def _occupancy_lines(meta_path: Path, records: list) -> list[str]:
    """The machine's capacity, what is held on it across campaigns, and what is free.

    Printed where the budget is, for the same reason: a loop deciding how many
    configs to submit next used to learn the machine was full only by being
    refused. A row that hands out nothing countable prints nothing -- the
    job-count gate has no capacity to state.
    """
    import json as _json

    from oncall_flow import connections as _conns
    from oncall_flow.occupancy import running_on

    try:
        meta = _json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    conn_id = str(meta.get("connection") or "")
    row = _conns.get(conn_id) if conn_id else None
    if not row:
        return []
    unit = _conns.resource_unit(row)
    if not unit:
        return []
    cap = _conns.capacity(row)
    noun = "device(s)" if unit == "gpus" else "core(s)"
    occupants = running_on(meta_path.parent.parent, conn_id)
    held = sum(o.units(unit) for o in occupants)
    name = _conns.display_name(conn_id) or conn_id
    out = [f"Machine: {name} has {cap[unit]} {noun}; {held} held, {max(0, cap[unit] - held)} free."]
    mine = {r.idem_key for r in records}
    for o in occupants:
        ids = o.held.get("device_ids") or []
        where = f" on {','.join(map(str, ids))}" if ids else ""
        owner = "this campaign" if o.idem_key in mine else f"campaign '{o.campaign}'"
        out.append(f"  {o.idem_key} ({owner}) holds {o.units(unit)} {noun}{where}")
    return out


def _whole_or(value: object, default: int, *, floor: int = 1) -> int:
    if isinstance(value, bool) or value is None:
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n >= floor else default


def _resource_requests(meta: dict, configs: list, unit: str) -> tuple[list[int], list[int]]:
    """Per config: units of ``unit`` held, and GB of memory held (0 = undeclared).

    The campaign's declaration (``meta["resources"]``, written by ops_declare) is
    the default; a config overrides with ``gpus_needed`` / ``cores_needed`` /
    ``memory_needed_gb``. A campaign declared before resources existed holds one
    unit per job, which is what it was billed as.
    """
    declared = meta.get("resources") if isinstance(meta.get("resources"), dict) else {}
    default_n = _whole_or(declared.get(f"{unit}_per_job"), 1)
    default_mem = _whole_or(declared.get("memory_per_job_gb"), 0, floor=0)
    key = "gpus_needed" if unit == "gpus" else "cores_needed"
    reqs, mems = [], []
    for cfg in configs:
        cfg = cfg if isinstance(cfg, dict) else {}
        reqs.append(_whole_or(cfg.get(key), default_n))
        mems.append(_whole_or(cfg.get("memory_needed_gb"), default_mem, floor=0))
    return reqs, mems


def _refuse_foreign_instance(ledger: Path) -> None:
    """Refuse an explicit ledger that lives inside ANOTHER raven instance.

    The explicit-ledger branch exists so a caller can point a tool at exactly
    the campaign it means, and it stays that way for everything inside this
    instance, for test fixtures in scratch directories, and for plain files
    anywhere else. What it must not do is reach into a different instance:
    measured 2026-08-25, a woken subagent turn was handed (by a wake message a
    misconfigured shell wrote) the DEFAULT instance's path for a same-named
    campaign, read the wrong experiment's ledger, and then concluded it --
    writing a finished report into a paused experiment it had nothing to do
    with. The 2026-08-14 rule closed this door for wakes; this closes the same
    door where a path is the key.

    Another instance is recognised by its shape rather than a list nobody
    maintains: some ancestor of the ledger holds a config.json beside the ops/
    tree the ledger sits in. No such ancestor -- a tmp dir, an archive copy, a
    bare file -- is nobody's instance, and stays reachable.
    """
    try:
        own_home = _ops_home().expanduser().resolve()
        led = ledger.resolve()
    except OSError:
        return
    for a in led.parents:
        # The fork's shape ("ops") still names a fork instance's tree; the
        # plugin's own home name covers a sibling activation's stateRoot.
        if a.name != "ops" and a.name != own_home.name:
            continue
        root = a.parent
        if (root / "config.json").is_file():
            if a != own_home:
                raise ValueError(
                    f"the ledger at {ledger} belongs to a different raven instance "
                    f"(rooted at {root}); this instance's campaigns live under "
                    f"{own_home}. Name the campaign instead of a foreign path -- "
                    f"another instance's experiment is not this one's to read or close."
                )
            return


def _ops_home() -> Path:
    """The campaign root for this activation (factory-installed; tools.base)."""
    return _tools_home()


def _slug(text: str, limit: int = 24) -> str:
    return _base_slug(text, limit)


def _campaign_dir(host: str, objective: str) -> Path:
    return _ops_home() / f"{host.replace('.', '-')}_{_slug(objective)}"


def _resolve_campaign_dir(
    campaign: str | None,
    ledger: str | None = None,
    session_key: str = "",
    task: str = "",
) -> Path:
    """The campaign's state directory.

    An explicit ledger path wins, then an explicit name. With neither, fall back to
    the one campaign under the ops home -- because a campaign name and its ledger
    path are an API handle ("state lives over there") that had to be typed into
    every task statement, and nobody asking for the work has a reason to know it.

    Exactly one, or an error naming the candidates. Silently picking one of several
    would be the worst of the three outcomes: the wrong campaign reads exactly like
    the right one, and the loop would drive somebody else's experiment.
    """
    if ledger and str(ledger).strip():
        led = Path(ledger).expanduser()
        _refuse_foreign_instance(led)
        return led.parent
    if campaign:
        d = _ops_home() / _slug(campaign)
        # Naming a campaign IS this window saying what it is working on. Claiming
        # used to happen only as a side effect of a successful submit, but a
        # window's first call is a read -- so the two-window case broke on the
        # first thing either window did. Not for a campaign that has already
        # finished: reading its report is ordinary, and the window is still
        # working on whatever it was working on.
        if session_key and d.is_dir() and not (d / "concluded.json").exists():
            from oncall_flow.window import bind_window

            bind_window(_ops_home(), session_key, d.name, pid=os.getpid())
        return d
    home = _ops_home()
    # What this window said it was working on, before any guessing. Counting
    # directories cannot tell two windows apart, and two windows each watching an
    # experiment is the ordinary case of using this without --config.
    # Tier 1: what this window claimed. Tier 2: what this task belongs to -- a
    # session key dies with its window, the statement the operator handed over
    # does not, and that is how a fresh window recognises the watch it is taking
    # over. Tier 3 is the error below, reached once per window at most.
    if session_key:
        from oncall_flow.window import campaign_for_window

        bound = campaign_for_window(home, session_key)
        for cand in [home / bound, home / _slug(bound)] if bound else []:
            # A finished experiment does not hold the window. One window doing one
            # experiment after another is ordinary, and the binding named the
            # finished one -- so the next statement's fingerprint was never asked,
            # and the agent's first call answered "there is nothing left to do
            # here" to "start the second experiment". The same exclude-concluded
            # rule already applied to counting directories; this is the level it
            # was missing from.
            if cand.is_dir() and (cand / "concluded.json").exists():
                break
            # Literally first: a name is not always its own slug, and on a
            # case-sensitive filesystem re-slugging "dambreak-legA" points at a
            # directory that does not exist. macOS matched both and hid it.
            if cand.is_dir():
                return cand
    if task:
        from oncall_flow.window import bind_window, campaign_for_task

        rejoined = campaign_for_task(home, task, exclude_live=True)
        if rejoined and (home / rejoined).is_dir():
            if session_key:
                bind_window(home, session_key, rejoined, pid=os.getpid())
            return home / rejoined
    found = sorted(d.name for d in home.iterdir() if d.is_dir()) if home.exists() else []
    # Finished campaigns are not candidates. From the second task in one instance
    # onward every unnamed call read as ambiguous, which is the whole of "you
    # cannot do two tasks in one window" -- and the cron service, deciding the
    # same question, already skipped them. Two answers to one question, and this
    # was the wrong one.
    live = [n for n in found if not (home / n / "concluded.json").exists()]
    if len(live) == 1:
        # Unless another window is watching it. "Only one campaign is live, so it
        # must be the one you mean" holds for a single window and hands one
        # window the other's experiment as soon as there are two -- measured while
        # walking one window finishing its first experiment while a second window
        # still ran its own.
        from oncall_flow.window import has_live_window

        if not has_live_window(home, live[0], exclude_session=session_key):
            return home / live[0]
    elif len(live) > 1:
        pass
    if not found:
        raise ValueError(f"no campaign under {home}; name one or create it first")
    if not live:
        # Listing finished names points at the wrong action: the answer is not
        # "which of these", it is "that work is over, this is new work".
        raise ValueError(
            f"every campaign under {home} has finished ({', '.join(found)}); this is new work, so give it a new name"
        )
    # Names alone cannot be matched against a task statement. The host, the case
    # and the budget can -- they are the things a statement names -- so an agent
    # holding one can pick without being told, and so can a person.
    raise ValueError(
        f"{len(live)} live campaigns under {home}; this window has not said which "
        f"one it is watching:\n"
        + "\n".join(f"  {n}   {_campaign_gist(home / n)}" for n in live)
        # And what to do when none of them is the task in hand. Without this line the
        # cheapest move is to take the nearest row, which is worse than asking:
        # naming a campaign binds this window to it, and the work then goes to
        # another experiment's ledger and budget. Two of the six live on
        # 2026-08-17 differed only in their objective.
        + "\nCall any ops tool once with campaign='<name>'; this window then stays on it."
        + " If none of these is the task you were given, do not take the nearest one:"
        + " say so with ops_ask_owner, because naming one binds this window to it."
    )


def _effective_config_lines(
    runner, remote_dir: str, keys: list[str], declared: dict | None = None
) -> dict[str, list[str]]:
    """Per trial, how the run differs from what was asked for.

    One remote call for the whole campaign: a status that opened a connection per
    trial would make looking expensive, and looking often is the behaviour the
    whole loop is built to encourage.

    Best effort throughout. A host that cannot answer, a job that writes no
    ``config.effective.json`` (most do not), a file that is not JSON -- all mean
    "nothing to say here", never a failed status call.
    """
    import json as _eff_json
    import shlex as _eff_shlex

    from oncall_flow.effective import describe

    if not runner or not remote_dir or not keys:
        return {}
    parts = []
    for k in keys:
        d = _eff_shlex.quote(f"{remote_dir.rstrip('/')}/jobs/{k}")
        parts.append(
            f'echo "@@{k}"; cat {d}/config.json 2>/dev/null; echo "@@EFF"; cat {d}/config.effective.json 2>/dev/null'
        )
    try:
        rc, out = runner("; ".join(parts))
    except Exception:  # noqa: BLE001 -- looking must not fail on the host
        return {}
    # Not gated on rc: the chain's status is its last command's, and a trial with
    # no config.effective.json ends in a failed cat -- which threw away every
    # other trial's answer while looking like "nothing to report". Emptiness is
    # the only reliable signal that the host said nothing.
    if not out.strip():
        return {}

    def _load(s: str):
        try:
            v = _eff_json.loads(s.strip() or "null")
            return v if isinstance(v, dict) else None
        except ValueError:
            return None

    # Blocks come out in pairs: the trial's name then "EFF", each followed by a
    # file's contents. Split consumed the markers, so they are matched by order.
    found: dict[str, list[str]] = {}
    pending: str | None = None
    submitted: dict | None = None
    for block in out.split("@@")[1:]:
        head, _, body = block.partition("\n")
        head = head.strip()
        if head == "EFF":
            if pending:
                lines = describe(submitted, _load(body), declared)
                if lines:
                    found[pending] = lines
            pending, submitted = None, None
        elif head in keys:
            pending, submitted = head, _load(body)
    return found


def _where(meta: dict[str, Any]) -> str:
    """Where a campaign's work runs, in the owner's words when there are any.

    Never raises: a campaign that names a connection carries no address of its
    own, and a line that only says where the work went must not be the thing
    that fails the submit.
    """
    conn = str(meta.get("connection") or "").strip()
    if conn:
        from oncall_flow.connections import display_name

        return display_name(conn)
    return str(meta.get("host") or "the campaign's machine")


def _has_live_watcher(campaign: str) -> bool:
    """Whether some open window is watching this campaign.

    A campaign whose window was closed keeps its pending wake, and that wake
    stays put rather than firing in whatever window is open next. So the listing
    has to say which ones are in that state: it is the only way a window can ask
    "what is waiting for someone" before naming one to take over.
    """
    try:
        from oncall_flow.window import _pid_alive, window_for_campaign

        bound = window_for_campaign(_ops_home(), campaign)
    except Exception:  # noqa: BLE001 -- an unreadable index must not break a listing
        return True
    return bool(bound and bound[1] and _pid_alive(int(bound[1])))


def _campaign_gist(campaign_dir: Path) -> str:
    """One line saying what a campaign is, in the terms a task statement uses."""
    import json as _gist_json

    try:
        meta = _gist_json.loads((campaign_dir / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "(no readable meta.json)"
    bits: list[str] = []
    # The owner's word for the machine, when the campaign names one. An address
    # does not identify a machine -- two connections can share one -- so the name
    # is what makes "which box is this experiment on" answerable at a glance.
    if conn := str(meta.get("connection") or "").strip():
        from oncall_flow.connections import display_name

        bits.append(f"on {display_name(conn)}")
    elif host := str(meta.get("host") or "").strip():
        bits.append(f"host {host}")
    for key in ("staged_case", "remote_dir"):
        if val := str(meta.get(key) or "").strip():
            bits.append(f"{key.replace('_', ' ')} {val}")
            break
    budget = meta.get("budget")
    if isinstance(budget, dict) and budget.get("total") is not None:
        bits.append(f"{budget['total']} {budget.get('unit') or 'unit'}")
    # What each one is for. Two campaigns can share a machine, a case directory and
    # a budget and differ only in what they are optimising -- measured 2026-08-17,
    # when two FEA campaigns rendered as the same line but for their names, leaving
    # a window to tell them apart by guessing which name meant which task.
    obj = meta.get("objective")
    if isinstance(obj, dict) and obj.get("metric"):
        arrow = {"max": "max", "min": "min"}.get(str(obj.get("direction")), "")
        bits.append(f"{arrow} {obj['metric']}".strip())
    elif isinstance(obj, dict) and obj.get("condition"):
        # A campaign with no metric still has a target, and it is the one thing
        # that tells two watches on the same machine apart.
        bits.append(f"watching for {str(obj['condition'])[:60]}")
    elif isinstance(obj, dict) and obj.get("kind"):
        bits.append(str(obj["kind"]))
    return "   ".join(bits) or "(meta.json names nothing to tell it by)"


def _read_notes(campaign_dir: Path) -> list[dict]:
    """Read the campaign's user-instruction notes (oldest first)."""
    import json

    path = campaign_dir / "notes.jsonl"
    if not path.exists():
        return []
    notes = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            notes.append(json.loads(line))
        except ValueError:
            continue
    return notes


_HISTORY_LIMIT = 12

# A failed job's own last words, capped. Long enough for a stack frame or an
# OpenFOAM FATAL line, short enough that the campaign's state stays readable.
_FAILURE_REASON_CHARS = 400


def _case_isolation_lines(records: list[Any], has_staged_case: bool = True) -> list[str]:
    """Whether trials of this campaign can run at the same time, as measured.

    A trial gets the owner's case as a tree of symlinks, so anything a run creates
    lands in its own directory. Overwriting a file that already exists is the one
    thing that escapes -- that write follows the link back into the case -- and the
    backend lists exactly which files it happened to.

    Four states, and the difference between the first two is the point: a campaign
    where no round has finished has not been shown to be safe, which is not the
    same claim as having looked and found nothing. There is no confirmation round:
    every round measures this, so whichever round runs next says whether the last
    finding settled, at no extra compute.

    Reported here rather than left in the trial's raw output because it decides how
    the rest of the campaign is allowed to run. Twenty cases overwriting each
    other's inputs read as twenty results.
    """
    latest: list[str] | None = None
    ever: set[str] = set()
    for r in records:
        if r.result is None:
            continue
        written = (r.result.output or {}).get("case_files_written")
        if written is None:
            continue
        latest = [str(w) for w in written]
        ever.update(latest)
    if latest is None:
        if not has_staged_case:
            return []
        return [
            "Case isolation: not measured yet -- no round has finished. Until one "
            "has, run trials one at a time: whether they can share the owner's case "
            "is unproven, not proven safe."
        ]
    if not latest:
        if ever:
            settled = ", ".join(sorted(ever)[:10])
            return [
                f"Case isolation: earlier rounds wrote {settled}; those are real "
                "copies in the trial directory now, and the last round wrote nothing. "
                "Trials can run at the same time."
            ]
        return [
            "Case isolation: measured, and no file in the owner's case was written. Trials can run at the same time."
        ]
    shown = ", ".join(sorted(latest)[:10])
    more = " ..." if len(latest) > 10 else ""
    return [
        f"Case isolation: the last round wrote into the owner's case -- {shown}{more}",
        "  Those files are real copies from the next round on, so the write should "
        "land in the trial directory instead. Whichever round runs next says whether "
        "it did -- there is nothing to run on purpose to find out.",
        "  Until then, do NOT run trials at the same time: each one is overwriting "
        "what the others read, and the numbers would be of a case that kept changing "
        "underneath.",
        "  If a path is fixed inside a compiled binary and the source cannot be "
        "changed, raven cannot make it local -- that is the owner's call, so ask with "
        "ops_ask_owner.",
    ]


# Keys this layer writes itself. They are readings about the run, not the score
# the run is being judged by, so they can never be the answer to "which metric".
_INFRASTRUCTURE_METRICS = frozenset({"gpu_minutes_used"})


def _campaign_metric(meta_path: Path, asked: str, records: "list | None" = None) -> str:
    """The metric name to read back: asked for, else declared, else observed.

    This tool layer is shared with every domain that runs jobs, so it must not
    know any metric by name. It used to fall back to "ndcg", which is how a CFD
    campaign came to read back "trials succeeded but none reported metric 'ndcg'"
    for runs that reported theirs perfectly well.

    The third step replaces that fallback. Campaigns predating ``objective`` (r13
    and r14) are still on disk, and rewriting their meta to suit today's code
    would edit the record of what the device was while they ran. What the records
    themselves report is a fact rather than a guess: exactly one candidate is the
    answer, and zero or several leave the name unresolved -- the caller is then
    told which names exist, which is a better prompt than a wrong key.
    """
    if asked:
        return asked
    try:
        import json as _json

        meta = _json.loads(meta_path.read_text(encoding="utf-8"))
        declared = (meta.get("objective") or {}).get("metric")
        if declared:
            return str(declared)
    except Exception:
        pass
    observed = {
        name
        for r in (records or [])
        if getattr(r, "result", None) is not None
        for name in (r.result.metrics or {})
        if name not in _INFRASTRUCTURE_METRICS
    }
    return observed.pop() if len(observed) == 1 else ""


def _budget_line(
    meta: dict,
    *,
    spent: float | None = None,
    remaining: float | None = None,
    unmeasured: dict | None = None,
    spend_error: str | None = None,
    cdir: Path | None = None,
) -> str:
    """The campaign's allowance and what is left of it, in its own unit.

    The unit comes from the declaration and is printed, never interpreted: this
    layer is shared by every domain that runs jobs, and it used to say "GPU
    minutes" to a solver campaign spending core-minutes.

    An undeclared budget says so rather than printing nothing. Silence reads as
    "there was no line about budget", which is indistinguishable from "this
    campaign has no limit" -- and the second is the case where the loop most needs
    to know, since nothing will stop it.

    **The spend is printed, and that is a change.** This line used to carry the
    total alone, on the reasoning that subtracting is the caller's judgement. That
    reasoning holds only if the caller can obtain the spend, and it cannot: what a
    solver log offers is one job's ExecutionTime, which is not the campaign's spend
    once there are several jobs, or a killed one, or a core count. Measured
    2026-08-12 -- a loop derived the spend from ExecutionTime and multiplied by a
    core count its job script silently ignores, read 70.9 core-minutes against a
    true 25.33, and cut the task's endTime in half inside a budget that covered the
    original. Its arithmetic was right; the premise was missing from the only place
    that could supply it.

    What is still left to the caller is the judgement: whether the remainder covers
    finishing. That needs a rate extrapolated from readings, and none of it is here.

    A spend that could not be read says so. A zero would read as "nothing spent
    yet", which is the state a loop acts on most freely.

    Not every budget is machine time, and the ones that are not are answered from
    the campaign's own record rather than from the host -- a watch that ran no
    jobs has spent no compute however long it has been watching, so the host's
    reading would be zero and would read as untouched. Which of the two applies
    is the declaration's to say (``budget.meter``), never inferred from the unit.
    """
    declared = budget_from_meta(meta)
    if declared is None:
        return "Campaign compute budget: none declared (nothing will stop a run on the total)."
    if declared.off_machine:
        return _watch_budget_line(declared, cdir)
    line = f"Campaign compute budget: {declared.total:g} {declared.unit} in total"
    if spend_error:
        return f"{line}; spend could not be read from the host ({spend_error})."
    if spent is None:
        return f"{line}."
    # Two decimals: this is core-minutes off a wall clock, and a raw float
    # printed 25.3333 where the reading is 25.33.
    left = f", {round(remaining, 2):g} left" if remaining is not None else ""
    line = f"{line}; {round(spent, 2):g} used{left} (measured on the host)."
    if unmeasured:
        named = "; ".join(f"{k} ({v})" for k, v in sorted(unmeasured.items()))
        # Named rather than folded into the total: a spend summed over the jobs that
        # happen to be measurable reads complete and is not. Measured on the ML
        # line, three of six trials recorded no duration at all.
        line += f" Spend not measurable for: {named}."
    return line


def _watch_budget_line(declared, cdir: Path | None) -> str:
    """The allowance for a watch, and what the trail says is left of it.

    Both figures come off the campaign's own record, so the wording says so: the
    host is not the source here and a line that read "measured on the host" would
    misattribute it. Without a campaign directory the total is still printed --
    which budget applies is a fact about the campaign, and withholding it because
    the spend could not be located is how a loop ends up carrying the number in
    its head.
    """
    from oncall_flow.attendance import off_machine_spend
    from oncall_flow.budget import LOOKS

    counted = "looks" if declared.meter == LOOKS else "watching"
    line = f"Campaign watch budget: {declared.total:g} {declared.unit} of {counted} in total"
    if cdir is None:
        return f"{line} (spend is read from the campaign's own record)."
    spent = off_machine_spend(cdir, declared)
    if spent is None:
        return f"{line}; spend could not be read from the campaign's record."
    left = max(0.0, declared.total - spent)
    return (
        f"{line}; {round(spent, 2):g} used, {round(left, 2):g} left "
        f"(counted from this campaign's own record, not the host)."
    )


def _campaign_meta(cdir: Path) -> dict:
    """The campaign's declaration, or {}. Never raises."""
    import json as _j

    try:
        return _j.loads((cdir / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _resolve_meta(meta: dict) -> dict:
    """The campaign's meta with its connection's address filled in.

    The same seam the backends use: a campaign names a machine and the registry
    holds the address, so anything that wants to run a command has to go through
    here rather than read meta["host"], which a declared campaign does not have.
    """
    from oncall_flow.connections import resolve_into

    return resolve_into(dict(meta))


def _advance_probe_seq(cdir: Path) -> None:
    """Count this look. Best-effort; a lost count is better than a lost answer."""
    try:
        from oncall_flow.state_claims import read_facts, write_facts

        facts = read_facts(cdir)
        write_facts(cdir, _dataclasses.replace(facts, probe_seq=facts.probe_seq + 1))
    except Exception:  # noqa: BLE001
        pass


async def _take_wake_readings(cdir: Path, meta: dict | None = None) -> None:
    """Take the each_wake readings. Once per look, whatever else the look found."""
    from oncall_flow import readings as ops_readings

    try:
        await ops_readings.take(meta if meta is not None else _campaign_meta(cdir), cdir, ops_readings.EACH_WAKE)
    except Exception:  # noqa: BLE001 -- a reading never costs the caller its answer
        pass


async def _take_during_readings(meta: dict, cdir: Path, backend, idem_key: str) -> None:
    """Take the during_trial readings for a trial that is still running."""
    from oncall_flow import readings as ops_readings

    try:
        job_dir = _job_dir_of(backend, idem_key)
        if not job_dir:
            return
        await ops_readings.take(meta, cdir, ops_readings.DURING_TRIAL, job_dir=job_dir, trial=idem_key)
    except Exception:  # noqa: BLE001
        pass


async def _take_after_trial_readings(meta: dict, cdir: Path, backend, led, rec) -> None:
    """Take a finished trial's readings, once, and file the numbers with it.

    Once per trial and per name: a job directory that has been cleaned up fails
    the same way on every look, and retrying would write one error per wake
    forever. The numeric values are merged into the trial's metrics as well as
    the readings record, because that is the field every existing reader ranks
    and reports on.
    """
    import dataclasses as _dc

    from oncall_flow import readings as ops_readings

    try:
        table = [r for r in ops_readings.declared(meta) if r.when == ops_readings.AFTER_TRIAL]
        if not table:
            return
        done = ops_readings.taken_for(cdir, rec.idem_key)
        due = [r for r in table if r.name not in done]
        if not due:
            return
        job_dir = _job_dir_of(backend, rec.idem_key)
        if not job_dir:
            return
        rows = await ops_readings.take(
            {**meta, "readings": [{"name": r.name, "command": r.command, "when": r.when} for r in due]},
            cdir,
            ops_readings.AFTER_TRIAL,
            job_dir=job_dir,
            trial=rec.idem_key,
        )
        numbers = {}
        for row in rows:
            value = ops_readings.numeric(row.get("value"))
            if value is not None:
                numbers[str(row["name"])] = value
        if numbers:
            merged = dict(rec.result.metrics or {})
            # The trial's own metrics win: those came from the run's own result
            # file, and a reading is a second way of getting at the same thing.
            merged.update({k: v for k, v in numbers.items() if k not in merged})
            led.set_result(rec.idem_key, _dc.replace(rec.result, metrics=merged))
    except Exception:  # noqa: BLE001
        pass


def _job_dir_of(backend, idem_key: str) -> str:
    """Where this trial's directory is, if the backend can say."""
    getter = getattr(backend, "job_dir", None)
    if not callable(getter):
        return ""
    try:
        return str(getter(idem_key) or "")
    except Exception:  # noqa: BLE001
        return ""


def _reading_series_lines(cdir: Path, limit: int = 12) -> list[str]:
    """Every declared reading's record, oldest first, with its starting value.

    The series and not the latest value. F2's four rounds read -70.88, -0.44,
    -10.47 and 9.9e-07: the fact worth having is that the sequence is not
    monotone, and no single point carries it. Same for the timestep that
    collapsed over an hour -- every individual sample is just a small number.

    Operands only. No trend word, no "improving", no best-so-far: which way a
    series is going is the reading the caller is here to do.
    """
    from oncall_flow import readings as ops_readings

    series = ops_readings.series(cdir, limit=limit)
    if not series:
        return []
    start = ops_readings.baseline(cdir)
    out = ["Readings (taken by this campaign, oldest first):"]
    for name, rows in sorted(series.items()):
        shown = []
        for row in rows:
            if row.get("when") == ops_readings.AT_DECLARE:
                continue
            if "error" in row:
                shown.append(f"[could not read: {str(row['error'])[:60]}]")
            else:
                shown.append(_short(row.get("value")))
        head = f"  {name}"
        if name in start:
            head += f"   at declare {_short(start[name])}"
        out.append(head + (("   " + " -> ".join(shown)) if shown else "   (nothing since)"))
    return out


def _short(value) -> str:
    """One reading, short enough to sit in a series line."""
    import json as _j

    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (dict, list)):
        text = _j.dumps(value, ensure_ascii=False)
        return text if len(text) <= 80 else text[:77] + "..."
    text = str(value)
    return text if len(text) <= 80 else text[:77] + "..."


def _watch_budget_spent(cdir: Path, *, attempting: str = "more") -> str:
    """Why this campaign may not go on, or "".

    Only for a budget the campaign itself meters. A compute budget is enforced
    where the compute is bought -- the backend refuses a submit it cannot pay for
    -- and arranging another look costs no machine time, so nothing there stops a
    watch that has run out of the thing it was actually spending.
    """
    import json as _j

    try:
        meta = _j.loads((cdir / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    declared = budget_from_meta(meta)
    if declared is None or not declared.off_machine:
        return ""
    from oncall_flow.attendance import off_machine_spend

    spent = off_machine_spend(cdir, declared)
    if spent is None or spent < declared.total:
        return ""
    return (
        f"REFUSED: this campaign's watch budget is spent -- {round(spent, 2):g} of "
        f"{declared.total:g} {declared.unit}, counted from its own record -- so {attempting} "
        f"is beyond what it was given.\n"
        f"The watch is over; what remains is to say what it saw. Finish with ops_finish, "
        f"reporting what you observed and what it started from. 'The condition never held' "
        f"is a result and belongs in that report, with the readings behind it. Nothing was "
        f"started or scheduled by this call, and the turn is still yours."
    )


def _concluded_notice(cdir: Path) -> str | None:
    """The CONCLUDED banner for this campaign, or None while it is live.

    One renderer for both status paths. The ledger path always carried this
    banner; the no-ledger path returned before reaching it, so a zero-trial
    watch read back as live after its own conclusion -- measured 2026-08-28,
    a look-then-re-arm on a concluded campaign scheduled a fresh wake and each
    look kept spending the closed campaign's budget.
    """
    import json as _json

    concluded_path = cdir / "concluded.json"
    if not concluded_path.exists():
        return None
    try:
        c = _json.loads(concluded_path.read_text(encoding="utf-8"))
        return (
            f"⚠️ CONCLUDED at {c.get('concluded_at', '?')}"
            + (f" ({c['outcome']})" if c.get("outcome") else "")
            + (f" -- {c['reason']}" if c.get("reason") else "")
            + ". This campaign is over: do NOT submit more rounds, and do "
            "not report again. There is nothing left to do here."
        )
    except (OSError, ValueError):
        return "⚠️ CONCLUDED. This campaign is over: do NOT submit more rounds, and do not report again."


def _meta_lines(meta_path: Path) -> list[str]:
    """Everything the campaign's own setup states, ready to print.

    Extracted so the pre-first-submit path can print it too. Before this, a status
    call with only meta.json on disk returned a single "no ledger yet" line and
    dropped the budget, the starting value and the operating policy -- the early
    return sat above the code that printed them. That is exactly the moment the
    policy matters most, because it is when the loop decides how much of the budget
    to ask for. Measured 2026-08-07: the arm then spent six ssh calls hunting for
    the port and the budget it had just been denied.

    Operands only: no remainder is computed, no comparison to the starting value is
    made, nothing is ranked. Those are the judgement under measurement.
    """
    import json as _j

    if not meta_path.exists():
        return []
    try:
        meta = _j.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: list[str] = []
    out.append(_budget_line(meta, cdir=meta_path.parent))
    refs = meta.get("reference_values")
    if isinstance(refs, dict) and refs:
        stated = ", ".join(f"{k} = {v}" for k, v in sorted(refs.items()))
        out.append(f"Campaign recorded starting value: {stated}")
    seed = meta.get("seed_config")
    if isinstance(seed, dict) and seed:
        out.append(
            f"Campaign declared starting config: {_j.dumps(seed, ensure_ascii=False)} "
            "-- round 0 runs exactly this. You may correct a value here you can show is wrong "
            "(a water viscosity of 1e-3, a unit off by a thousand) by submitting the correction "
            "with a basis saying how you know; you may not change it because it is expensive or "
            "awkward -- a smaller mesh or fewer batches answers a different question than the "
            "one you were given. From round 1 change it freely, once a reading says what to change"
        )
    policy = _policy_lines(meta.get("operating_policy"))
    if policy:
        out.append("Campaign operating policy (as configured):")
        out.extend(policy)
    else:
        out.append("Campaign operating policy: (none configured)")
    return out


def _policy_lines(policy: Any) -> list[str]:
    """Render a campaign's operating policy for printing, verbatim.

    Three shapes accepted, because a campaign with two rules should not have to
    wrap them in the full structure:

      - a list of strings          -> read as hard triggers
      - {"hard_triggers": [...], "exemplars": [...], "playbook_skill": "..."}
      - anything else / empty      -> no lines, and the caller says "(none configured)"

    Exemplars are printed as saw / thought / did, in the configured order. The
    order matters and is preserved: an exemplar list whose cases all end in "act"
    reads as "always act", so whoever authors it puts a wait-case in on purpose --
    a loop that always kills is broken in the mirror direction of one that never
    does. Reordering here would defeat that.
    """
    if not policy:
        return []
    out: list[str] = []
    if isinstance(policy, (list, tuple)):
        return [f"  - {str(item)}" for item in policy if str(item).strip()]
    if not isinstance(policy, dict):
        return [f"  - {policy}"]

    triggers = policy.get("hard_triggers") or []
    if isinstance(triggers, (list, tuple)) and triggers:
        out.append("  Stop-now conditions:")
        out.extend(f"    - {t}" for t in triggers if str(t).strip())

    exemplars = policy.get("exemplars") or []
    if isinstance(exemplars, (list, tuple)) and exemplars:
        out.append("  How this kind of run has been read before:")
        for i, ex in enumerate(exemplars, 1):
            if not isinstance(ex, dict):
                out.append(f"    {i}. {ex}")
                continue
            out.append(f"    {i}. saw:     {ex.get('saw', '')}")
            out.append(f"       thought: {ex.get('thought', '')}")
            out.append(f"       did:     {ex.get('did', '')}")

    skill = policy.get("playbook_skill")
    if skill:
        out.append(f"  Remedies for these situations: skill {skill} (read its SKILL.md).")

    # Anything the three known keys do not cover, printed as written. The
    # structured shape is this line's; the CFD line writes flat keys of its own
    # ({"example_1": ...}), and a renderer that understands only its own schema
    # dropped those silently -- the operator wrote a rule, the status output had
    # no heading for it, and nothing anywhere said a rule had been discarded.
    # Whoever authors a policy is the one who decides what belongs in it.
    known = {"hard_triggers", "exemplars", "playbook_skill"}
    for key, value in policy.items():
        if key in known:
            continue
        out.append(f"  {key}: {value}")
    return out


def _same_value(a: Any, b: Any) -> bool:
    """Whether two config values name the same setting.

    Compared as numbers when both read as numbers, so a declaration of "5e-4"
    and a submit of 0.0005 are one starting point rather than two; otherwise as
    trimmed text, so 1 and "1" agree as well.
    """
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a).strip() == str(b).strip()


def _seed_departures(seed: Any, configs: list[dict[str, Any]]) -> list[str]:
    """How a round-0 submit differs from the declared starting point.

    Returns one line per difference, empty when the submit is the declaration.
    The keys are not interpreted -- this holds for any domain, because what is
    compared is the campaign's own words against the submit.
    """
    declared: list[dict[str, Any]] = []
    if isinstance(seed, dict) and seed:
        declared = [seed]
    elif isinstance(seed, (list, tuple)) and seed:
        declared = [dict(c) for c in seed if isinstance(c, dict)]
    if not declared:
        return []

    if len(configs) != len(declared):
        return [f"the campaign declares {len(declared)} config(s) for round 0, this submit has {len(configs)}"]

    out: list[str] = []
    for want, got in zip(declared, configs):
        for k, v in want.items():
            if k not in got:
                out.append(f"{k}: declared {v!r}, not passed at all")
            elif not _same_value(v, got[k]):
                out.append(f"{k}: declared {v!r}, submitted {got[k]!r}")
        for k in got.keys() - want.keys():
            out.append(f"{k}: not part of the declared start, submitted {got[k]!r}")
    return out


def _campaign_history(campaign_dir: Path) -> list[str]:
    """The campaign's own events, oldest first, one line each, verbatim.

    A wake runs in a fresh session, so a conclusion that lives only in one turn's
    text is gone by the time it matters. Measured 2026-08-06 on two independent
    lines: a turn worked out that a fluid property was a thousand times too large,
    said so, and the wake thirty minutes later knew nothing of it; and on the ML
    line the same quantity (steps per epoch) came out right inside one continuous
    turn and wrong by 2.5x on every cold-start wake.

    Rendered at call time rather than baked into the wake message, because that
    message is written when the wake is *scheduled* and read back unchanged when
    it fires -- an injection there would be a snapshot from up to an hour earlier
    while looking entirely current.

    Deliberately not summarised, ranked, or counted ("you have waited 3 times"):
    those are conclusions, and drawing them is the loop's job. This prints
    operands, the same rule the metric series follows.
    """
    import json as _j

    path = campaign_dir / "events.jsonl"
    if not path.exists():
        return []
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            e = _j.loads(line)
        except ValueError:
            continue
        ts = str(e.pop("ts", "?"))
        kind = str(e.pop("kind", "?"))
        rest = " ".join(f"{k}={_j.dumps(v, ensure_ascii=False)}" for k, v in e.items())
        out.append(f"[{ts}] {kind} {rest}".rstrip())
    return out[-_HISTORY_LIMIT:]


class OpsTuneStatusTool(Tool):
    """Report a tuning campaign's progress and best config from its ledger."""

    @property
    def name(self) -> str:
        return "ops_tune_status"

    @property
    def description(self) -> str:
        return (
            "Report the progress of a campaign -- a long remote computation of any kind, solver "
            "case or training run alike: how many trials have finished, whether it is still "
            "running, and the best result so far. "
            "Pass the ledger path that ops_tune_launch returned. "
            "Call it with NO ARGUMENTS to read the campaign that is already set up: that is where the host and port, the starting config, the compute budget, the starting value and the operating policy live, so this is the first thing to do on an ops task rather than looking for the machine yourself. Name the campaign only when more than one exists. "
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "ledger": {"type": "string", "description": "Ledger path returned by ops_tune_launch."},
                "metric": {
                    "type": "string",
                    "description": "Metric to rank by; defaults to the campaign's declared objective.",
                },
            },
            "required": [],
        }

    async def execute(self, ledger: str = "", metric: str = "", **kwargs: Any) -> str:
        import json as _json

        from oncall_flow.backend import JobStatus
        from oncall_flow.backends import backend_from_meta
        from oncall_flow.ledger import Ledger

        # No ledger given: fall back to the one campaign under the ops home, so the
        # handle does not have to be typed into every task statement. Ambiguity is
        # reported, never guessed (see _resolve_campaign_dir).
        if not str(ledger).strip():
            try:
                path = _resolve_campaign_dir(kwargs.get("campaign") or "", None) / "ledger.json"
            except ValueError as exc:
                return f"Cannot tell which campaign to read: {exc}"
        else:
            path = Path(ledger).expanduser()
        if not path.exists():
            # Concluded first, before anything is read or spent: a zero-trial
            # campaign never grows a ledger, so this branch is the only status
            # it ever renders -- and a look taken here would spend the budget of
            # a campaign that already reported itself over.
            over = _concluded_notice(path.parent)
            if over:
                pre = _meta_lines(path.with_name("meta.json"))
                return "\n".join([over, *pre, *_reading_series_lines(path.parent)])
            # Not started yet is not a reason to withhold what the campaign states:
            # this is precisely when the loop decides how much budget to ask for.
            # And a watch may live entirely on this path: a campaign waiting for a
            # condition submits nothing, so if the readings were only taken where
            # trials are reconciled, the tool would tell it about the world by
            # never looking at it.
            await _take_wake_readings(path.parent)
            # This was a look, and on this path nothing else would say so. The
            # counter is what a look budget is spent from and what tells a
            # decision whether an observation has happened since the last one --
            # a campaign that never submits a trial would otherwise watch all day
            # against a spend that stayed at zero.
            _advance_probe_seq(path.parent)
            # Rendered after the count, so the spend the caller reads includes the
            # look it is reading it on.
            pre = _meta_lines(path.with_name("meta.json"))
            notice = f"No campaign ledger at {path} yet; round 0 has not been submitted."
            return "\n".join([notice, *pre, *_reading_series_lines(path.parent)])

        led = Ledger(path)
        # Reconcile against the remote before reporting: submit only records a
        # trial as pending, so without this poll+fetch the ledger never advances
        # and every check would read stale "pending". meta.json (written by
        # ops_submit) says how to reach the host. Best-effort: on any error, fall
        # back to the stored records.
        meta_path = path.with_name("meta.json")
        # Resolved here, above the progress rendering that reads it, and again
        # after the reconcile below. Both are needed and the order is the whole
        # point: the running-trial series is labelled and filtered by this name,
        # so resolving it only afterwards left the series keyed on the empty
        # string. Measured 2026-08-07 on r15 and r16 -- the loop was shown
        # ``, as logged: None:69.89 100:100 220:220`` instead of the evaluation
        # curve, and spent its first fifty minutes recording "no nDCG eval
        # results captured yet" while the job was writing them all along.
        metric = _campaign_metric(meta_path, metric, led.all())
        # Bound here because a campaign with no meta.json never enters the block
        # that builds one, and later readers must not depend on that block having
        # run.
        backend = None
        meta: dict[str, Any] = {}
        progress_lines: list[str] = []
        progress_samples: list[dict] = []
        # Read alongside the reconcile below, from the same backend, because the
        # spend is a measurement and the only place holding it is the host.
        budget_spent: float | None = None
        budget_remaining: float | None = None
        budget_unmeasured: dict = {}
        budget_spend_error: str | None = None
        # Whether the look got counted by the probe below. A campaign with no job
        # backend still looked, and a look budget is spent from that count.
        probe_counted = False
        if meta_path.exists():
            meta = _campaign_meta(meta_path.parent)
            # Before the backend, and not inside its try: a campaign that watches
            # something runs nothing, so it has no job backend at all -- and its
            # readings are the only thing it has. Measured 2026-08-21: a watch
            # campaign was refused for having no command and invented
            # ``echo "condition watch - no trial to run"`` to get past it, because
            # everything downstream assumed a backend could be built.
            await _take_wake_readings(path.parent, meta=meta)
            try:
                from oncall_flow.instrument import log_event

                backend = backend_from_meta(meta)
                try:
                    budget_spent = await backend.spent_minutes()
                    budget_remaining = await backend.remaining_minutes()
                    if hasattr(backend, "unmeasured_spend"):
                        budget_unmeasured = backend.unmeasured_spend() or {}
                except Exception as exc:  # noqa: BLE001 -- an unread spend is reported, not raised
                    # Separate try from the reconcile: a host that cannot answer the
                    # spend must not also cost the trial statuses, and a spend that
                    # silently became 0 is worse than one that says it is unknown.
                    budget_spend_error = type(exc).__name__
                for rec in led.all():
                    if rec.is_terminal or rec.handle is None:
                        continue
                    st = await backend.poll(rec.handle)
                    if st.is_terminal:
                        led.set_result(rec.idem_key, await backend.fetch_result(rec.handle))
                        log_event(path.parent, "trial_terminal_observed", trial=rec.idem_key, status=st.value)
                    else:
                        if st is not rec.status:
                            led.set_status(rec.idem_key, st)
                        # Progress, in the campaign's own terms. The backend's
                        # feed below is whatever the job happens to write; this
                        # is what the campaign said to watch while it runs, and
                        # for the case that burned 152 core-minutes on a
                        # collapsing timestep it is the only thing that would
                        # have shown it.
                        await _take_during_readings(meta, path.parent, backend, rec.idem_key)
                        # The tail of the still-running trial's progress feed. What
                        # the numbers in it mean is the caller's reading, so neither
                        # this line nor the header that precedes it names a verdict:
                        # a phrase like "non-finite = diverged" is a criterion, and
                        # it is exclusive -- it implies finite values are fine, which
                        # points away from any failure whose numbers stay finite. The
                        # CFD line measures exactly such a failure: a phase fraction
                        # of -122 is impossible and every number in the log is a
                        # number. Wording from that line's 7a92c1f.
                        #
                        # The newest line alone is not enough. A job that logs
                        # loss ten times per evaluation makes the newest line an
                        # evaluation reading only about 9% of the time (measured:
                        # 23 of 253 lines), and loss alone does not separate a
                        # good configuration from a bad one. So also surface the
                        # newest line that actually carries the metric, with its
                        # own step, and let the caller see how stale it is.
                        #
                        # Both series as well, oldest first. This used to print
                        # one reading per look, on the reasoning that assembling
                        # readings into a trend is the judgement being measured.
                        # That conflated two things: a series is the operands, a
                        # trend word is the comparison. Withholding the operands
                        # left the loop unable to answer "has it moved since the
                        # last look" at all -- over a 90 minute run it looked
                        # three times, the fetch held 25, 36 and 36 loss samples
                        # and 2, 4 and 4 evaluation samples, one of each was
                        # printed, and it waited every time. Training loss
                        # falling two orders of magnitude while the evaluation
                        # reading does not improve is the signature that decides
                        # this case, and it exists only across the two series.
                        # Wide enough to cover a whole run, because the series is
                        # the point: 40 samples reach back 800 steps on a job that
                        # logs every 20, and a 90 minute run is 4500. One `tail`
                        # over a few hundred JSON lines costs one ssh round trip.
                        samples = await backend.fetch_progress(rec.handle, tail=400)
                        progress_samples.extend(samples)
                        if samples:
                            progress_lines.append(f"  {rec.idem_key}: {_json.dumps(samples[-1], ensure_ascii=False)}")
                            latest_metric = _latest_with_metric(samples[:-1], metric)
                            if latest_metric is not None:
                                progress_lines.append(
                                    f"  {rec.idem_key} (most recent {metric} sample): "
                                    f"{_json.dumps(latest_metric, ensure_ascii=False)}"
                                )
                            for field, cap in (("loss", _LOSS_SERIES_LIMIT), (metric, _METRIC_SERIES_LIMIT)):
                                series = _as_logged_series(samples, field, limit=cap)
                                if series is not None:
                                    progress_lines.append(f"  {rec.idem_key} {field}, as logged: {series}")
                # Each finished round's result in the terms the campaign
                # declared. The numeric ones join that trial's metrics, which is
                # what makes them rankable -- the FEA campaign that optimised
                # max_penetration had it nowhere in the ledger, so nothing could
                # order its rounds by the number it existed to move. (The
                # each_wake readings were taken above, before the backend, since
                # a campaign that watches has no backend to build.)
                for rec in led.all():
                    if rec.is_terminal and rec.result is not None:
                        await _take_after_trial_readings(meta, path.parent, backend, led, rec)
                # Record what was just probed, so a later report's claims about
                # budget, job state and which checkpoints exist can be checked
                # against a reading rather than against nothing. Written here
                # because this is where the probe happens; the loop cannot write it.
                from oncall_flow.state_claims import collect_facts, read_facts, write_facts

                facts = await collect_facts(backend, led.all(), now_ms=int(datetime.now().timestamp() * 1000))
                # Carry the readings this probe saw, and advance a probe counter.
                # A decision tool can then ask "did an observation happen since
                # your last one?" without a clock or a turn boundary: citing a
                # reading is only current if the counter moved.
                # Exactly the readings the series printed, via the same thinning,
                # so the basis check never accepts a number that was not shown.
                seen: list[float] = [
                    float(value) for _, value in _series_points(progress_samples, metric, limit=_METRIC_SERIES_LIMIT)[0]
                ]
                for rec in led.all():
                    if rec.result is not None:
                        value = (rec.result.metrics or {}).get(metric)
                        if isinstance(value, (int, float)) and not isinstance(value, bool):
                            seen.append(float(value))
                facts = _dataclasses.replace(
                    facts,
                    metric_readings={metric: tuple(seen)} if seen else {},
                    probe_seq=read_facts(path.parent).probe_seq + 1,
                )
                write_facts(path.parent, facts)
                probe_counted = True
            except Exception:
                pass
            if not probe_counted:
                # The probe above is what normally counts a look, and it needs a
                # backend. A campaign that watches has none, and its look still
                # happened: without this its look budget never moves and every
                # decision it takes is refused for resting on no new observation.
                _advance_probe_seq(path.parent)

        records = led.all()
        if not records:
            # The campaign's own facts belong on this exit too. It is the turn that
            # decides what to submit, and it is exactly the turn a rule like "check
            # the magnitudes before submitting" exists for -- but with the rules
            # only on the has-trials path it got back one sentence and nothing
            # else. Found on the CFD line's dry run; the same shape as the earlier
            # fix for "no ledger file yet", which left this one standing because a
            # file that exists and holds nothing is a different branch.
            pre = _meta_lines(meta_path)
            notice = f"Campaign ledger {path} is empty (starting up)."
            return "\n".join([notice, *pre, *_reading_series_lines(path.parent)])

        # Resolved after the reconcile above, so a result fetched on this pass can
        # supply the name for a campaign that never declared one.
        metric = _campaign_metric(meta_path, metric, records)

        counts: dict[str, int] = {}
        succeeded = []
        terminal_records = []
        for r in records:
            counts[r.status.value] = counts.get(r.status.value, 0) + 1
            if r.is_terminal and r.result is not None:
                terminal_records.append(r)
            if r.status is JobStatus.SUCCEEDED and r.result and metric in r.result.metrics:
                succeeded.append(r)

        running = any(not r.is_terminal for r in records)
        state = "in progress" if running else "done"
        lines = [
            f"Campaign {state}: {len(records)} trials ({', '.join(f'{k}={v}' for k, v in sorted(counts.items()))}).",
        ]
        # The campaign's compute budget, as a fact and not a derived remainder.
        # It was previously invisible here: the training job writes it once in its
        # opening progress sample and this tool prints only the latest sample, so a
        # loop that woke mid-run saw elapsed time with nothing to measure it
        # against, and had to carry the total from its task text. The subtraction
        # is left to the caller on purpose -- deciding when to look again is the
        # judgement under measurement.
        meta_path = path.with_name("meta.json")
        if meta_path.exists():
            try:
                lines.append(
                    _budget_line(
                        _json.loads(meta_path.read_text(encoding="utf-8")),
                        spent=budget_spent,
                        remaining=budget_remaining,
                        unmeasured=budget_unmeasured,
                        spend_error=budget_spend_error,
                        cdir=meta_path.parent,
                    )
                )
            except (OSError, ValueError):
                pass
            lines.extend(_occupancy_lines(meta_path, records))
        # The starting values this campaign recorded, as a campaign-level fact, next
        # to the budget and in the same shape: operands, never a result. No
        # difference, no "below/above", no ordering -- the comparison is the
        # judgement being measured. It is a campaign line and NOT paired onto each
        # progress sample, because pairing would align the two operands for the
        # caller once per reading, which is the comparison itself.
        #
        # Read from `reference_values` rather than from `expected_baseline`, which
        # the report gate checks. Same number in practice, two separate roles: once
        # a value is printed here, "copied it correctly" stops being evidence about
        # the loop and becomes evidence that the answer was on screen -- so the gate
        # needs its own field to keep its refusal count meaningful.
        if meta_path.exists():
            try:
                refs = _json.loads(meta_path.read_text(encoding="utf-8")).get("reference_values")
            except (OSError, ValueError):
                refs = None
            if isinstance(refs, dict) and refs:
                stated = ", ".join(f"{k} = {v}" for k, v in sorted(refs.items()))
                lines.append(f"Campaign recorded starting value: {stated}")
        # What the campaign declared it watches, as a series. Next to the budget
        # and the starting values because it is the same kind of line: operands,
        # in the campaign's own terms, with the reading left to the caller.
        lines.extend(_reading_series_lines(path.parent))
        # The campaign's operating procedure, verbatim and in the configured order.
        #
        # A wake turn starts cold from disk, so procedure stated in the task text
        # reaches only the first turn and is absent from every turn where a decision
        # gets made. Measured 2026-08-06: a loop woke six times, each time
        # re-deriving the situation from scratch, its basis identical in structure
        # every time -- and the task text had spelled the rule out.
        #
        # Neither other route closes this. A skill must survive an LLM relevance
        # gate whose empty answer is legal (measured: injected on 0 of 2 wake turns,
        # 2 of 9 calls, and the failure is silent). The system prompt is global, so
        # it cannot carry one procedure per domain. Campaign meta is the only place
        # that is per-campaign AND reprinted on every look, which is how the budget
        # and the starting value already travel.
        #
        # Exemplars rather than a longer rule list, because the rule list has been
        # tried: the same 2026-08-06 run had "if the score is clearly below the
        # starting value, do not wait -- change the configuration and rerun" in its
        # task text and did not act, six times. A worked example shows how to read
        # the numbers; another rule competes in a channel that is already saturated.
        # Domain timing knowledge rides inside the reasoning ("this kind of run
        # shows its direction inside the first 20% of steps") rather than being
        # declared.
        #
        # Printed, never applied: no summary, no reordering, no "you should".
        if meta_path.exists():
            try:
                policy = _json.loads(meta_path.read_text(encoding="utf-8")).get("operating_policy")
            except (OSError, ValueError):
                policy = None
            policy_lines = _policy_lines(policy)
            if policy_lines:
                lines.append("Campaign operating policy (as configured):")
                lines.extend(policy_lines)
            else:
                # Absent must not read as satisfied. Same failure shape as a report
                # gate printing Accepted while reading a directory that held nothing
                # to check against.
                lines.append("Campaign operating policy: (none configured)")
        # The campaign's durable "chart": the user's conclusion and mid-campaign
        # instructions live on disk so every role (wake turn, heartbeat, chat)
        # sees the same facts regardless of which conversation they run in.
        over = _concluded_notice(path.parent)
        if over:
            lines.insert(0, over)
        notes = _read_notes(path.parent)
        if notes:
            # The count at the time is what tells a later read whether an
            # instruction has already been acted on: "add two more angles",
            # recorded when the ledger held 3 trials and read now that it holds 5,
            # was probably handled by those two.
            lines.append(
                "Owner's instructions (latest first). The trial count is the "
                "one at the time it was said, so compare it with the ledger "
                "before acting on an old one again:"
            )
            for n in reversed(notes[-5:]):
                who = "owner" if n.get("source") == "owner" else "you"
                at = n.get("trials_at_the_time")
                stamp = f", {at} trial(s) then" if at is not None else ""
                lines.append(f"  [{n.get('ts', '?')}, from {who}{stamp}] {n.get('note', '')}")
        history = _campaign_history(path.parent)
        if history:
            lines.append("This campaign's record so far, oldest first:")
            lines.extend(f"  {h}" for h in history)
        if progress_lines:
            lines.append("Running-trial progress, as logged:")
            lines.extend(progress_lines)
        if terminal_records:
            # Every finished trial, in submission order, with its numbers as
            # reported. Deliberately not ranked and not reduced to a winner.
            #
            # Gated on there being ANY terminal trial, not on there being a
            # successful one. Measured 2026-08-12 on the OpenFOAM divergence leg:
            # a campaign whose only trial had died rendered the single sentence
            # "No successful trial yet." -- no trial name, no status, and nothing
            # about the SIGFPE stack trace the backend had already captured. The
            # loop guessed at the cause, guessed wrong, and rewrote the case's
            # initial conditions; the next job "reached endTime" in 51 seconds
            # with a third of the water. A failure is a reading, and it was the
            # only reading this campaign had.
            #
            # Sorting them and printing "Best so far" answers the question the
            # agent is there to answer -- whether what it just tried is better or
            # worse than what came before -- and it answers it on a single scalar,
            # which is a poor summary of a training run (a final value, a peak and
            # a stable plateau are different things). Listing is strictly more
            # informative than ranking: the ordering is still derivable, the
            # judgement is left where it belongs.
            # What the runs differ from what was asked for. Computed once for the
            # whole campaign, and printed under whichever trials have something to
            # say -- a key that was submitted and then not used is invisible
            # everywhere else, and on 2026-08-13 that invisibility reached a
            # delivered conclusion.
            _eff = (
                _effective_config_lines(
                    getattr(backend, "_run", None),
                    str(meta.get("remote_dir") or ""),
                    [r.idem_key for r in records],
                    meta.get("seed_config") if isinstance(meta.get("seed_config"), dict) else None,
                )
                if meta
                else {}
            )
            lines.append("Finished trials, in submission order:")
            for r in records:
                if not r.is_terminal or r.result is None:
                    continue
                # The config as submitted, from the ledger. The job's own result
                # rarely echoes one and the folded idem_key is not a config: a wake
                # turn reading only the key has to decode "m2p0e6" back to -2.0e6
                # and "nx80_ny12_nz12" back to a mesh, and one arm decoded it wrong
                # (2026-08-17, it reported round 0 as a mesh and load neither of
                # which had run).
                cfg = r.result.output.get("config") or r.config or r.idem_key
                if isinstance(cfg, dict):
                    cfg = " ".join(f"{k}={v}" for k, v in sorted(cfg.items()))
                numbers = " ".join(f"{k}={v}" for k, v in sorted((r.result.metrics or {}).items()))
                note = "" if r.status is JobStatus.SUCCEEDED else f" [{r.status.value}]"
                lines.append(f"  {cfg}{note}  {numbers or 'no metrics reported'}")
                for _line in _eff.get(r.idem_key, []):
                    lines.append(f"      config: {_line}")
                if r.status is not JobStatus.SUCCEEDED:
                    # What the job itself said on the way out, verbatim and
                    # truncated. Backends put a log tail here exactly for this,
                    # and it was never rendered -- so "the job failed" and "the
                    # job failed because the solver hit a floating point
                    # exception" reached the reader as the same sentence.
                    #
                    # Truncated because status output is read on every wake and a
                    # full log tail would push the rest of the campaign out of
                    # view; whoever wants the rest has ops_outputs. Absence is
                    # printed too: a killed process leaves nothing behind, and
                    # that is a different fact from not having looked.
                    reason = (r.result.error or "").strip()
                    if reason:
                        shown = reason[:_FAILURE_REASON_CHARS]
                        more = " ..." if len(reason) > _FAILURE_REASON_CHARS else ""
                        flat = " / ".join(x.strip() for x in shown.splitlines() if x.strip())
                        lines.append(f"      why: {flat}{more}")
                    else:
                        lines.append("      why: no reason captured (the job left no output)")
                # What this run would hand over, named by the backend. Printed
                # verbatim: the ref and the label come from whoever knows the domain,
                # so nothing here assumes a checkpoint, a step, or that a larger
                # number is the better one. Absent for a backend that cannot name
                # one -- a transient run has no best moment inside it.
                deliverable = r.result.deliverable or {}
                ref = str(deliverable.get("ref") or "")
                if ref:
                    label = str(deliverable.get("label") or "")
                    value = deliverable.get("value")
                    tail = f"  {label}={value}" if label and value is not None else ""
                    lines.append(f"    would hand over: {ref}{tail}")
        elif any(r.status is JobStatus.SUCCEEDED for r in records):
            # Trials finished but none reported the metric asked for. Saying "no
            # successful trial yet" here contradicts the count on the first line and
            # reads as "nothing came back", which is the opposite of the truth.
            available = sorted({name for r in records if r.result is not None for name in (r.result.metrics or {})})
            if metric:
                lines.append(
                    f"Trials succeeded but none reported metric '{metric}'. "
                    + (
                        f"Metrics they do report: {', '.join(available)}."
                        if available
                        else "They reported no metrics at all."
                    )
                )
            else:
                # No name asked for, none declared, and the records do not settle it
                # either. Picking one of several would rank the campaign by a number
                # nobody chose, and that reads exactly like a real ranking.
                kind = str(((meta or {}).get("objective") or {}).get("kind") or "")
                if kind in ("complete", "condition"):
                    # It declared a target; it declared no number. Telling this
                    # campaign to set a metric is what produced invented ones --
                    # a case whose task is "run to endTime" has nothing to rank.
                    lines.append(
                        f"This campaign's target is '{kind}', so there is no metric ranking one "
                        f"round above another; whether the work holds up is read from the run "
                        f"itself. "
                        + (
                            f"The trials report: {', '.join(available)}. Pass metric= to read one."
                            if available
                            else "The trials report no metrics at all."
                        )
                    )
                else:
                    lines.append(
                        "This campaign declares no objective, so there is no metric to rank by. "
                        + (
                            f"The trials report: {', '.join(available)}. Pass metric= to read one of them, "
                            if available
                            else "The trials report no metrics at all. "
                        )
                        + "or set objective {metric, direction} on the campaign so every round reads the same one."
                    )
        else:
            lines.append("No successful trial yet.")
        lines.extend(_case_isolation_lines(records, bool((meta or {}).get("staged_case"))))

        log_path = path.with_name("run.log")
        if log_path.exists():
            tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]
            if tail:
                lines.append("Recent log:\n  " + "\n  ".join(tail))
        rendered = "\n".join(lines)
        # Record every number this probe actually put in front of the caller, so a
        # basis may cite any of them -- the configuration being run, step counts,
        # elapsed time, the budget -- not only the metric readings. Taken from the
        # rendered text, because that is precisely what was shown: a number that
        # never appeared here is still refused.
        #
        # Written here, after the text exists, and NOT next to the earlier facts
        # write: `lines` is built further down, so computing this there referenced a
        # name that did not exist yet and the surrounding except swallowed it,
        # leaving the field silently empty. Measured 2026-08-06.
        try:
            import dataclasses as _dc

            from oncall_flow.state_claims import BASIS_NUMBER_RE, read_facts, write_facts

            current = read_facts(path.parent)
            write_facts(
                path.parent,
                _dc.replace(
                    current,
                    shown_values=tuple(float(m.group()) for m in BASIS_NUMBER_RE.finditer(rendered)),
                ),
            )
        except Exception:
            pass
        return rendered


def _latest_with_metric(samples: list[dict], metric: str) -> dict | None:
    """The newest sample carrying a numeric value for metric, or None.

    Matches on the key containing the metric name because the job names its
    own fields (a metric asked for as "ndcg" arrives as "eval_ndcg").
    """
    for sample in reversed(samples):
        for key, value in sample.items():
            if metric in key and isinstance(value, (int, float)) and not isinstance(value, bool):
                return sample
    return None


_METRIC_SERIES_LIMIT = 24
_LOSS_SERIES_LIMIT = 12


def _series_points(samples: list[dict], field: str, *, limit: int) -> tuple[list[tuple], bool]:
    """``(step, value)`` pairs carrying field, oldest first, thinned to limit.

    Thinning keeps the first and the last and spaces the rest evenly. Taking the
    newest limit instead would cover only the tail: a job logging every 20 steps
    puts 12 consecutive readings inside 240 steps, one noise band, while the
    change worth seeing spans thousands of steps.

    Shared with the recorded facts on purpose. The basis check accepts a decision
    whose cited number is among the readings this probe recorded, so recording
    more than was printed would accept a number the loop was never shown.
    """
    points = []
    for sample in samples:
        step = sample.get("step")
        for key, value in sample.items():
            if field in key and isinstance(value, (int, float)) and not isinstance(value, bool):
                points.append((step, value))
                break
    if len(points) <= limit:
        return points, False
    spacing = (len(points) - 1) / (limit - 1)
    return [points[round(i * spacing)] for i in range(limit)], True


def _as_logged_series(samples: list[dict], field: str, *, limit: int = _LOSS_SERIES_LIMIT) -> str | None:
    """The values of field across samples as ``step:value`` pairs, oldest first.

    Chronological and unreduced. Not sorted by value, no marked best, no
    difference against anything, no word for the direction: those are the
    comparison the loop is scored on. The order they were logged in is not a
    comparison, and without it "below the starting value" is available on every
    look while "still not moving after another 600 steps" is available on none.

    Returns None rather than an empty string when nothing carries the field, so
    a field the job never logs gets no line at all -- an empty series reads as
    "measured, and there is nothing there".
    """
    points, sampled = _series_points(samples, field, limit=limit)
    if not points:
        return None
    line = " ".join(f"{step}:{value}" for step, value in points)
    return f"{line} (sampled)" if sampled else line


# The basis gate (_BASIS_HELP / _basis_refusal / _record_basis) lives in
# oncall_flow.policy since part 2a -- the whole resubmit/kill chain is one
# mechanism layer -- and is imported at the top of this module.


class OpsSubmitTool(_OpsScheduler):
    """Submit one round of trials to a remote host and schedule a self-wake to decide the next.

    This is the agent-in-the-loop primitive: the agent (not a baked-in proposer)
    picks the config(s), submits them as detached jobs, and -- because job runtime
    ranges from seconds to days -- estimates when to check back and schedules a
    one-shot wake at that time. On that wake the agent reads the ledger and decides
    the next round: ops_submit again, ops_check_later to wait more, ops_finish to
    finish, or ops_ask_owner when the decision is genuinely the owner's.
    ``max_rounds`` is the runaway backstop.
    """

    timeout_seconds = 120.0

    @property
    def name(self) -> str:
        return "ops_submit"

    @property
    def description(self) -> str:
        return (
            "Run a long computation on a remote machine -- ONE ROUND AT A TIME, steering it "
            "yourself. The work is whatever the owner wants run and watched on a host: a solver "
            "case (CFD, FEA, any simulation), a training or fine-tuning run, a parameter sweep. "
            'Use this for any such request, however it is worded: "run this OpenFOAM case on '
            'host X", "compute the limit load with CalculiX on that box", "fine-tune a model '
            'on host X", "run this config and watch it", "tune these hyperparameters". What '
            "makes it this tool's job is the shape -- a remote host, a long run, a result worth "
            "waiting for -- not the field it comes from. DO NOT do it yourself with exec or ssh: routing the job "
            "through this tool is what makes its compute budget enforceable, its state survive a "
            "restart, and its results wake you when they are due.\n"
            "The machine, the case, how a trial starts, the budget and the starting config "
            "all belong to the campaign's declaration, not to this call: supply only what "
            "THIS round does. Read it with ops_tune_status, and ops_campaigns for which "
            "exist. A campaign not declared yet needs ops_declare first, which runs "
            "nothing.\n"
            "Each round YOU choose the config(s) and submit them: start small -- one config, or a "
            "few only if the host has the capacity to run them at once, and NEVER a full grid -- "
            "because the whole point is to read each round's results "
            "and choose the next configs from them. Because a job may take seconds or days, estimate "
            "eta_seconds -- when to look next, NOT when the job will finish -- and this tool wakes you then. On that wake, "
            "read the ledger with ops_tune_status and decide: ops_submit the next config(s) "
            "(increment round), ops_check_later if still running, ops_finish to end it and hand the "
            "result back, or ops_ask_owner if the decision is genuinely the owner's. Every branch "
            "names the tool that performs it, because a wake turn is a cold start and a branch with "
            "no tool behind it is one the loop can only act out in prose. State is durable in the "
            "ledger, so the campaign resumes across a restart.\n"
            "DO NOT run the experiment yourself: do not reproduce the computation locally with exec, "
            "and do not pull a domain skill (use_skill) to compute the result in this process. The "
            "job must run on the remote host through this tool. Reading a skill or reference to help "
            "CHOOSE configs is fine; executing the experiment anywhere but here is not."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        # Five, and it stays five. Everything a campaign is set up with belongs to
        # ops_declare: those fields are read once at round 0 and overridden from the
        # campaign on every round after, so carrying them here made sixteen of
        # twenty parameters dead weight on every call but the first -- and left an
        # agent choosing which of them this round even cared about.
        return {
            "type": "object",
            "properties": {
                "campaign": {
                    "type": "string",
                    "description": "The campaign this round belongs to, as declared with "
                    "ops_declare. Omit only when the ops home holds exactly one.",
                },
                "round": {
                    "type": "integer",
                    "description": "This round index. Start at 0 and increment each call.",
                },
                "configs": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": 'Config dict(s) to run this round, e.g. [{"deltaT": 1e-4}]. '
                    "One is usual. Several only if the machine can run them at "
                    "once, and NEVER a full grid -- a sweep submitted in one round "
                    "spends the budget before any of it has been read.",
                },
                "basis": {
                    "type": "string",
                    "description": "What you read that supports this round -- cite the actual "
                    "numbers from the last one. Required from round 1 on. "
                    "Round 0 runs the declared starting point, so it needs one "
                    "only if it departs from that.",
                },
                "eta_seconds": {
                    "type": "integer",
                    "description": "When to look next, in seconds of wall-clock time. Not an "
                    "estimate of the run: it is how long you choose to leave it "
                    "before deciding anything. The pace is yours.",
                },
                "action": {
                    "type": "string",
                    "description": "Which of the campaign's declared actions to take, when this "
                    "round is an ACT rather than a run -- placing the order the "
                    "campaign was watching for, say. Omit to submit trials, which "
                    "is the usual case. The name has to be in the campaign's action "
                    "table: that table is the list of what may be done, and it "
                    "cannot be added to once results exist. An action goes here "
                    "rather than through exec because only this path records it, "
                    "and a wake that comes back after your turn is gone has nothing "
                    "but that record to tell it the thing was already done.",
                },
                "values": {
                    "type": "object",
                    "description": "This call's values for the {placeholders} in that action's "
                    'command, e.g. {"qty": 3}. Every placeholder must get one.',
                },
            },
            "required": ["configs", "eta_seconds"],
        }

    async def execute(
        self,
        eta_seconds: int,
        objective: str = "",
        connection: str = "",
        host: str = "",
        configs: list[dict] | None = None,
        ledger: str = "",
        round: int = 0,
        basis: str = "",
        # Only reached by a campaign whose declaration names no cap. Matches
        # ops_declare's default: a runaway stop, not a round count anyone chose.
        max_rounds: int = 50,
        action: str = "",
        values: dict | None = None,
        port: int = 22,
        key: str = "~/.ssh/id_rsa",
        remote_dir: str = "/root/raven-ops",
        image: str = "python:3.12-slim",
        app_dir: str = "benchmarks/ops_bm25",
        # How to start the owner's own case, as opposed to the container trial this
        # tool was born running. All three are fixed at round 0 and read from the
        # campaign afterwards, so a later round that omits them keeps running the
        # same thing.
        command: str = "",
        backend: str = "",
        staged_case: str = "",
        # metric + goal are recorded on the campaign at round 0 as its objective, and
        # every later reader takes the metric name from there. Neither is guessed:
        # this backend is shared with every domain that runs a plain command, and the
        # direction cannot be inferred from the name -- for a loss the best value is
        # the smallest, so a guess would name the worst checkpoint as confidently as
        # the best one.
        metric: str = "",
        goal: str = "",
        # Empty, not "ops": a literal default made an unnamed submit anchor to a
        # campaign called "ops" while every later call used the real name, so one
        # experiment ended up with two drivers -- observed on the cfd line as
        # ops:ops:r1 alongside ops:cfd-transient:recheck. Empty means "the one
        # campaign under the ops home", and ambiguity is reported, never guessed.
        campaign: str = "",
        **kwargs: Any,
    ) -> str:
        from oncall_flow.backend import JobBackendError, JobResult, JobSpec, JobStatus
        from oncall_flow.backends import backend_from_meta, prepare_from_meta
        from oncall_flow.ledger import Ledger
        from oncall_flow.proposer import config_key

        if round >= max_rounds:
            return (
                f"Reached max_rounds ({max_rounds}) for campaign '{campaign}'. Not submitting more. "
                f"Read ops_tune_status(ledger='{ledger}') and report the best config to the user, or "
                f"raise max_rounds only if the user asks to keep going."
            )
        if not campaign:
            try:
                campaign = _resolve_campaign_dir("", ledger, self._session_key, self._task).name
            except ValueError as exc:
                return f"Cannot tell which campaign to submit to: {exc}"
        # A watch that has spent its own budget cannot buy a round with what is
        # left of it either. Checked here rather than in the backend, which meters
        # machine time and would find this campaign's allowance untouched.
        try:
            spent = _watch_budget_spent(_resolve_campaign_dir(campaign, ledger, "", ""), attempting="another round")
        except ValueError:
            spent = ""
        if spent:
            return spent
        # An act, not a round. Handled before any of the trial machinery: none of
        # it applies -- there is no config to stage, no job to poll, no wake to
        # arrange, because the thing is over when the command returns. What it
        # does share with a trial is the ledger and the idempotency key, and that
        # is the whole reason it comes through here rather than through exec.
        if action and action != ACTION_RUN:
            return await self._take_action(campaign, ledger, action, values or {}, basis, eta_seconds)
        # This window is working on this campaign. Written here rather than at the
        # end because every later unnamed call in this window resolves through it,
        # including the ones this round's own wake will make.
        from oncall_flow.window import bind_window, remember_task

        bind_window(
            _ops_home(), self._session_key, _resolve_campaign_dir(campaign, ledger, "", "").name, pid=os.getpid()
        )
        # And which statement it was started for, so a window opened after this
        # one is gone can find it again from the statement alone.
        remember_task(_ops_home() / _slug(campaign), self._task)
        if not configs:
            # Round zero's config belongs to whoever set the campaign up -- the code
            # and its defaults are the upstream deliverable -- not to the person
            # asking for the work. Read it from the campaign, and say plainly when
            # there is none: a submit with nothing to run must not look like it ran.
            seed = None
            try:
                import json as _seed_json

                seed_meta = _seed_json.loads(
                    (_resolve_campaign_dir(campaign, ledger) / "meta.json").read_text(encoding="utf-8")
                )
                seed = seed_meta.get("seed_config")
            except (OSError, ValueError):
                seed = None
            if isinstance(seed, dict) and seed:
                configs = [seed]
            elif isinstance(seed, (list, tuple)) and seed:
                configs = [dict(c) for c in seed if isinstance(c, dict)]
            if not configs:
                return "No configs given and the campaign has no seed_config; nothing to submit."

        # Tolerate the agent packing the port into host ("1.2.3.4:64106") -- the
        # prompt phrasing "host (SSH port N)" invites it. Split it so the SSH runner
        # gets a bare host instead of an unresolvable "host:port" name.
        def _split_hostport(h: str, p: int) -> tuple[str, int]:
            if isinstance(h, str) and ":" in h:
                head, _, tail = h.rpartition(":")
                if head and tail.isdigit():
                    return head, int(tail)
            return h, p

        host, port = _split_hostport(host, port)

        # Anchor a relative/empty ledger to a stable absolute path keyed by campaign:
        # the path travels through the wake message to later rounds and to
        # ops_tune_status, and a relative path would resolve against a cwd that can
        # differ across turns (especially a cron-triggered wake).
        led = Path(ledger).expanduser() if ledger else Path()
        if not led.is_absolute():
            led = _ops_home() / _slug(campaign) / "ledger.json"
        ledger = str(led)

        # The user's conclusion is durable state every role must respect: once a
        # campaign is concluded, no turn -- a late wake, the heartbeat, anyone --
        # may submit more rounds, regardless of what its own context says.
        if (led.parent / "concluded.json").exists():
            # Say that the *name* is taken, not that permission was withdrawn.
            # "CONCLUDED by the user; not submitting" reads as a state or
            # permission problem, and an agent reading it that way reaches for
            # concluded.json with edit_file, or edits meta.json -- the
            # apparatus-editing behaviour measured 2026-08-12, triggered by our
            # own wording. Task statements no longer carry a campaign name, so the
            # agent invents one and two similar tasks colliding is not unlikely.
            when = "?"
            try:
                import json as _taken_json

                when = _taken_json.loads((led.parent / "concluded.json").read_text(encoding="utf-8")).get(
                    "concluded_at", "?"
                )
            except (OSError, ValueError):
                pass
            return (
                f"REFUSED: the name '{campaign}' already belongs to a campaign that finished "
                f"at {when}, and its record is what a report was filed against.\n"
                f"If this is that same work, there is nothing left to submit -- read it with "
                f"ops_tune_status and report the recorded best.\n"
                f"If this is different work, submit it under a different name. Do not delete "
                f"concluded.json and do not edit meta.json to free the name up: that would "
                f"overwrite the finished campaign's record. Nothing was submitted."
            )

        # The declaration itself, before anything else is asked of this round. It is
        # a local file, so no host is involved, and a campaign whose meta has moved
        # is not one whose basis is worth grading. Measured 2026-08-12: an arm
        # rewrote remote_dir/staged_case/command because the task text named a
        # different case path, and the job then ran out of an undeclared directory
        # while the budget guard watched the declared one.
        from oncall_flow.apparatus import load_baseline as _load_baseline
        from oncall_flow.apparatus import meta_sha as _meta_sha
        from oncall_flow.instrument import log_event

        _base = _load_baseline(led.parent)
        if _base and _base.get("meta_sha") not in ("-", None) and _meta_sha(led.parent) != _base["meta_sha"]:
            log_event(led.parent, "apparatus_refused", reason="meta_changed", round=round)
            return (
                "REFUSED: this campaign's meta.json has changed since its first submit. "
                "That file is the apparatus' declaration -- what the target is, what the budget is, "
                "which host and which case the job runs on -- so changing it changes what this "
                "experiment measures, and where it runs.\n"
                "If you believe something in it is wrong, say so with ops_ask_owner. To carry on, "
                "restore it to what it was. Nothing was submitted."
            )

        # Round 0 runs the starting point the campaign declared. This is the basis
        # rule one round earlier: from round 1 on a config change has to cite a
        # reading, and at round 0 no reading exists yet, so there is nothing a
        # change could be grounded in. Measured 2026-08-13 -- both CFD legs read
        # the declared seed off the status line and submitted something else (one
        # dropped the declared key entirely, one halved the declared value), and
        # each then ran a question nobody had asked.
        #
        # A ledger that already holds trials is the exception, and it is the same
        # premise read the other way: their scores and spend ARE a reading, so the
        # incoming shift of a handover is free to act on them. Holding it to the
        # declared seed there would order it to re-run a configuration whose result
        # is already in the ledger.
        if round == 0 and not Ledger(led).all():
            try:
                import json as _gate_json

                _seed_declared = _gate_json.loads((led.parent / "meta.json").read_text(encoding="utf-8")).get(
                    "seed_config"
                )
            except (OSError, ValueError):
                _seed_declared = None
            _departures = _seed_departures(_seed_declared, configs)
            if _departures and not (basis or "").strip():
                # A value handed over can be wrong, and a first run that spends an
                # hour on a viscosity nobody believes is a poor way to find that
                # out. So departing at round 0 is allowed -- and has to be said out
                # loud, because the only thing that separates correcting a value
                # from making the first run cheaper is why it was done, and no
                # check can read that from the numbers.
                #
                # Measured 2026-08-17, six campaigns: five departed at round 0, and
                # four of the five were after a cheaper start -- a coarser mesh, six
                # keys left to the script's defaults, six batches instead of the
                # corpus. The fifth raised a length limit it had reason to think
                # truncated the data. Refusing them all cost 7 to 27 seconds each;
                # allowing them silently would have cost the declared baseline in
                # four campaigns out of five.
                log_event(led.parent, "seed_refused", round=round, changes=_departures)
                return (
                    "REFUSED: this changes the starting point the campaign declared, and gives "
                    "no reason.\n"
                    + "\n".join(f"  {d}" for d in _departures)
                    + "\nThat config is the handover -- the code and its defaults as they were "
                    "given to you. You may correct a value you can show is wrong: a water "
                    "viscosity of 1e-3, a path that does not exist, a unit that is off by a "
                    "thousand. Pass 'basis' saying which value is wrong and how you know.\n"
                    "What is NOT a reason is that the declared start is expensive or awkward: a "
                    "smaller mesh, fewer batches, a shorter run, leaving keys to the job's "
                    "defaults. Those change the question rather than answer it, and the reading "
                    "they produce is about a different problem than the one you were given.\n"
                    "Submit it as declared (or with no configs at all, which runs it) if you "
                    "have no such reason. If you suspect the declared start is wrong but cannot "
                    "yet show it, say so with ops_ask_owner rather than changing it quietly. "
                    "Nothing was submitted."
                )
            if _departures:
                # Allowed, and recorded with the reason: what it changed, and what it
                # said was wrong with the declared value. Both halves are needed --
                # the diff alone cannot tell a correction from a shortcut.
                log_event(led.parent, "seed_departed", round=round, changes=_departures, basis=(basis or "")[:400])

        # A question the loop itself called blocking, still unanswered. It said
        # nothing worth doing was left until it heard back; submitting now
        # contradicts that, and spends compute on a branch the answer may make
        # wrong. Looking is untouched -- exec on the machine, the logs, the case are all
        # still open, and writing down what was found is how the wait gets used.
        from oncall_flow.escalation import blocking_question_open

        _blocked = blocking_question_open(led.parent)
        if _blocked:
            log_event(led.parent, "submit_refused", reason="blocking_question_open")
            return (
                "REFUSED: you asked the owner this and called it blocking, and it is still "
                f"unanswered:\n  {_blocked[:300]}\n"
                "Blocking was your own reading that nothing worth doing was left until you "
                "heard back -- so spending compute now contradicts it. Reading logs, reading "
                "the case and writing down what you find cost nothing and are still open to "
                "you. If you have since found something that makes the answer unnecessary, "
                "record it with ops_note and submit again. Nothing was submitted."
            )

        if round >= 1:
            # From round 1 on there has been something to observe, so the same basis
            # requirement applies here as to waiting and killing. Round 0 is exempt
            # because nothing has been observed yet -- that is the absence of the
            # thing being cited, not a concession.
            #
            # After the runaway backstop and the concluded check: those two are "you
            # cannot submit at all", and a refusal should name the more fundamental
            # reason, not a missing justification for an action that was not going to
            # happen anyway.
            refusal = _basis_refusal(led.parent, basis, "submit")
            if refusal:
                return refusal

        # Persist how to reach the host next to the ledger so ops_tune_status can
        # reconcile the ledger against the remote (poll + fetch) without the agent
        # re-supplying connection details on every check.
        import json as _json

        led.parent.mkdir(parents=True, exist_ok=True)
        meta_file = led.parent / "meta.json"
        meta: dict[str, Any] = {
            "host": host,
            "port": port,
            "key": key,
            "remote_dir": remote_dir,
            "image": image,
        }
        if connection:
            from oncall_flow.connections import describe as _conn_describe
            from oncall_flow.connections import get as _conn_get

            if _conn_get(connection) is None:
                # Refused here rather than at the staging step: a campaign written
                # with an id nothing resolves has no address, and every round after
                # it fails for a reason that looks like the machine is down.
                return (
                    f"REFUSED: there is no connection with id {connection!r}, so nothing was "
                    f"written or submitted.\n{_conn_describe()}"
                )
            # Named by the caller from ops_connections. Kept instead of an address:
            # the address is the connection's business, and a campaign that stores
            # one goes stale the moment a port changes.
            meta["connection"] = connection
            for _t in ("host", "port", "key"):
                meta.pop(_t, None)
        if command:
            meta["command"] = command
            # A command and a container are two different ways to start a trial, and
            # only one of them runs what the owner installed. Inferred rather than
            # asked for again: nothing on the docker path reads "command", so a call
            # that carries one has already said which it means.
            meta["backend"] = backend or "process"
        elif backend:
            meta["backend"] = backend
        if staged_case:
            meta["staged_case"] = staged_case
        if not meta_file.exists() and not connection and not str(host).strip():
            # Nothing here says what this campaign is. Creating one from a round's
            # arguments is what ops_declare is for, and it refuses in one place
            # rather than leaving the checks scattered through a submit.
            return (
                f"REFUSED: there is no campaign called '{campaign}' -- it has not been declared, "
                f"so there is nothing saying which machine it runs on, how a trial starts, or "
                f"what it is optimising.\n"
                f"Declare it first with ops_declare, then submit round 0 against it. "
                f"ops_campaigns lists the ones that do exist, if you meant one of those. "
                f"Nothing was submitted."
            )
        if metric and goal in ("max", "min"):
            meta["objective"] = {"metric": metric, "direction": goal}
        if meta_file.exists():
            # Campaign state wins over this round's arguments: the connection (and
            # the backend the campaign runs on) is fixed at round 0, so a later
            # round that omits port/key -- agents routinely drop them -- cannot
            # fall back to a wrong default such as SSH port 22.
            try:
                stored = _json.loads(meta_file.read_text(encoding="utf-8"))
                meta.update(stored)
                if stored.get("connection"):
                    # A campaign that names a connection keeps no address of its
                    # own, so the merge above leaves this round's arguments in
                    # place -- including a port that defaulted to 22 because the
                    # caller had no reason to pass one. Measured 2026-08-17: both
                    # FEA campaigns declared a connection on port 64106 and every
                    # submit was refused with "connect to host ... port 22".
                    #
                    # Cleared and then filled from the connection, rather than
                    # only cleared: the rest of this call reads meta["host"] to
                    # say where the work went, and a meta with no address at all
                    # made that a KeyError the agent could only read as "the tool
                    # wants a host" -- measured minutes later, when it passed one
                    # by hand and then edited the campaign's own declaration.
                    from oncall_flow.connections import resolve_into as _conn_resolve

                    for _t in ("host", "port", "key", "user"):
                        meta.pop(_t, None)
                    meta = _conn_resolve(meta)
                else:
                    meta["host"], meta["port"] = _split_hostport(meta.get("host", host), int(meta.get("port", port)))
                if "objective" not in stored and meta.get("objective"):
                    # A campaign whose meta was written by hand before the first
                    # submit -- how every experiment here is set up -- reached this
                    # branch, so the objective assembled above lived in memory for
                    # one call and was never on disk. Only this key is added back:
                    # rewriting the merged dict would also push this round's
                    # connection arguments into a file that deliberately outranks
                    # them.
                    stored["objective"] = meta["objective"]
                    meta_file.write_text(_json.dumps(stored), encoding="utf-8")
            except (OSError, ValueError):
                pass
        else:
            meta_file.write_text(_json.dumps(meta), encoding="utf-8")

        # The campaign's own words for what it is doing. Declared once and read
        # from there on every round, because the wake message that carries it is
        # written by a turn that no longer has the task statement in front of it.
        objective = objective or str(meta.get("objective_words") or "") or metric
        if round == 0:
            prepare_from_meta(meta, app_dir=app_dir)

        backend = backend_from_meta(meta)

        # The apparatus, checked against what this campaign was set up with. The
        # first submit is where the baseline is taken -- it is the last moment the
        # setup is known to be as the operator left it.
        #
        # Two halves, deliberately unequal. meta.json is the campaign's own
        # declaration and an agent editing it is out of role in any domain, so a
        # changed meta refuses; the case is the agent's to edit and its changes are
        # only reported. Measured 2026-08-12: one arm rewrote remote_dir/staged_case
        # /command because the task text named a different path than the meta did,
        # and the job then ran out of an undeclared directory while a budget guard
        # watched the declared one. Another arm rewrote the case's initial
        # conditions and solved a different problem in 51 seconds. Neither concealed
        # anything -- both wrote a reason -- so recording is not the gap; noticing is.
        from oncall_flow.apparatus import baseline_of, compare, load_baseline, save_baseline

        cdir = led.parent
        _runner = getattr(backend, "_run", None)
        try:
            _now = baseline_of(cdir, _runner)
            _drift = compare(load_baseline(cdir), _now)
        except Exception:  # noqa: BLE001 -- a fingerprint failure must not block work
            _now, _drift = None, None
        _drift_lines: list[str] = []
        if _drift is not None and not _drift.is_clean:
            _drift_lines = _drift.describe()
            log_event(cdir, "apparatus_drift", round=round, changes=_drift_lines)
        if _now is not None and load_baseline(cdir) is None:
            save_baseline(cdir, _now)

        ledger_obj = Ledger(ledger)
        submitted = []
        refused = []
        # The machine, checked across every campaign under this ops home before
        # anything is bought. Each campaign scheduling from its own ledger alone
        # is how two of them stacked trials on one device (2026-08-31, CUDA OOM)
        # -- the contention lives between the ledgers, so it is read there.
        from oncall_flow import connections as _occ_conns
        from oncall_flow.occupancy import (
            admission_refusal,
            capacity_refusal,
            duplicate_refusal,
            free_device_ids,
            reservation_lock,
        )

        # The home scanned is this campaign's own parent, not the global ops
        # home: the two are the same for every campaign that lives where
        # _resolve_campaign_dir puts them, and the campaign's actual siblings
        # are the ones it can collide with.
        _occ_home = led.parent.parent
        _conn_id = str(meta.get("connection") or "")
        # A trial is its config AND the apparatus it ran against. Named from the
        # config alone, "same config, edited case" reused one job directory: the
        # restart branch keeps system/ so the edits never reached the job, the
        # previous round's log was overwritten in place, and its spend vanished
        # with the directory the backend measures by. Ten edits, two identical
        # eleven-minute failures, 8.8% of the campaign's spend off the books.
        #
        # This does not weaken idempotency, it corrects it: the same config
        # against a changed case is a different run. A campaign with no staged
        # case has no digest and its names are unchanged.
        _apparatus = ""
        if _now and _now.get("case"):
            import hashlib as _ap_hash

            _apparatus = _ap_hash.sha1(
                "\n".join(f"{k}:{v}" for k, v in sorted(_now["case"].items())).encode()
            ).hexdigest()[:8]
        # Two passes. The first, under the ops-home lock, reads every sibling
        # ledger, decides what each config holds, and writes this campaign's
        # records -- check and reservation as one step, so two submits cannot
        # both see a free machine (or the same free device ids) between one's
        # read and the other's write. The second hands the reserved jobs to the
        # backend outside the lock; the fresh handle-less records left by the
        # first pass are what the gate counts while those calls are in flight.
        from oncall_flow.proposer import legacy_config_key

        _admitted: list[tuple[int, dict, str]] = []
        # The one look at the machine itself, taken BEFORE the lock: which of its
        # devices a process outside the ledger holds right now. The lock below is a
        # synchronous file lock, and an await inside it hands the event loop to a
        # sibling submit that then blocks the whole thread acquiring the same lock
        # -- the first can never resume to release it (reproduced in review,
        # 2026-09-07). So nothing under the lock awaits; the probe's answer is a
        # snapshot the lock-held arithmetic reads, and the launcher's own check at
        # start stays the last guard against a card taken in between.
        _foreign: dict[str, int] = {}
        if _conn_id:
            _pre_row = _occ_conns.get(_conn_id) or {}
            if _occ_conns.resource_unit(_pre_row) == "gpus":
                _all_ids = [str(i) for i in range(_occ_conns.capacity(_pre_row).get("gpus", 0))]
                _probe = getattr(backend, "busy_devices", None)
                if _all_ids and callable(_probe):
                    try:
                        _foreign = dict(await _probe(_all_ids) or {})
                    except Exception:  # noqa: BLE001 -- a probe that cannot answer must not refuse
                        _foreign = {}
        with reservation_lock(_occ_home):
            # What each config will hold, decided before anything is recorded: the
            # gate admits by free devices or cores (owner's rulings 2026-09-03), and
            # the ids it hands out ride to the launcher, which exports them. A row
            # that hands out nothing countable keeps the job-count gate.
            _held_by_cfg: list[dict[str, Any] | None] = [None] * len(configs)
            _labels_by_cfg: list[dict[str, str]] = [{} for _ in configs]
            if _conn_id:
                _row = _occ_conns.get(_conn_id) or {}
                _unit = _occ_conns.resource_unit(_row)
                if _unit:
                    _capacity = _occ_conns.capacity(_row)
                    _requests, _mem_requests = _resource_requests(meta, configs, _unit)
                    _cap = admission_refusal(
                        _occ_home,
                        _conn_id,
                        unit=_unit,
                        capacity=_capacity[_unit],
                        requests=_requests,
                        memory_capacity_gb=_capacity.get("memory_gb"),
                        memory_requests=_mem_requests,
                        display=_occ_conns.display_name(_conn_id),
                    )
                    if _cap:
                        log_event(cdir, "capacity_refused", round=round, connection=_conn_id)
                        return _cap
                    _free_ids = free_device_ids(_occ_home, _conn_id, _capacity[_unit]) if _unit == "gpus" else []
                    if _free_ids and _foreign:
                        # The ledger's free ids, minus the ones a person outside the
                        # ledger is using right now (probed above, before the lock):
                        # a shared machine is the ordinary case, and the launcher's
                        # own check can only refuse the card it was given --
                        # skipping here is what lets the idle higher card be chosen.
                        _busy = {d: m for d, m in _foreign.items() if d in _free_ids}
                        if _busy:
                            _free_ids = [d for d in _free_ids if d not in _busy]
                            if len(_free_ids) < sum(_requests):
                                _held_txt = ", ".join(f"device {d} ({m} MiB)" for d, m in sorted(_busy.items()))
                                log_event(
                                    cdir, "capacity_refused", round=round, connection=_conn_id, foreign=list(_busy)
                                )
                                return (
                                    f"REFUSED: {_held_txt} is/are held by a process outside the ledger -- the machine is "
                                    f"shared -- and only {len(_free_ids)} device(s) remain free for the {sum(_requests)} this "
                                    f"round asks for. Submit what fits, or wait and try again; nothing was submitted."
                                )
                    for _i, (_n, _mem) in enumerate(zip(_requests, _mem_requests)):
                        _held: dict[str, Any] = {_unit: _n}
                        if _mem:
                            _held["memory_gb"] = _mem
                        if _unit == "gpus":
                            _ids, _free_ids = _free_ids[:_n], _free_ids[_n:]
                            _held["device_ids"] = _ids
                            _labels_by_cfg[_i]["device_ids"] = ",".join(_ids)
                        _labels_by_cfg[_i]["width"] = str(_n)
                        _held_by_cfg[_i] = _held
                else:
                    _cap = capacity_refusal(
                        _occ_home,
                        _conn_id,
                        incoming=len(configs),
                        concurrency=_row.get("concurrency"),
                        display=_occ_conns.display_name(_conn_id),
                    )
                    if _cap:
                        log_event(cdir, "capacity_refused", round=round, connection=_conn_id)
                        return _cap
            for _i, cfg in enumerate(configs):
                key_id = config_key(cfg)
                if _apparatus:
                    key_id = f"{key_id}__{_apparatus}"
                # A campaign started before the key was shortened holds its records
                # under the old spelling; recomputing would read them as trials that
                # never ran and spend the compute again.
                _known = {r.idem_key for r in ledger_obj.all()}
                if key_id not in _known and legacy_config_key(cfg) in _known:
                    key_id = legacy_config_key(cfg)
                # The same trial under another live campaign's name is the same
                # measurement bought twice (2026-08-31: two campaigns, one idem_key,
                # double spend). Checked before the record so a refused duplicate
                # leaves no handle-less orphan in this ledger.
                _dup = duplicate_refusal(_occ_home, led.parent, key_id)
                if _dup:
                    refused.append(_dup)
                    continue
                ledger_obj.record(key_id, campaign=campaign, config=dict(cfg), resources_held=_held_by_cfg[_i])
                _admitted.append((_i, cfg, key_id))
        for _i, cfg, key_id in _admitted:
            try:
                handle = await backend.submit(
                    JobSpec(cfg, idem_key=key_id, labels={"campaign": campaign, **_labels_by_cfg[_i]})
                )
            except JobBackendError as exc:
                # Recording before submitting is deliberate -- a crash in between
                # leaves an orphan a later reconcile can find. But a submit that
                # was *refused* has no job to find, and a record with no handle is
                # skipped by reconciliation, so it would read as pending forever
                # and keep the agent waiting on something that never started.
                ledger_obj.set_result(key_id, JobResult(JobStatus.FAILED, error=str(exc)[:400]))
                refused.append(f"{key_id}: {exc}")
                continue
            ledger_obj.set_handle(key_id, handle)
            submitted.append(key_id)

        if not submitted:
            detail = "\n".join(refused) or "no reason reported"
            return (
                f"No jobs were submitted for campaign '{campaign}' round {round}; the backend refused:\n"
                f"{detail}\nNothing is running and nothing is scheduled. Read "
                f"ops_tune_status(ledger='{ledger}') and report the best result so far."
            )

        if _drift_lines:
            # Said on the way out, not only written to the trail: the turn that
            # made the edit is the one that can still explain or undo it.
            submitted_note = "\n".join(f"  - {x}" for x in _drift_lines)
        else:
            submitted_note = ""
        # The failure branch is split by whether the text states a cause, not by
        # whether the run failed. Measured 2026-08-14: a trial killed from outside
        # left no result.json, the backend filled `error` with `tail -30 job.log`,
        # and for a training script that tail is checkpoint-writing progress bars.
        # The loop read the bars as the crash site, called it a code fault in the
        # trial script, and took the branch below to hand it back -- with 88% of the
        # budget unspent and nothing wrong with the code. An unexplained death is
        # the case the previous wording had no name for, so it borrowed this one.
        # The round-due wake, from the shared composer (fork text verbatim in
        # wakes.py). The fork keyed the wake ops:<campaign>:r<N>; under the
        # grant the campaign is the key and the round rides in the message.
        message = wakes.round_due_message(
            campaign=campaign,
            round_no=round,
            where=_where(meta),
            objective=objective,
            ledger=ledger,
            max_rounds=max_rounds,
        )
        wake_note = self.schedule_look(
            campaign,
            led.parent,
            message=message,
            eta_seconds=eta_seconds,
        )
        from oncall_flow.instrument import log_event

        log_event(led.parent, "submit", round=round, trials=submitted)
        if round >= 1:
            _record_basis(led.parent, basis, "submit")
        # Recorded from what actually happened, not from having asked. This event
        # is what attendance counts as a wake and what a reader checks to answer
        # "will this campaign come back on its own" -- and with no scheduler in
        # the process (a one-shot `raven agent -m` turn), schedule_look
        # declines and nothing is pending. Measured 2026-08-25: a subagent
        # campaign's trail said wake_scheduled while the wake store said
        # {"jobs": []}, and the resident host polled that empty store forever.
        if wake_note.startswith("Scheduled a wake"):
            log_event(led.parent, "wake_scheduled", round_due=round, eta_seconds=eta_seconds)
        else:
            log_event(led.parent, "wake_unavailable", round_due=round, note=wake_note[:200])
        drift_note = ""
        if submitted_note:
            # Stated as fact, with no verdict attached. Whether a given edit was the
            # job being done or the question being changed depends on the domain,
            # and that call belongs to whoever wrote this task's pre-registration.
            drift_note = (
                "\nThe case has changed since this campaign's first submit:\n"
                f"{submitted_note}\n"
                "That is recorded. If any of it was not you, or not intended, say so before "
                "this round's results are read against the earlier ones.\n"
            )
        return (
            f"Submitted {len(submitted)} job(s) for campaign '{campaign}' round {round} on {_where(meta)}: "
            f"{', '.join(submitted)}.\nLedger: {ledger}\n{drift_note}{wake_note}"
        )

    async def _take_action(
        self, campaign: str, ledger: str, name: str, values: dict, basis: str, eta_seconds: int = 0
    ) -> str:
        """Do one of the campaign's declared actions, once, and record it.

        Everything here is about the record. The command itself is one line that
        the transport could have run directly -- and running it directly is the
        failure this exists to prevent: a wake turn is a cold start, so an order
        placed through ``exec`` leaves nothing behind, and the next wake reads the
        ledger, sees no order, and places it again. Six shares.

        So the key comes from the action and its values, the ledger holds the
        result, and a repeat of a harmful action meets its own record instead of
        the broker.
        """
        from oncall_flow import actions as ops_actions
        from oncall_flow.backend import JobResult, JobStatus
        from oncall_flow.instrument import log_event
        from oncall_flow.ledger import Ledger
        from oncall_flow.transport import runner_from

        cdir = _resolve_campaign_dir(campaign, ledger, "", "")
        meta = _campaign_meta(cdir)
        if name == ops_actions.NONE:
            # "I looked and nothing needed doing" is a real and common outcome --
            # 65 of SentinelBench's 100 tasks end that way -- and it already has a
            # door. ops_check_later records the basis and arranges the next look,
            # and the readings this campaign declared record what was seen. A
            # second door here would write the same three facts under a different
            # name.
            return (
                "Nothing to do here: an action of 'none' is ops_check_later.\n"
                f"  ops_check_later(campaign='{campaign}', eta_seconds=..., basis='what you read "
                f"and why it does not call for anything yet')\n"
                "That records the look and its basis and brings you back; the values you read are "
                "already in the campaign's readings. Nothing was done."
            )
        chosen = ops_actions.find(meta, name)
        if chosen is None:
            declared = ", ".join(sorted(a.name for a in ops_actions.declared(meta))) or "none"
            return (
                f"REFUSED: {name!r} is not one of this campaign's declared actions.\n"
                f"  declared  {declared}\n"
                f"The action table is the list of what this campaign may do, and it is fixed "
                f"before anything runs -- so an action that is not in it cannot be taken now. "
                f"Nothing was done."
            )
        # The shape of the call first, then the decision behind it: a caller that
        # left out the quantity is told about the quantity, not about its basis.
        command, missing = ops_actions.fill(chosen, values)
        if missing:
            return (
                f"REFUSED: action {name!r} needs {', '.join(missing)}, and this call gave "
                f"{'nothing' if not values else ', '.join(sorted(values))}.\n"
                f"  the command  {chosen.command}\n"
                f"Pass values={{...}} with one entry per placeholder. A command missing its "
                f"quantity is not a smaller order. Nothing was done."
            )
        # Acting is a decision, and it gets the same gate waiting and killing get:
        # a basis resting on an observation taken since the last decision. Exempting
        # it would put the friction on every branch except the irreversible one.
        refusal = _basis_refusal(cdir, basis, f"action:{name}")
        if refusal:
            return refusal
        led = Ledger(Path(ledger).expanduser() if ledger else cdir / "ledger.json")
        prior = [r for r in led.all() if (r.config or {}).get("action") == name]
        key = ops_actions.idem_key(chosen, values, already=len(prior))
        done = led.get(key)
        if done is not None and done.is_terminal:
            # The cold-start guarantee, in the one place it can be given.
            out = (done.result.output if done.result else {}) or {}
            return (
                f"ALREADY DONE: this campaign already did {name!r} with these values.\n"
                f"  when      {out.get('at') or 'earlier'}\n"
                f"  it said   {str(out.get('stdout') or '').strip()[:200] or '(nothing)'}\n"
                f"Repeating it was declared harmful, so nothing was done now. If this needs "
                f"doing again with DIFFERENT values, pass those; if the record is wrong, say so "
                f"with ops_note rather than acting twice."
            )
        try:
            runner = runner_from(_resolve_meta(meta), what="campaign", cap_seconds=120.0)
        except Exception as exc:  # noqa: BLE001
            return f"Could not reach the campaign's machine to act: {exc}. Nothing was done."
        import asyncio

        led.record(key, campaign=campaign, config={"action": name, "values": dict(values or {})})
        try:
            rc, out = await asyncio.to_thread(runner, command)
        except Exception as exc:  # noqa: BLE001
            led.set_result(
                key,
                JobResult(status=JobStatus.FAILED, output={"command": command}, error=f"{type(exc).__name__}: {exc}"),
            )
            log_event(cdir, "action_failed", action=name, error=str(exc)[:200])
            return f"Action {name!r} could not be run: {exc}. It is recorded as failed."
        at = datetime.now().isoformat(timespec="seconds")
        body = str(out).strip()
        led.set_result(
            key,
            JobResult(
                status=JobStatus.SUCCEEDED if rc == 0 else JobStatus.FAILED,
                output={"command": command, "rc": rc, "stdout": body[:2000], "at": at},
                error=None if rc == 0 else f"exit {rc}",
            ),
        )
        log_event(cdir, "action_taken", action=name, values=dict(values or {}), rc=rc, idem_key=key)
        _record_basis(cdir, basis, f"action:{name}")
        woke = self._wake_after_action(campaign, cdir, led, name, key, at, eta_seconds)
        if rc != 0:
            return (
                f"Action {name!r} ran and failed (exit {rc}). Recorded as {key}.\n"
                f"  it said  {body[:400] or '(nothing)'}\n"
                f"Whether it took effect anyway is not something this can tell you -- read the "
                f"world before deciding to try again.\n{woke}"
            )
        return (
            f"Did {name!r} for campaign '{campaign}'. Recorded as {key}, so a later wake will "
            f"see it was done.\n  it said  {body[:400] or '(nothing)'}\n{woke}"
        )

    def _wake_after_action(self, campaign: str, cdir: Path, led, name: str, key: str, at: str, eta_seconds: int) -> str:
        """Leave something that will bring this campaign back, or say why not.

        The invariant every branch of an on-call turn owes: when the turn ends,
        either something is running or a wake is pending, or the campaign simply
        stops with whatever budget it had left. Measured 2026-08-21 -- three
        campaigns asked the owner a question, heard nothing, and sat untouched for
        three hours with 86% and 81% of their budgets unspent, because neither
        return path of ops_ask_owner scheduled anything. An action is the same
        shape: it finishes the moment the command returns, so nothing about it
        outlives the turn.

        Naming ops_check_later in the reply is not the same thing. That is a
        sentence the loop may or may not act on; this is a timer.

        The exception is a trial still running: its own wake is already pending,
        and arranging another would replace it with an earlier one and wake the
        loop to a round that is not finished.
        """
        if any(not rec.is_terminal for rec in led.all()):
            return (
                "A trial of this campaign is still running, and its own wake is already "
                "pending -- so nothing further was scheduled here."
            )
        if self._scheduler is None:
            return (
                "Nothing is scheduled and no timer is available here: arrange the next look "
                "with ops_check_later, or end the campaign with ops_finish, before this turn "
                "ends."
            )
        delay = int(eta_seconds) if int(eta_seconds or 0) > 0 else 600
        return self.schedule_look(
            campaign,
            cdir,
            message=wakes.after_action_message(campaign=campaign, name=name, at=at, key=key),
            eta_seconds=delay,
        )


class OpsCheckLaterTool(_OpsScheduler):
    """Re-schedule a later check on a running campaign without submitting anything.

    For the "woke too early" case: the agent wakes, sees via ops_tune_status that
    the jobs are still running, and simply wants to wait more. This schedules
    another wake at the agent's re-estimated ETA -- no new trials -- so it does not
    duplicate work, and (like ops_submit) schedules through the wake grant
    directly so it works inside a scheduler-triggered turn.
    """

    timeout_seconds = 30.0

    # The fork's ends_turn=True ("waiting is the whole point, the turn is over")
    # is a loop primitive the trunk does not have; the gate hook says
    # the same thing through after_iteration short_circuit. Every refusal
    # below is a plain string on purpose: under the hook,
    # as under the fork's ends_turn=False override, a refused wait leaves the
    # turn open -- nothing was arranged, and a closed turn here is the one
    # state no wake recovers.

    @property
    def name(self) -> str:
        return "ops_check_later"

    @property
    def description(self) -> str:
        return (
            "Wait longer on a campaign whose jobs are not finished yet: schedule another wake to "
            "re-check, without submitting any new trials. Use this when you wake and ops_tune_status "
            "shows work still running. Pass eta_seconds = your re-estimate of the remaining time."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "campaign": {"type": "string", "description": "Campaign name (same as used in ops_submit)."},
                "ledger": {"type": "string", "description": "Ledger path for this campaign."},
                "eta_seconds": {
                    "type": "integer",
                    "description": (
                        "When to look next, in seconds of wall-clock time. Not an estimate of when the "
                        "job finishes. The compute budget is counted in GPU minutes, which on a "
                        "multi-GPU host is a different quantity. The pace is yours to choose."
                    ),
                },
                "basis": {"type": "string", "description": _BASIS_HELP},
                "metric": {
                    "type": "string",
                    "description": "Metric name to read back; defaults to the campaign's declared objective.",
                },
                "objective": {"type": "string", "description": "Objective text, for the wake message (optional)."},
            },
            "required": ["eta_seconds", "basis"],
        }

    async def execute(
        self,
        eta_seconds: int,
        campaign: str = "",
        ledger: str = "",
        basis: str = "",
        metric: str = "",
        objective: str = "",
        **kwargs: Any,
    ) -> str:
        cdir = _resolve_campaign_dir(campaign, ledger, getattr(self, "_session_key", ""), getattr(self, "_task", ""))
        # Backfill both handles from the resolved campaign. Omitting them is allowed,
        # and without this the wake was named "ops::recheck" -- outside the
        # "ops:<campaign>:" prefix that every dedup and re-arm check keys on, so the
        # one-pending-wake invariant did not apply to it -- and its message read
        # "[Ops campaign '' re-check] Call ops_tune_status(ledger='')", which is the
        # entire context a wake turn gets.
        campaign = campaign or cdir.name
        ledger = ledger or str(cdir / "ledger.json")
        # All refusals below leave the turn open, and have to: this tool's
        # default is that the turn is over because a wake was arranged, and on a
        # refusal none was. A closed turn then leaves the campaign with nothing
        # pending and nothing reported, which is the one state no wake recovers.
        #
        # Concluded is checked here and not only in the cron re-arm: that guard
        # (service._keep_ops_campaign_watched) covers the wake the machinery adds
        # by itself, while this tool is how a turn schedules one on purpose -- and
        # ops_finish stands down pending wakes, not the right to arrange new ones.
        # Measured 2026-08-28: a look-then-re-arm after conclusion put a fresh
        # wake in the store, and the wake shell adopted and ran it.
        over = _concluded_notice(cdir)
        if over:
            return (
                f"REFUSED: this campaign is concluded, so no further wake will be arranged.\n"
                f"  {over}\n"
                f"If there is genuinely more to watch, declare a new campaign. Nothing was scheduled."
            )
        spent = _watch_budget_spent(cdir, attempting="another look")
        if spent:
            return spent
        refusal = _basis_refusal(cdir, basis, "check_later")
        if refusal:
            return refusal
        # No metric named here: the status tool reads the campaign's declared
        # objective, so pinning one in the wake message can only override it.
        # Every branch names the tool that performs it. "stop" was a verb with no
        # tool behind it, and a wake turn is a cold start whose whole context is
        # this message: measured 2026-08-11 across four runs in two domains, the
        # loop finished its work, wrote a complete report into the chat, offered
        # the user three options and waited -- for an answer nobody was there to
        # give. It re-armed until the cap each time. It had written the report;
        # what it lacked was the name of the door. ops_check_later is named here
        # and was called every time; ops_finish's predecessor was named nowhere and called never.
        message = wakes.recheck_message(campaign=campaign, ledger=ledger)
        from oncall_flow.instrument import log_event

        log_event(cdir, "check_later", eta_seconds=eta_seconds)
        _record_basis(cdir, basis, "check_later")
        return self.schedule_look(campaign, cdir, message=message, eta_seconds=eta_seconds)


class OpsCampaignsTool(Tool):
    """List the campaigns in this instance. Lists; does not decide.

    A campaign's name used to reach the loop in exactly one place -- the error
    raised when an unnamed call could not tell which campaign was meant -- so the
    only way to see what existed was to trip over that ambiguity on purpose. For
    the shape this is used in (several windows, several experiments, some
    finished) that is not a listing.

    Which campaign is "current" is deliberately absent: that is a per-window fact
    the resolver answers, and answering it a second way here is how two sources of
    one truth begin to disagree.

    Each line carries what a task statement would name -- host, case, budget --
    so an incoming window can match its own statement against the list.
    """

    @property
    def name(self) -> str:
        return "ops_campaigns"

    @property
    def description(self) -> str:
        return (
            "List the ops campaigns in this instance: name, whether each is still being watched "
            "or has finished, its host and case, its budget and what it has spent. Use when the "
            "user asks what experiments exist or which one something refers to, or when you need "
            "a campaign's name and do not have it. Read-only."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        import json as _c_json

        home = _ops_home()
        dirs = sorted(d for d in home.iterdir() if d.is_dir()) if home.exists() else []
        if not dirs:
            return f"No campaign under {home} yet."

        lines: list[str] = []
        for d in dirs:
            try:
                meta = _c_json.loads((d / "meta.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                lines.append(f"  {d.name}   (no readable meta.json)")
                continue
            concluded = d / "concluded.json"
            bits: list[str] = []
            if concluded.exists():
                try:
                    c = _c_json.loads(concluded.read_text(encoding="utf-8"))
                    bits.append(f"finished {c.get('concluded_at', '?')} ({c.get('outcome', '?')})")
                except (OSError, ValueError):
                    bits.append("finished")
                # Where the result actually is. A finished campaign's report is the
                # reason anyone comes back to it -- a day later, in a new window
                # with no memory of the run -- and saying only that it finished
                # leaves them to search for it.
                reports = sorted(d.glob("report-*.md"))
                if reports:
                    bits.append(f"report {reports[-1]}")
            else:
                bits.append("being watched" if _has_live_watcher(d.name) else "waiting, no window is watching it")
            bits.append(_campaign_gist(d))
            # Spend is measured on the host, so only live campaigns are probed: a
            # finished one's number is already in its record, and a listing has to
            # stay cheap enough to ask casually.
            if not concluded.exists():
                try:
                    from oncall_flow.backends import backend_from_meta

                    spent = await backend_from_meta(meta).spent_minutes()
                    total = (meta.get("budget") or {}).get("total")
                    unit = (meta.get("budget") or {}).get("unit") or ""
                    bits.append(f"spent {spent:.2f}" + (f" of {total} {unit}" if total else ""))
                except Exception:  # noqa: BLE001 -- an unread spend is a dash, never a failure
                    bits.append("spent unread (host did not answer)")
            trials = 0
            try:
                trials = len((_c_json.loads((d / "ledger.json").read_text(encoding="utf-8")).get("records") or {}))
            except (OSError, ValueError):
                pass
            if trials:
                bits.append(f"{trials} trial(s)")
            lines.append(f"  {d.name}   " + "   ".join(bits))
        return (
            f"{len(dirs)} campaign(s) under {home}:\n"
            + "\n".join(lines)
            + "\nWhich one a call means is per window; name one to work on it."
        )


class OpsNoteTool(Tool):
    """Record a user instruction on a campaign's durable state (its "chart").

    Wake turns start cold from disk and never see the chat conversation, so a
    mid-campaign instruction said in chat is invisible to them unless it is
    written down. This appends the instruction to the campaign's notes file;
    ops_tune_status surfaces the latest notes to every subsequent turn.
    """

    @property
    def name(self) -> str:
        return "ops_note"

    @property
    def description(self) -> str:
        return (
            "Record the user's mid-campaign instruction or decision for a running ops campaign "
            "(e.g. 'try a finer time step next round', 'prefer fewer trials'). Wake turns cannot see "
            "this chat -- writing the note is the ONLY way your instruction reaches the next round. "
            "Use whenever the user gives guidance about an ongoing campaign."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "campaign": {"type": "string", "description": "Campaign name (as used in ops_submit)."},
                "note": {"type": "string", "description": "The user's instruction, verbatim or faithfully summarized."},
                "ledger": {"type": "string", "description": "Ledger path if known (locates the campaign dir)."},
            },
            "required": ["campaign", "note"],
        }

    async def execute(self, campaign: str, note: str, ledger: str | None = None, **kwargs: Any) -> str:
        cdir = _resolve_campaign_dir(campaign, ledger, getattr(self, "_session_key", ""), getattr(self, "_task", ""))
        _append_note(cdir, note)
        from oncall_flow.instrument import log_event

        log_event(cdir, "note", note=note)
        return f"Noted for campaign '{campaign}': {note}\nThe next wake turn will see it via ops_tune_status."


class OpsKillTool(Tool):
    """Stop a running trial early (the early-stop lever).

    The saving-compute half of on-call lives here: a trial the caller judges not
    worth its remaining compute frees the machine for the next round instead of
    being paid for to the end. The killed trial is recorded FAILED with the
    reason, so history stays honest.

    When to use it is deliberately absent, here and in the description. Naming a
    condition -- "diverged", "clearly dominated" -- hands over the judgement the
    round is measuring, and any condition named is also a condition implied to be
    the only one.
    """

    timeout_seconds = 120.0

    @property
    def name(self) -> str:
        return "ops_kill"

    @property
    def description(self) -> str:
        return (
            "Kill specific RUNNING trials of an ops campaign early, when you judge a trial is not "
            "worth the compute it has left, so that compute is available for what you do next. "
            "Killed trials are recorded as failed with your reason. This kills individual trials; to conclude the whole campaign use ops_finish."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "campaign": {"type": "string", "description": "Campaign name."},
                "trials": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Trial keys to kill (as shown in ops_tune_status).",
                },
                "reason": {"type": "string", "description": "Why, in your own words."},
                "basis": {"type": "string", "description": _BASIS_HELP},
                "ledger": {"type": "string", "description": "Ledger path (locates the campaign dir)."},
            },
            "required": ["trials", "basis"],
        }

    async def execute(
        self,
        trials: list[str],
        campaign: str = "",
        basis: str = "",
        reason: str = "",
        ledger: str | None = None,
        **kwargs: Any,
    ) -> str:
        import json as _json

        from oncall_flow.backend import JobResult, JobStatus
        from oncall_flow.backends import backend_from_meta
        from oncall_flow.instrument import log_event
        from oncall_flow.ledger import Ledger

        cdir = _resolve_campaign_dir(campaign, ledger, getattr(self, "_session_key", ""), getattr(self, "_task", ""))
        ledger_path = cdir / "ledger.json"
        meta_path = cdir / "meta.json"
        if not ledger_path.exists() or not meta_path.exists():
            return f"No campaign state under {cdir} (need ledger.json + meta.json)."
        refusal = _basis_refusal(cdir, basis, "kill")
        if refusal:
            return refusal
        meta = _json.loads(meta_path.read_text(encoding="utf-8"))
        backend = backend_from_meta(meta)
        led = Ledger(ledger_path)
        killed, skipped, notes = [], [], []
        for key in trials:
            rec = led.get(key)
            if rec is None or rec.is_terminal or rec.handle is None:
                skipped.append(key)
                continue
            note = await backend.cancel(rec.handle)
            said = f"killed early: {reason or 'agent decision'}"
            if note:
                said += f" ({note})"
                notes.append(f"{key}: {note}")
            led.set_result(rec.idem_key, JobResult(JobStatus.FAILED, error=said))
            log_event(cdir, "kill", trial=key, reason=reason, found=note or "")
            killed.append(key)
        if killed:
            _record_basis(cdir, basis, "kill")
        out = f"Killed {len(killed)} trial(s): {', '.join(killed) or '-'}."
        if notes:
            out += " " + " ".join(notes) + "."
        if skipped:
            out += f" Skipped (not running/unknown): {', '.join(skipped)}."
        return out
