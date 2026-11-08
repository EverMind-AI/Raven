"""AppWorld scorer adapter — the fast bench for a real self-evolution round.

AppWorld (StonyBrookNLP) is an interactive benchmark: the agent writes Python
that calls app APIs (venmo / spotify / gmail / ...) to complete a task, scored
``pass@1`` over K attempts. Splits: train 90 / dev 57 / test_normal 168. It runs
much faster than SWE-bench, which makes a full evolution round tractable.

The scorer is the batch orchestrator ``benchmarks.appworld.batch`` (in
the appworld worktree; talks to appworld over HTTP — two venvs, appworld pins
pydantic v1, raven v2). CLI (from the real ``batch.py``)::

    python -m benchmarks.appworld.batch \
        --split train --n 90 --k 3 --conc 8 \
        --config <subject_config.json> \
        --out-dir <dir> --experiment <name> --env "VERIFY_FINALIZE=1"

A task subset (anchor screen) is passed as ``--tasklist <file>`` (one id per
line), not a comma list. The primary eval path checks a candidate's real
commit out into a worktree and runs against that checkout (``cwd`` override
in :func:`run_eval`); the ``--env`` activation string remains supported for
env-gated experiments but the shipped harness carries no env-gated levers.

Output layout (what ``batch.py`` writes): the out-dir
holds one ``{task_id}_k{k}.json`` per attempt plus a ``summary.json``. Each
attempt dict carries ``success`` (pass), ``task_completed``, and — on infra
failure — ``infra_error``. :func:`read_out_dir` replicates
``paired_verdict.load_passcounts`` exactly: attempts counts every trial
(including infra, which scores as a non-pass), passes counts ``success``, and
infra attempts are surfaced separately for Gate-f re-run accounting. Emitting
the bench-neutral :class:`TaskEval` contract keeps the orchestrator
bench-agnostic above this seam.
"""

from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Optional

from evolver import runmark
from evolver.analysis.stability_bucket import (
    TaskStability,
    _bucket_for,
)
from evolver.orchestrator.scoring import (
    EvalBackend,
    TaskEval,
    with_infra_rerun,
)
from evolver.scheduler.anchor_selection import simple_anchor
from evolver.tree.node import HarnessNode

ActivationOf = Callable[[HarnessNode], Any]

# Batch mode classifications (from batch.py's mode()). INFRA is an
# infrastructure failure (env/proxy/timeout) — a Gate-f re-run candidate.
MODE_PASS = "PASS"
MODE_LEGIT_FAIL = "LEGIT_FAIL"  # completed but wrong answer
MODE_INCOMPLETE = "INCOMPLETE"  # stopped early / empty response
MODE_INFRA = "INFRA"  # env/proxy/timeout

_TRIAL_SUFFIX_RE = re.compile(r"_k\d+\.json$")
_TRIAL_INDEX_RE = re.compile(r"_k(\d+)\.json$")

#: The SOP infra-rerun ladder's rung numbers (see ``eval_with_infra_rerun``).
_LADDER_RERUNS = (1, 2)

#: Bounded retries for transient Windows sharing violations on a read/unlink.
_IO_ATTEMPTS = 5
_IO_BACKOFF = 0.05

_LOG = logging.getLogger(__name__)


