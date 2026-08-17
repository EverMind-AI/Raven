"""``raven ops`` commands -- run an Ops orchestration campaign in Raven's runtime.

``ops tune`` runs a BM25 hyperparameter grid on a remote Docker host: it syncs
the trial code, ensures the image, then drives the campaign (submit each config
as a detached container, poll, pick the best) from this process. State lives in
a ledger, so re-running resumes. The dataset is expected to already be present
on the host under ``<remote_dir>/app/data`` (a one-time download).
"""

from __future__ import annotations

import asyncio
import os

import typer
from rich.console import Console
from rich.table import Table

ops_app = typer.Typer(help="Ops: run long-running orchestration campaigns.")
console = Console()


@ops_app.command()
def tune(
    host: str = typer.Option(..., help="Remote host running Docker."),
    port: int = typer.Option(22, help="SSH port."),
    key: str = typer.Option("~/.ssh/id_rsa", help="SSH private key path."),
    app_dir: str = typer.Option("benchmarks/ops_bm25", help="Local dir (trial.py) to sync to the host."),
    remote_dir: str = typer.Option("/root/raven-ops", help="Remote working dir."),
    image: str = typer.Option("python:3.12-slim", help="Container image."),
    k1: list[float] = typer.Option([0.6, 1.0, 1.4], help="BM25 k1 grid values."),
    b: list[float] = typer.Option([0.3, 0.6, 0.9], help="BM25 b grid values."),
    metric: str = typer.Option("ndcg", help="Metric to optimize."),
    goal: str = typer.Option("max", help="max or min."),
    ledger: str = typer.Option("", help="Ledger path (default: bm25-tune-ledger.json in cwd)."),
    interval: float = typer.Option(3.0, help="Seconds between poll passes."),
    adaptive: bool = typer.Option(
        False, "--adaptive/--no-adaptive", help="Let an LLM propose configs each round instead of a fixed grid."
    ),
    objective: str = typer.Option(
        "Tune BM25 k1 and b to maximize nDCG@10 on the corpus.",
        help="Objective handed to the LLM proposer (adaptive mode).",
    ),
    max_rounds: int = typer.Option(5, help="Max proposer rounds (adaptive mode)."),
    batch_size: int = typer.Option(3, help="Configs proposed per round (adaptive mode)."),
    llm_base_url: str = typer.Option("", help="OpenAI-compatible base URL for the proposer LLM (adaptive mode)."),
    llm_model: str = typer.Option("", help="Model id for the proposer LLM (adaptive mode)."),
) -> None:
    """Run BM25 tuning on a remote host via Docker, driven by Raven.

    Grid mode (default) sweeps every ``k1`` x ``b`` combination once. ``--adaptive``
    instead seeds round 0 with that grid, then lets the LLM propose where to look
    next each round -- scoring stays deterministic in the container, so the reward
    stays verifiable.
    """
    from raven.ops import (
        Campaign,
        DockerExecutor,
        Ledger,
        LLMProposer,
        Trial,
        config_key,
        drive_polling,
        make_openai_completer,
        make_ssh_runner,
        make_ssh_sync,
        prepare_remote,
        run_adaptive_polling,
    )

    if adaptive and not (llm_base_url and llm_model):
        console.print("[red]--adaptive requires --llm-base-url and --llm-model[/red]")
        raise typer.Exit(1)

    key_path = os.path.expanduser(key)
    run = make_ssh_runner(host, port, key_path)
    sync = make_ssh_sync(host, port, key_path)

    console.print(f"Preparing {host}:{remote_dir} (sync {app_dir}, ensure {image}) ...")
    prepare_remote(run, sync, image=image, app_local=os.path.abspath(app_dir) + "/", app_remote=f"{remote_dir}/app")

    backend = DockerExecutor(run, image=image, remote_dir=remote_dir)
    ledger_path = ledger or "bm25-tune-ledger.json"
    ledger_obj = Ledger(ledger_path)
    grid = [{"k1": x, "b": y} for x in k1 for y in b]

    if adaptive:
        proposer = LLMProposer(
            make_openai_completer(llm_base_url, llm_model),
            objective=objective,
            seed=grid,
            batch_size=batch_size,
            max_rounds=max_rounds,
        )
        console.print(
            f"Adaptive tuning on {host}: seed {len(grid)} configs, LLM={llm_model}, <= {max_rounds} rounds ..."
        )
        best = asyncio.run(
            run_adaptive_polling(
                "bm25_tune",
                proposer,
                backend,
                ledger_obj,
                metric=metric,
                goal=goal,
                max_rounds=max_rounds,
                interval=interval,
            )
        )
        campaign = Campaign("bm25_tune", [], backend, ledger_obj, metric=metric, goal=goal)
    else:
        trials = [Trial(config_key(c), c) for c in grid]
        campaign = Campaign("bm25_tune", trials, backend, ledger_obj, metric=metric, goal=goal)
        console.print(f"Running {len(trials)} trials on {host} ...")
        best = asyncio.run(drive_polling(campaign, interval=interval))

    table = Table("config", metric)
    for h in sorted(campaign.history(), key=lambda x: x["score"], reverse=goal == "max"):
        table.add_row(str(h["config"]), f"{h['score']}")
    console.print(table)
    if best is not None:
        console.print(
            f"[green]BEST[/green]: {best.result.output.get('config')}  {metric}={best.result.metrics[metric]}"
        )
    else:
        console.print("[red]no successful trial[/red]")


__all__ = ["ops_app"]
