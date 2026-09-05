"""Batch runner for AppWorld via N single-world env servers (multi-port).

Each ``appworld serve environment`` holds ONE world at a time, so concurrency =
N server processes on N ports. We pin one port per worker thread and stream
tasks through; per task we spawn the agent_cli subprocess (this raven venv)
pointed at that worker's port.

  raven venv    : this orchestrator + agent_cli subprocesses (pydantic v2)
  appworld venv : N env servers (pydantic v1), started here as subprocesses

Usage::

    python -m benchmarks.appworld.batch \
        --split train --n 20 --k 1 --conc 8 \
        --config /path/to/subject_config.json \
        --out-dir /private/tmp/appworld-eval/runs/floor \
        --experiment vanilla
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.request

from raven.evolver.activation.ledger import (
    WORKSPACE_ENV,
    beacon_workspace,
    mark_beacons_enabled,
)

# Dev-box defaults, overridable per machine without a code edit.
APPWORLD_ROOT = os.environ.get("APPWORLD_ROOT", os.path.expanduser("~/workspace/appworld-run"))
APPWORLD_BIN = os.environ.get("APPWORLD_BIN", os.path.join(APPWORLD_ROOT, "appworld-venv/bin/appworld"))
APPWORLD_PY = os.environ.get("APPWORLD_PY", os.path.join(APPWORLD_ROOT, "appworld-venv/bin/python"))


def _task_ids(split: str, n: int | None) -> list[str]:
    out = subprocess.check_output(
        [APPWORLD_PY, "-c", f"from appworld import load_task_ids; print('\\n'.join(load_task_ids('{split}')))"],
        cwd=APPWORLD_ROOT,
        text=True,
        timeout=120,
    )
    ids = [x.strip() for x in out.splitlines() if x.strip()]
    return ids[:n] if n else ids


def _start_server(port: int, log_dir: str) -> subprocess.Popen:
    logf = open(os.path.join(log_dir, f"envserver-{port}.log"), "w")
    return subprocess.Popen(
        [APPWORLD_BIN, "serve", "environment", "--port", str(port)],
        cwd=APPWORLD_ROOT,
        stdout=logf,
        stderr=subprocess.STDOUT,
    )


def _wait_up(port: int, timeout: float = 60.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(1)
    return False


def _run_one(task_id: str, k: int, port: int, args, out_dir: str) -> dict:
    out = os.path.join(out_dir, f"{task_id}_k{k}.json")
    # Trial-level resume: a parseable result file is the proof this trial ran;
    # skip it so re-invocation only fills the gaps. A half-written file (crash
    # mid-dump) fails to parse and is re-run. Infra-marked results are kept:
    # their retry belongs to the infra-rerun ladder's separate out-dirs.
    try:
        with open(out) as f:
            return json.load(f)
    except (OSError, ValueError):
        pass
    cmd = [
        sys.executable,
        "-m",
        "benchmarks.appworld.agent_cli",
        "--task-id",
        task_id,
        "--env-url",
        f"http://127.0.0.1:{port}",
        "--config",
        args.config,
        "--out",
        out,
        "--workspace",
        args.workspace,
        "--experiment",
        args.experiment,
        "--session",
        f"{task_id}_{args.experiment}_k{k}",
    ]  # per-attempt: retain all K trajectories
    if args.model:
        cmd += ["--model", args.model]
    env = dict(os.environ)
    # Per-attempt beacon workspace (Gate-b): each agent_cli subprocess writes
    # its activation beacons under its own dir, pre-split by task. Beacon-less
    # code never writes anything, so this is behavior-neutral for vanilla.
    beacon_ws = beacon_workspace(out_dir, task_id, k)
    try:
        beacon_ws.mkdir(parents=True, exist_ok=True)
        env[WORKSPACE_ENV] = str(beacon_ws)
    except OSError:
        pass
    for kv in (args.env or "").split(","):
        if "=" in kv:
            kk, vv = kv.split("=", 1)
            env[kk.strip()] = vv.strip()
    logf = os.path.join(out_dir, f"{task_id}_k{k}.stderr")
    run_err = None
    try:
        with open(logf, "w") as lf:
            subprocess.run(cmd, stderr=lf, stdout=lf, timeout=args.task_timeout, env=env)
    except Exception as e:  # timeout / spawn failure: agent_cli never wrote --out
        run_err = f"runner: {type(e).__name__}: {e}"
    try:
        return json.load(open(out))
    except Exception as e:
        # The trial MUST leave a result file: scoring counts attempts from
        # files, and the infra-rerun ladder only reruns tasks whose eval shows
        # infra trials. An unwritten timeout would otherwise be invisible
        # (task scored over fewer attempts, never re-run).
        rec = {
            "task_id": task_id,
            "success": False,
            "task_completed": False,
            "infra_error": run_err or f"no-result: {e}",
        }
        try:
            with open(out, "w") as f:
                json.dump(rec, f)
        except OSError:
            pass
        return rec


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="appworld-batch")
    p.add_argument("--split", default="train")
    p.add_argument("--n", type=int, default=20)
    p.add_argument("--k", type=int, default=1)
    p.add_argument("--conc", type=int, default=8)
    p.add_argument("--config", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--workspace", default=os.path.join(APPWORLD_ROOT, "ws"))
    p.add_argument("--model", default=None)
    p.add_argument("--experiment", default="vanilla")
    p.add_argument("--env", default="", help="Candidate env vars, e.g. 'VERIFY_FINALIZE=1,C3=1'")
    p.add_argument("--tasklist", default="", help="Explicit task-id file (overrides --split/--n).")
    p.add_argument("--base-port", type=int, default=8100)
    p.add_argument("--task-timeout", type=int, default=900)
    args = p.parse_args(argv)

    os.makedirs(args.out_dir, exist_ok=True)
    os.makedirs(args.workspace, exist_ok=True)
    mark_beacons_enabled(args.out_dir)
    if args.tasklist:
        tasks = [x.strip() for x in open(args.tasklist) if x.strip()]
    else:
        tasks = _task_ids(args.split, args.n)
    print(f"[batch] {len(tasks)} tasks x K={args.k}, conc={args.conc}, exp={args.experiment} env={args.env}")

    ports = list(range(args.base_port, args.base_port + args.conc))
    servers: dict[int, subprocess.Popen] = {}

    def _shutdown_servers():
        for proc in servers.values():
            proc.terminate()
        for proc in servers.values():
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    try:
        for pt in ports:
            servers[pt] = _start_server(pt, args.out_dir)
        failed_ports = []
        for pt in ports:
            ok = _wait_up(pt)
            print(f"[batch] env server :{pt} {'UP' if ok else 'FAILED'}")
            if not ok:
                failed_ports.append(pt)
        if failed_ports:
            # A dead server would burn 1/N of every task as infra errors;
            # refusing to score is the only honest option.
            print(
                f"[batch] aborting: env servers failed to start on ports "
                f"{failed_ports} (see envserver-*.log in {args.out_dir})",
                file=sys.stderr,
            )
            return 3

        # work queue of (task_id, k); worker per port
        work: "queue.Queue" = queue.Queue()
        for t in tasks:
            for k in range(args.k):
                work.put((t, k))
        results: list[dict] = []
        lock = threading.Lock()
        done = [0]
        total = work.qsize()

        def worker(port: int):
            while True:
                try:
                    task_id, k = work.get_nowait()
                except queue.Empty:
                    return
                try:
                    res = _run_one(task_id, k, port, args, args.out_dir)
                except Exception as e:
                    res = {"task_id": task_id, "success": False, "infra_error": f"runner: {e}"}
                with lock:
                    results.append(res)
                    done[0] += 1
                    print(
                        f"[batch] {done[0]}/{total} :{port} {task_id} "
                        f"success={res.get('success')} done={res.get('task_completed')} "
                        f"infra={res.get('infra_error')}"
                    )
                work.task_done()

        threads = [threading.Thread(target=worker, args=(pt,)) for pt in ports]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
    finally:
        _shutdown_servers()

    # ---- summarize ----
    def mode(r: dict) -> str:
        if r.get("success"):
            return "PASS"
        if r.get("infra_error"):
            return "INFRA"
        if r.get("task_completed"):
            return "LEGIT_FAIL"  # tried + completed but wrong
        return "INCOMPLETE"  # stopped early / empty response

    from collections import Counter

    by_mode = Counter(mode(r) for r in results)
    npass = by_mode.get("PASS", 0)
    summary = {
        "n_tasks": len(tasks),
        "k": args.k,
        "n_trials": len(results),
        "pass_at_1": round(npass / len(results), 4) if results else 0,
        "modes": dict(by_mode),
        "experiment": args.experiment,
    }
    tmp_path = os.path.join(args.out_dir, "summary.json.tmp")
    with open(tmp_path, "w") as f:
        json.dump({"summary": summary, "results": results}, f, indent=2)
    os.replace(tmp_path, os.path.join(args.out_dir, "summary.json"))
    print("\n[batch] SUMMARY:", json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