def _selection_digest(task_ids: list[str] | None) -> str:
    """Stable digest of the requested task selection, or ``"all"`` for the full split.

    A count is not enough: two same-sized selections (``["t1"]`` vs ``["t2"]``)
    must not reuse each other's completed batch. Sorting makes the digest
    independent of the caller's list order.
    """
    if not task_ids:
        return "all"
    digest = hashlib.sha256()
    for task_id in sorted(task_ids):
        digest.update(task_id.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def _content_digest(path: Path) -> str:
    """Digest of the subject config's bytes; the path alone cannot pin identity.

    ``evolver/launch/config.py`` only compares the YAML value, so editing the
    file at the same path leaves that drift gate green. A transient read
    failure (a Windows editor or scanner holding the file) is retried before
    it is reported as ``"unreadable"``; treating it as a new identity would
    discard paid-for trials that are still valid for the same config.
    """
    delay = _IO_BACKOFF
    for attempt in range(_IO_ATTEMPTS):
        try:
            return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
        except OSError:
            if attempt + 1 == _IO_ATTEMPTS:
                return "unreadable"
            time.sleep(delay)
            delay *= 2
    return "unreadable"  # pragma: no cover - the loop always returns


def _read_trial(path: Path) -> dict | None:
    try:
        with open(path) as f:
            record = json.load(f)
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _results_complete(out_dir: Path, task_ids: list[str] | None, k: int, n: int | None) -> bool:
    """Whether ``out_dir`` holds every trial the requested selection asked for.

    A marker alone cannot prove the set is whole: ``batch.py`` resumes file by
    file, so a deleted or corrupt trial would otherwise vanish from the
    measurement, and from ``cold_start_done``'s count, which only sees this
    directory. Unparseable files do not count — the batch re-runs those.
    """
    out_dir = Path(out_dir)
    if task_ids:
        return all(_read_trial(out_dir / f"{tid}_k{i}.json") is not None for tid in task_ids for i in range(k))
    if not n or n <= 0:
        return False
    counts: dict[str, int] = {}
    for name in glob.glob(str(out_dir / "*_k*.json")):
        match = _TRIAL_INDEX_RE.search(name)
        if match is None or int(match.group(1)) >= k:
            continue
        record = _read_trial(Path(name))
        if record is None:
            continue
        tid = _task_id_of(record, name)
        counts[tid] = counts.get(tid, 0) + 1
    return len(counts) == n and all(count == k for count in counts.values())


def _select_tasks(evals: dict[str, TaskEval], task_ids: list[str] | None) -> dict[str, TaskEval]:
    """Restrict a whole-directory read to the tasks this call asked for."""
    if not task_ids:
        return evals
    wanted = set(task_ids)
    return {tid: ev for tid, ev in evals.items() if tid in wanted}


def _ladder_dirs(out_dir: Path) -> list[Path]:
    """The base out-dir and its SOP infra-rerun siblings, existing or not."""
    out_dir = Path(out_dir)
    return [out_dir] + [out_dir.parent / f"{out_dir.name}_infra_rerun{i}" for i in _LADDER_RERUNS]


def _unlink_with_retry(path: Path) -> None:
    """Remove ``path``, retrying a transient Windows sharing violation.

    A viewer, indexer or backup tool can hold a result file open; on Windows
    ``unlink`` then fails with ``PermissionError`` while POSIX would remove it.
    The denial is retried with bounded backoff, then raised: the caller must
    not score an old measurement under a new identity.
    """
    delay = _IO_BACKOFF
    for attempt in range(_IO_ATTEMPTS):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt + 1 == _IO_ATTEMPTS:
                raise
            time.sleep(delay)
            delay *= 2


def _discard_stale_trials(out_dir: Path) -> None:
    """Clear trial files left by a different identity, ladder siblings included.

    A mismatched marker means this out-dir does not hold the requested
    measurement, but ``batch.py`` would still resume every parseable file it
    finds. The infra-rerun siblings feed ``read_kept_out_dir``, so they carry
    the same stale identity.

    A result file that cannot be removed is raised, not ignored: carrying on
    would silently score the old measurement under the new identity. Nothing
    has been paid for yet, so refusing here is cheaper than a wrong number.
    """
    failed: list[Path] = []
    for directory in _ladder_dirs(Path(out_dir)):
        if not directory.is_dir():
            continue
        for path in directory.glob("*_k*.json"):
            try:
                _unlink_with_retry(path)
            except OSError:
                failed.append(path)
        for path in directory.glob("*_k*.stderr"):
            try:
                _unlink_with_retry(path)
            except OSError:
                pass
    # Never remove the marker while a scored file from the old identity is
    # still there: a marker-less directory would be treated as a fresh one on
    # the next retry, and batch.py would resume the stale JSON under the new
    # identity. Remove it only after every scored file was cleared, so an
    # interrupted refill can resume instead of being discarded again.
    if failed:
        raise OSError(f"could not clear {len(failed)} stale trial file(s) under {out_dir}: {failed[0]}")
    try:
        _unlink_with_retry(Path(out_dir) / runmark.MARK)
    except OSError as exc:
        raise OSError(f"could not remove the stale marker under {out_dir}") from exc


@dataclass(frozen=True)
class AppWorldConfig:
    """Locate and parameterise the AppWorld batch scorer."""

    appworld_root: Path  # raven checkout/worktree (holds the batch module)
    python_exe: str  # the raven (pydantic v2) venv python
    config_path: Path  # subject runtime config JSON
    out_dir_root: Path
    split: str = "train"
    n: int = 90
    conc: int = 8
    base_port: int | None = None
    batch_module: str = "benchmarks.appworld.batch"
    # Agent workspace (sessions land under <workspace>/...). Must equal the
    # ws_root diagnosis reads trajectories from; None = batch.py's default.
    workspace: Path | None = None
    extra_args: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("appworld_root", "config_path", "out_dir_root"):
            object.__setattr__(self, name, Path(getattr(self, name)))
        if self.workspace is not None:
            object.__setattr__(self, "workspace", Path(self.workspace))


def _activation_arg(activation_env: str | dict[str, str] | None) -> str:
    """Coerce a candidate's activation into the ``--env "A=1,B=1"`` string."""
    if not activation_env:
        return ""
    if isinstance(activation_env, dict):
        return ",".join(f"{k}={v}" for k, v in activation_env.items())
    return str(activation_env)


def _task_id_of(record: dict, path: str) -> str:
    """Task id from the record, falling back to the filename (as paired_verdict does)."""
    return record.get("task_id") or _TRIAL_SUFFIX_RE.sub("", os.path.basename(path))


def build_argv(
    aw: AppWorldConfig,
    K: int,
    experiment: str,
    out_dir: str | Path,
    *,
    activation_env: str | dict[str, str] | None = None,
    tasklist_path: str | Path | None = None,
) -> list[str]:
    """Assemble the ``batch.py`` command line for one screen/confirm run.

    A subset run passes ``--tasklist`` (a file of ids); a full-split run passes
    ``--n``. ``batch.py`` has no comma ``--tasks`` flag.
    """
    if K < 1:
        raise ValueError(f"K must be >= 1, got {K}")
    argv = [
        aw.python_exe,
        "-m",
        aw.batch_module,
        "--split",
        aw.split,
        "--k",
        str(K),
        "--conc",
        str(aw.conc),
        "--config",
        str(aw.config_path),
        "--out-dir",
        str(out_dir),
        "--experiment",
        experiment,
    ]
    if tasklist_path is not None:
        argv += ["--tasklist", str(tasklist_path)]
    else:
        argv += ["--n", str(aw.n)]
    if aw.workspace is not None:
        argv += ["--workspace", str(aw.workspace)]
    env_arg = _activation_arg(activation_env)
    if env_arg:
        argv += ["--env", env_arg]
    if aw.base_port is not None:
        argv += ["--base-port", str(aw.base_port)]
    argv += list(aw.extra_args)
    return argv


def read_out_dir(out_dir: str | Path) -> dict[str, TaskEval]:
    """Aggregate a batch out-dir into per-task ``TaskEval``.

    Reads the per-attempt ``{task_id}_k{k}.json`` files exactly as
    ``paired_verdict.load_passcounts``: ``attempts`` counts every trial
    (infra included, as a non-pass), ``passes`` counts ``success``, and
    ``infra_attempts`` counts trials carrying ``infra_error``.
    """
    out_dir = Path(out_dir)
    files = sorted(glob.glob(str(out_dir / "*_k*.json")))
    if not files:
        raise FileNotFoundError(f"no per-attempt *_k*.json files in AppWorld out-dir: {out_dir}")

    passes: dict[str, int] = {}
    attempts: dict[str, int] = {}
    infra: dict[str, int] = {}
    for path in files:
        try:
            rec = json.load(open(path))
        except Exception:  # noqa: BLE001 — a corrupt trial file is skipped
            continue
        tid = _task_id_of(rec, path)
        attempts[tid] = attempts.get(tid, 0) + 1
        if rec.get("infra_error"):
            infra[tid] = infra.get(tid, 0) + 1
        if rec.get("success"):
            passes[tid] = passes.get(tid, 0) + 1

    return {
        t: TaskEval(
            task_id=t,
            passes=passes.get(t, 0),
            attempts=attempts[t],
            infra_attempts=infra.get(t, 0),
        )
        for t in attempts
    }


def ladder_out_dirs(out_dir: str | Path) -> list[Path]:
    """The base out-dir plus its SOP §0 infra-rerun ladder siblings that exist
    (``<name>_infra_rerun{1,2}``, written by ``eval_with_infra_rerun``)."""
    dirs = []
    for d in _ladder_dirs(Path(out_dir)):
        if d.exists() and list(d.glob("*_k*.json")):
            dirs.append(d)
    return dirs


def read_kept_out_dir(out_dir: str | Path) -> dict[str, TaskEval]:
    """Per-task KEPT measurement across the infra-rerun ladder.

    Mirrors ``eval_with_infra_rerun``'s keep rule exactly — fewest infra trials
    wins, earlier dir wins ties — so a control arm read from disk sees the same
    measurement the candidate arm was scored with. Reading only the base dir
    would score a rerun-salvaged task as its contaminated first attempt,
    deflating the control and handing every candidate a free lift (SOP §0).
    """
    dirs = ladder_out_dirs(out_dir)
    if not dirs:
        raise FileNotFoundError(f"no per-attempt *_k*.json files in AppWorld out-dir: {out_dir}")
    kept: dict[str, TaskEval] = {}
    for d in dirs:
        for tid, ev in read_out_dir(d).items():
            cur = kept.get(tid)
            if cur is None or ev.infra_attempts < cur.infra_attempts:
                kept[tid] = ev
    return kept


def run_eval(
    aw: AppWorldConfig,
    K: int,
    experiment: str,
    *,
    activation_env: str | dict[str, str] | None = None,
    task_ids: list[str] | None = None,
    timeout: float | None = None,
    cwd: str | Path | None = None,
    reuse_complete: bool = False,
) -> dict[str, TaskEval]:
    """Run the AppWorld batch scorer and read per-task results back.

    ``experiment`` names the out-dir under ``out_dir_root`` and the session tag.
    Use a distinct ``experiment`` (and ``base_port``) per concurrent run so two
    batches never share single-world env servers (onboarding §6.5). A
    ``task_ids`` subset is written to ``<out_dir>/tasklist.txt`` and passed via
    ``--tasklist``.

    ``cwd`` overrides the subprocess working directory (default
    ``aw.appworld_root``). Passing a candidate commit's worktree here makes
    ``python -m raven...`` import the candidate's harness from that checkout
    (cwd is first on ``sys.path`` for ``-m``) — the zero-contamination eval that
    replaces writing candidate files into the live repo. Activation env is then
    unnecessary: the committed code is already the candidate.

    A successful batch writes a trial-identity marker into ``out_dir``
    (``evolver.runmark``) recording the experiment name, its split/K, the
    requested task selection, the subject config's content digest, the extra
    batch args, and a digest of the tree the batch ran in (``cwd``, or
    ``aw.appworld_root`` when ``cwd`` is None). With ``reuse_complete=True`` a
    marker matching all of those values *and* a complete set of trial files
    short-circuits the batch and re-reads the existing results — the idempotency
    the cold-start and confirm paths rely on when a run is resumed. A different
    selection of the same size does not match.

    A marker that does not match means this out-dir's files came from a
    different identity, so they are discarded before the batch runs: the batch
    resumes parseable trial files one by one and would otherwise return the old
    measurement. A partial or corrupt set under a *matching* marker is kept and
    refilled by the batch, so the base directory always ends up whole.

    A marker that cannot be written degrades to a warning: the trial is already
    paid for, and the worst case is a later run re-measuring it.
    """
    out_dir = aw.out_dir_root / experiment
    out_dir.mkdir(parents=True, exist_ok=True)
    harness_dir = Path(cwd) if cwd is not None else aw.appworld_root
    config_digest = _content_digest(aw.config_path)
    base_expected = {
        "split": aw.split,
        "k": K,
        "n": aw.n,
        "tasks": _selection_digest(task_ids),
        "extra_args": list(aw.extra_args),
    }
    expected = {**base_expected, "config": config_digest}
    if reuse_complete:
        matched = runmark.is_complete(out_dir, run_id=experiment, harness_dir=harness_dir, expected=expected)
        if matched and _results_complete(out_dir, task_ids, K, aw.n):
            try:
                return _select_tasks(read_out_dir(out_dir), task_ids)
            except FileNotFoundError:
                pass
        elif not matched and runmark.read_mark(out_dir) is not None:
            same_except_config = runmark.is_complete(
                out_dir, run_id=experiment, harness_dir=harness_dir, expected=base_expected
            )
            if config_digest == "unreadable" and same_except_config:
                # The config file was transiently unreadable, not different.
                # Keep the paid-for trials; the batch will resume them.
                _LOG.warning("config digest unreadable; keeping existing trials in %s", out_dir)
            else:
                _discard_stale_trials(out_dir)
    tasklist_path: Path | None = None
    if task_ids:
        tasklist_path = out_dir / "tasklist.txt"
        tasklist_path.write_text("\n".join(task_ids) + "\n")
    argv = build_argv(
        aw,
        K,
        experiment,
        out_dir,
        activation_env=activation_env,
        tasklist_path=tasklist_path,
    )
    subprocess.run(argv, cwd=str(cwd or aw.appworld_root), env=dict(os.environ), check=True, timeout=timeout)
    evals = _select_tasks(read_out_dir(out_dir), task_ids)
    try:
        runmark.mark_complete(out_dir, run_id=experiment, harness_dir=harness_dir, extra=expected)
    except OSError as exc:
        _LOG.warning("could not write the trial marker in %s: %s", out_dir, exc)
    return evals


def stability_from_out_dir(out_dir: str | Path) -> dict[str, TaskStability]:
    """Build a ``{task_id: TaskStability}`` from an AppWorld vanilla out-dir.

    The orchestrator's cold start normally reads legacy trial-dirs via
    ``compute_stability``; AppWorld's ``{task}_k{k}.json`` format needs this
    equivalent so a vanilla AppWorld run can seed ``vanilla_stability`` and the
    anchor. Buckets use the same ``_bucket_for`` as the legacy path. Reads the
    KEPT measurement across the infra-rerun ladder (:func:`read_kept_out_dir`)
    so the baseline reflects the same salvage rule candidate evals get.
    """
    evals = read_kept_out_dir(out_dir)
    return {
        t: TaskStability(
            task_id=t,
            attempts=ev.attempts,
            passes=ev.passes,
            bucket=_bucket_for(ev.passes, ev.attempts),
        )
        for t, ev in evals.items()
    }


def make_appworld_backend(
    aw: AppWorldConfig,
    *,
    vanilla_out_dir: str | Path,
    train_task_ids: list[str],
    test_task_ids: list[str] = (),
    activation_of: Optional[ActivationOf] = None,
    cull_sigma_mult: float = 1.5,
    trajectory_source=None,
    eval_fn=None,
    infra_max_reruns: int = 2,
    precheck=None,
    vanilla_node: Optional[HarnessNode] = None,
    cold_start_k: int = 3,
) -> EvalBackend:
    """AppWorld backend: batch scorer + cold-start over a vanilla out-dir.

    AppWorld surfaces per-trial ``infra_error``, so the SOP §0 rerun ladder is
    active here: infra-contaminated tasks are re-scored up to ``infra_max_reruns``
    times before any survivor is left to score 0 in the denominator.

    Cold start is **read-or-run** (SOP §1, idempotent): if ``vanilla_out_dir``
    already holds a ledger it is reused; otherwise, given ``vanilla_node``, the
    vanilla harness is scored on train x ``cold_start_k`` into that dir first
    (through the same infra-rerun eval). ``vanilla_out_dir`` must be
    ``aw.out_dir_root / <name>`` so the run lands where the read looks.
    """
    act = activation_of or (lambda _node: None)
    vdir = Path(vanilla_out_dir)

    def default_eval(node, task_ids, k, job_name, *, split="train"):
        cfg = aw if split == aw.split else replace(aw, split=split)
        return run_eval(cfg, K=k, experiment=job_name, task_ids=task_ids, activation_env=act(node))

    wrapped_eval = with_infra_rerun(eval_fn or default_eval, infra_max_reruns)

    def cold_start() -> dict[str, TaskStability]:
        if not (vdir.exists() and list(vdir.glob("*_k*.json"))):
            if vanilla_node is None:
                raise FileNotFoundError(f"vanilla ledger missing at {vdir} and no vanilla_node given to run it")
            wrapped_eval(vanilla_node, list(train_task_ids), cold_start_k, vdir.name, split="train")
        return stability_from_out_dir(vdir)

    def anchor(affinity=None):
        return simple_anchor(stability_from_out_dir(vdir), cull_sigma_mult=cull_sigma_mult)

    return EvalBackend(
        train_task_ids=list(train_task_ids),
        test_task_ids=list(test_task_ids),
        eval=wrapped_eval,
        cold_start=cold_start,
        anchor=anchor,
        trajectories=trajectory_source,
        precheck=precheck,
    )


__all__ = [
    "AppWorldConfig",
    "MODE_PASS",
    "MODE_LEGIT_FAIL",
    "MODE_INCOMPLETE",
    "MODE_INFRA",
    "build_argv",
    "read_out_dir",
    "read_kept_out_dir",
    "ladder_out_dirs",
    "run_eval",
    "stability_from_out_dir",
    "make_appworld_backend",
]
