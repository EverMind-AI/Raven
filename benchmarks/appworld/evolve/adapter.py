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
import json
import os
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Optional

from raven.evolver.analysis.stability_bucket import (
    TaskStability,
    _bucket_for,
)
from raven.evolver.orchestrator.scoring import (
    EvalBackend,
    TaskEval,
    with_infra_rerun,
)
from raven.evolver.scheduler.anchor_selection import simple_anchor
from raven.evolver.tree.node import HarnessNode

ActivationOf = Callable[[HarnessNode], Any]

# Batch mode classifications (from batch.py's mode()). INFRA is an
# infrastructure failure (env/proxy/timeout) — a Gate-f re-run candidate.
MODE_PASS = "PASS"
MODE_LEGIT_FAIL = "LEGIT_FAIL"  # completed but wrong answer
MODE_INCOMPLETE = "INCOMPLETE"  # stopped early / empty response
MODE_INFRA = "INFRA"  # env/proxy/timeout

_TRIAL_SUFFIX_RE = re.compile(r"_k\d+\.json$")


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
    out_dir = Path(out_dir)
    dirs = []
    for name in (out_dir.name, f"{out_dir.name}_infra_rerun1", f"{out_dir.name}_infra_rerun2"):
        d = out_dir.parent / name
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
    """
    out_dir = aw.out_dir_root / experiment
    out_dir.mkdir(parents=True, exist_ok=True)
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
    return read_out_dir(out_dir)


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
