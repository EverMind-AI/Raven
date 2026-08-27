"""AppWorld bench plugin for the unified launcher (built-in scorer line).

``bench_config`` schema (YAML, under the run spec):

    bench_config:
      config_path: /path/subject_runtime.json   # required: agent runtime config
      appworld_data_root: /path/appworld        # AppWorld install (holds data/);
                                                # exported as APPWORLD_ROOT
      train_task_file: /path/train.txt          # or train_task_ids: [...]
      test_task_file: /path/test.txt            # optional (enables sealed test)
      n: 90                # optional cap on train tasks
      conc: 8
      base_port: 8600
      python_exe: <venv python>                 # default: current interpreter
      vanilla_experiment: vanilla               # cold-start ledger name
      extra_args: ["--task-timeout", "400"]     # passed through to batch.py
      whitelist: ["raven/agent/", ...]          # default: sandbox whitelist
      min_confirm_lift: 0.0
      taxonomy_mode: hardcoded                  # or induce
      why_selection: driver
      analysis_mode: mapreduce                  # or agentic
      agentic_model: claude-opus-4-8
      require_beacon: true
      zero_hit_preflight: false
      baseline_mode: frozen                     # or same_session (~2x eval cost,
                                                # immune to endpoint drift)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from benchmarks.appworld.evolve import adapter as aw_adapter
from raven.evolver.launch.contract import BenchBundle, LaunchContext, validate_whitelist
from raven.evolver.orchestrator.scoring import eval_with_infra_rerun

_KNOWN_KEYS = {
    "config_path",
    "train_task_file",
    "train_task_ids",
    "test_task_file",
    "test_task_ids",
    "n",
    "conc",
    "base_port",
    "python_exe",
    "vanilla_experiment",
    "extra_args",
    "whitelist",
    "min_confirm_lift",
    "taxonomy_mode",
    "taxonomy_path",
    "why_selection",
    "analysis_mode",
    "agentic_model",
    "require_beacon",
    "zero_hit_preflight",
    "appworld_data_root",
    "precheck",
    "precheck_min_tok_s",
    "baseline_mode",
}


def _wait_ports_free(base: int, count: int, timeout: float = 20.0) -> None:
    """Wait for this run's own env-server ports to free after a batch.

    ``batch.py`` terminates its servers with SIGTERM; a server can outlive the
    batch by a few seconds. The Gate0 precheck (correctly) treats a bound port
    as an orphan, so give our just-finished phase a grace window instead of
    failing the round on our own shutdown race.
    """
    import socket
    import time

    def bound(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", port)) == 0

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(bound(p) for p in range(base, base + count)):
            return
        time.sleep(1.0)


def _abs_against(base_dir: Path, value: str) -> Path:
    # bench_config paths resolve like the top-level keys do: against the
    # config file's directory, so a resume from another CWD reads the same
    # inputs the run started with.
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base_dir / p).resolve()


def _task_ids(bc: dict, prefix: str, base_dir: Path) -> list[str]:
    ids = bc.get(f"{prefix}_task_ids")
    if ids:
        ids = [str(t) for t in ids]
    else:
        file_key = f"{prefix}_task_file"
        if not bc.get(file_key):
            return []
        path = _abs_against(base_dir, bc[file_key])
        if not path.is_file():
            raise ValueError(f"bench_config.{file_key}: not found: {path}")
        ids = [line.strip() for line in path.read_text().splitlines() if line.strip()]
    placeholders = [t for t in ids if "<" in t or ">" in t]
    if placeholders:
        raise ValueError(
            f"bench_config {prefix} task ids contain placeholders "
            f"{placeholders[:3]} — replace them with real AppWorld task ids "
            "(see raven/evolver/README.md, Bootstrap)"
        )
    return ids


def build(ctx: LaunchContext) -> BenchBundle:
    spec = ctx.spec
    bc = dict(spec.bench_config)
    unknown = set(bc) - _KNOWN_KEYS
    if unknown:
        raise ValueError(f"bench_config: unknown keys {sorted(unknown)}")
    if not bc.get("config_path"):
        raise ValueError("bench_config.config_path is required (subject runtime config)")
    config_path = _abs_against(spec.config_dir, bc["config_path"])
    if not config_path.is_file():
        raise ValueError(
            f"bench_config.config_path not found: {config_path} — this is the "
            "subject agent's runtime config JSON (model endpoint etc.; start "
            "from docs/examples/subject_runtime.json)"
        )
    # Validate against the runtime-config schema now, not at trial time: an
    # empty or malformed subject config otherwise passes `check` and fails
    # 270 trials deep into the cold start.
    try:
        import contextlib
        import io

        from raven.config.loader import load_config as _load_subject_config

        with contextlib.redirect_stdout(io.StringIO()):
            _load_subject_config(config_path)
    except ValueError as exc:
        raise ValueError(
            f"bench_config.config_path {config_path} is not a valid Raven "
            f"runtime config: {exc}\nStart from docs/examples/subject_runtime.json"
        ) from exc

    # The batch scorer locates the AppWorld install/data via APPWORLD_ROOT;
    # surface a missing install at build time (check catches it), not as a
    # mid-run stack trace. Falls back to the APPWORLD_ROOT env var, then the
    # batch scorer's default, but is validated unconditionally either way.
    data_root = _abs_against(
        spec.config_dir,
        bc.get("appworld_data_root") or os.environ.get("APPWORLD_ROOT") or "~/workspace/appworld-run",
    )
    if not (data_root / "data").is_dir():
        raise ValueError(
            f"no AppWorld install found at {data_root} (no data/ under it) — "
            "install AppWorld there or point bench_config.appworld_data_root "
            "at your install (see raven/evolver/README.md, Bootstrap)"
        )
    if not any((data_root / "data").iterdir()):
        raise ValueError(
            f"AppWorld data dir is empty: {data_root / 'data'} — the download "
            "did not finish; run `appworld download data` in that install"
        )
    appworld_bin = Path(os.environ.get("APPWORLD_BIN") or data_root / "appworld-venv/bin/appworld")
    if not appworld_bin.is_file():
        raise ValueError(
            f"appworld binary not found at {appworld_bin} — create the venv "
            "per raven/evolver/README.md Bootstrap step 1, or set APPWORLD_BIN"
        )
    os.environ["APPWORLD_ROOT"] = str(data_root)

    train_ids = _task_ids(bc, "train", spec.config_dir)
    if not train_ids:
        raise ValueError("bench_config: train_task_ids or train_task_file is required")
    if bc.get("n") is not None:
        n = int(bc["n"])
        if n <= 0:
            raise ValueError(f"bench_config.n must be > 0, got {n}")
        train_ids = train_ids[:n]
    conc = int(bc.get("conc") or 8)
    if conc <= 0:
        raise ValueError(f"bench_config.conc must be > 0, got {conc}")
    test_ids = _task_ids(bc, "test", spec.config_dir)
    overlap = set(train_ids) & set(test_ids)
    if overlap:
        raise ValueError(f"train/test task sets overlap: {sorted(overlap)[:5]} …")

    from benchmarks.appworld.evolve.sandbox import WHITELIST_PREFIXES

    whitelist = tuple(bc.get("whitelist") or WHITELIST_PREFIXES)
    validate_whitelist(spec.repo_root, spec.base_sha, whitelist)

    work = Path(spec.work_dir)
    runs_root = work / "runs"
    ws_root = work / "ws"
    worktree_root = work / "wt"
    van_exp = bc.get("vanilla_experiment", "vanilla")
    vanilla_out_dir = runs_root / van_exp
    k_confirm = spec.funnel.k_confirm

    cfg = aw_adapter.AppWorldConfig(
        appworld_root=spec.repo_root,
        python_exe=bc.get("python_exe") or sys.executable,
        config_path=config_path,
        out_dir_root=runs_root,
        split="train",
        n=len(train_ids),
        conc=conc,
        base_port=int(bc.get("base_port") or 8100),
        workspace=ws_root,
        extra_args=tuple(str(a) for a in bc.get("extra_args", ())),
    )

    def cold_start_done() -> int:
        if not vanilla_out_dir.is_dir():
            return 0
        return sum(1 for tid in train_ids for k in range(k_confirm) if (vanilla_out_dir / f"{tid}_k{k}.json").is_file())

    def make_precheck():
        if not bc.get("precheck", True):
            return lambda: None
        from benchmarks.appworld.evolve.precheck import make_appworld_precheck

        if bc.get("precheck_min_tok_s") is not None:
            return make_appworld_precheck(cfg, min_tok_per_s=float(bc["precheck_min_tok_s"]))
        return make_appworld_precheck(cfg)

    def run_cold_start() -> None:
        # Fill missing trials, then the SOP infra-rerun ladder: a vanilla
        # baseline scored with infra failures kept as zeros hands every
        # candidate a free lift, so salvageable infra must be re-scored
        # (into vanilla_infra_rerun{1,2}; the KEPT readers pick them up).
        runs_root.mkdir(parents=True, exist_ok=True)
        needs_fill = cold_start_done() < len(train_ids) * k_confirm
        needs_salvage = False
        if not needs_fill:
            try:
                kept = aw_adapter.read_kept_out_dir(vanilla_out_dir)
                needs_salvage = any(ev.infra_attempts > 0 for ev in kept.values()) or len(kept) < len(train_ids)
            except FileNotFoundError:
                needs_fill = True
        if needs_fill or needs_salvage:
            # Gate0 before spending: neither the initial fill nor the
            # infra-rerun ladder may burn trials against a dead endpoint
            # (SOP §0). A clean, complete cold start skips the probe —
            # rounds have their own per-round Gate0.
            make_precheck()()

        def base_eval(_node, task_ids, k, job_name, *, split="train"):
            return aw_adapter.run_eval(cfg, K=k, experiment=job_name, task_ids=task_ids)

        eval_with_infra_rerun(base_eval, None, train_ids, k_confirm, van_exp)

    def build_orchestrator():
        from benchmarks.appworld.evolve.run import build_appworld_orchestrator

        if cfg.base_port is not None:
            _wait_ports_free(cfg.base_port, cfg.conc)
        return build_appworld_orchestrator(
            config=spec.funnel,
            aw_cfg=cfg,
            repo_root=spec.repo_root,
            base_sha=spec.base_sha,
            driver_call_fn=ctx.models.get("driver"),
            design_call_fn=ctx.models.get("design") or ctx.models.get("driver"),
            verdict_call_fn=ctx.models.get("verdict"),
            vanilla_out_dir=vanilla_out_dir,
            train_task_ids=train_ids,
            test_task_ids=test_ids,
            runs_root=runs_root,
            ws_root=ws_root,
            worktree_root=worktree_root,
            min_confirm_lift=float(bc.get("min_confirm_lift", 0.0)),
            taxonomy_mode=bc.get("taxonomy_mode", "hardcoded"),
            taxonomy_path=bc.get("taxonomy_path"),
            require_beacon=bool(bc.get("require_beacon", True)),
            zero_hit_preflight=bool(bc.get("zero_hit_preflight", False)),
            why_selection=bc.get("why_selection", "driver"),
            analysis_mode=bc.get("analysis_mode", "mapreduce"),
            agentic_model=bc.get("agentic_model", "claude-opus-4-8"),
            whitelist_prefixes=whitelist,
            precheck=make_precheck(),
            baseline_mode=bc.get("baseline_mode", "frozen"),
        )

    from raven.evolver.tree.node import HarnessNode

    root_node = HarnessNode(
        node_id="C0",
        parent_id=None,
        git_commit_sha=spec.base_sha,
        git_branch="",
        created_at=HarnessNode.utc_now(),
        created_at_iter=0,
    )

    unseal = None
    if test_ids:

        def unseal(records: list[dict], orch) -> dict:
            import dataclasses

            from benchmarks.appworld.evolve.run import build_appworld_sealed_runner
            from raven.evolver.orchestrator.sealed.runner import unseal_retention

            runner = build_appworld_sealed_runner(
                aw_cfg=cfg,
                repo_root=spec.repo_root,
                test_task_ids=test_ids,
                sealed_dir=spec.funnel.sealed_output_dir or work / "sealed",
                k=k_confirm,
            )
            report = unseal_retention(
                runner,
                records,
                vanilla_node=root_node,
                vanilla_train=orch.vanilla_train_mean,
            )
            return dataclasses.asdict(report) if dataclasses.is_dataclass(report) else dict(report)

    return BenchBundle(
        root_node_id="C0",
        root_node=root_node,
        journal_path=work / "journal" / "rounds.jsonl",
        cold_start_total=len(train_ids) * k_confirm,
        cold_start_done=cold_start_done,
        run_cold_start=run_cold_start,
        build_orchestrator=build_orchestrator,
        unseal=unseal,
        precheck=make_precheck() if bc.get("precheck", True) else None,
    )


__all__ = ["build"]
