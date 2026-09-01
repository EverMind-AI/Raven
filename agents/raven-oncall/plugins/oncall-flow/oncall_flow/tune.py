"""``python -m oncall_flow.tune`` -- the fork's ``raven ops tune``, as a module entry.

The D5 ruling: the product's CLI kind is not a seam the trunk offers a plugin,
so the one typer command the fork mounted rides the package itself instead.
Same flags, same flow (sync the trial code, ensure the image, drive the
campaign from this process, print the table and the best), rendered in plain
text -- typer and rich were host dress, not behaviour. The ``raven ops
connection`` sub-app did not move: the connection registry's CLI lives on the
trunk and writes the same store this plugin's reader reads.

State lives in a ledger, so re-running resumes. The dataset is expected to
already be present on the host under ``<remote_dir>/app/data`` (a one-time
download).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m oncall_flow.tune",
        description="Run a tuning campaign on a remote Docker host, driven from this process.",
    )
    p.add_argument("--host", required=True, help="Remote host running Docker.")
    p.add_argument("--port", type=int, default=22, help="SSH port.")
    p.add_argument("--key", default="~/.ssh/id_rsa", help="SSH private key path.")
    p.add_argument("--app-dir", default="benchmarks/ops_bm25", help="Local dir (trial.py) to sync to the host.")
    p.add_argument("--remote-dir", default="/root/raven-ops", help="Remote working dir.")
    p.add_argument("--image", default="python:3.12-slim", help="Container image.")
    p.add_argument("--k1", type=float, action="append", help="BM25 k1 grid value (repeatable).")
    p.add_argument("--b", type=float, action="append", help="BM25 b grid value (repeatable).")
    p.add_argument("--metric", default="ndcg", help="Metric to optimize.")
    p.add_argument("--goal", default="max", choices=("max", "min"))
    p.add_argument("--ledger", default="", help="Ledger path (default: bm25-tune-ledger.json in cwd).")
    p.add_argument("--interval", type=float, default=3.0, help="Seconds between poll passes.")
    p.add_argument(
        "--adaptive", action="store_true", help="Let an LLM propose configs each round instead of a fixed grid."
    )
    p.add_argument(
        "--objective",
        default="Tune BM25 k1 and b to maximize nDCG@10 on the corpus.",
        help="Objective handed to the LLM proposer (adaptive mode).",
    )
    p.add_argument("--max-rounds", type=int, default=5, help="Max proposer rounds (adaptive mode).")
    p.add_argument("--batch-size", type=int, default=3, help="Configs proposed per round (adaptive mode).")
    p.add_argument(
        "--llm-base-url", default="", help="OpenAI-compatible base URL for the proposer LLM (adaptive mode)."
    )
    p.add_argument("--llm-model", default="", help="Model id for the proposer LLM (adaptive mode).")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    from oncall_flow.campaign import Campaign, Trial
    from oncall_flow.docker_backend import DockerExecutor, make_ssh_runner
    from oncall_flow.ledger import Ledger
    from oncall_flow.proposer import LLMProposer, config_key, make_openai_completer
    from oncall_flow.runner import (
        drive_polling,
        make_ssh_sync,
        prepare_remote,
        run_adaptive_polling,
    )

    if args.adaptive and not (args.llm_base_url and args.llm_model):
        print("--adaptive requires --llm-base-url and --llm-model", file=sys.stderr)
        return 1

    key_path = os.path.expanduser(args.key)
    run = make_ssh_runner(args.host, args.port, key_path)
    sync = make_ssh_sync(args.host, args.port, key_path)

    print(f"Preparing {args.host}:{args.remote_dir} (sync {args.app_dir}, ensure {args.image}) ...")
    prepare_remote(
        run, sync, image=args.image, app_local=os.path.abspath(args.app_dir) + "/", app_remote=f"{args.remote_dir}/app"
    )

    backend = DockerExecutor(run, image=args.image, remote_dir=args.remote_dir)
    ledger_obj = Ledger(args.ledger or "bm25-tune-ledger.json")
    k1 = args.k1 or [0.6, 1.0, 1.4]
    b = args.b or [0.3, 0.6, 0.9]
    grid = [{"k1": x, "b": y} for x in k1 for y in b]

    if args.adaptive:
        proposer = LLMProposer(
            make_openai_completer(args.llm_base_url, args.llm_model),
            objective=args.objective,
            seed=grid,
            batch_size=args.batch_size,
            max_rounds=args.max_rounds,
        )
        print(
            f"Adaptive tuning on {args.host}: seed {len(grid)} configs, "
            f"LLM={args.llm_model}, <= {args.max_rounds} rounds ..."
        )
        best = asyncio.run(
            run_adaptive_polling(
                "bm25_tune",
                proposer,
                backend,
                ledger_obj,
                metric=args.metric,
                goal=args.goal,
                max_rounds=args.max_rounds,
                interval=args.interval,
            )
        )
        campaign = Campaign("bm25_tune", [], backend, ledger_obj, metric=args.metric, goal=args.goal)
    else:
        trials = [Trial(config_key(c), c) for c in grid]
        campaign = Campaign("bm25_tune", trials, backend, ledger_obj, metric=args.metric, goal=args.goal)
        print(f"Running {len(trials)} trials on {args.host} ...")
        best = asyncio.run(drive_polling(campaign, interval=args.interval))

    ranked = sorted(campaign.history(), key=lambda x: x["score"], reverse=args.goal == "max")
    print(f"{'config':<48} {args.metric}")
    for h in ranked:
        print(f"{str(h['config']):<48} {h['score']}")
    if best is not None:
        print(f"BEST: {best.result.output.get('config')}  {args.metric}={best.result.metrics[args.metric]}")
        return 0
    print("no successful trial", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
