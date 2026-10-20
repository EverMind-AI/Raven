"""Run the market rung's paired arms on one pinchbench task, and grade them.

Two arms, same task, same model, same grader, compared pairwise:

* **market-on**  the market rung is in the chain (``market.enabled: true``);
* **market-off** the same flow, the same gates, with the rung's gate out
  (``market.enabled: false``).

The arm is a **config switch**, not a label. A label names a shipped
distribution, and the flow refuses a retired one, so the ablation has to be
expressed the way the distribution actually differs: by the switch.

Grading uses the task's **automated** check, which is model-free and
deterministic: it reads the report the run produced and checks the file, the
competitors, the comparison table, the pricing, the trends, the structure,
the executive summary and whether web search was used. The task's second
component is an LLM judge, and this machine has no funded judge endpoint, so
that component is reported as **not run** rather than as a zero. A judge
that could not answer is not a grader that scored zero.

    python3 benchmarks/pinchbench/direct/market_rung_arms.py \\
        --task benchmarks/pinchbench/tasks/task_18_market_research.md \\
        --repeats 3 --out /tmp/market-rung-arms
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO))

from grade_bridge import automated_grade  # noqa: E402
from market_rung import ArmResult, paired_gate  # noqa: E402
from task_loader import load_all_tasks  # noqa: E402

AGENT_DIR = REPO / "agents" / "raven-research"
ARMS = ("market-on", "market-off")


def arm_config(arm: str, home: Path) -> Path:
    """The agent home for this arm, and the config the launcher renders.

    Two files, because the launcher reads two things:

    * the **config it renders from** carries the flow's slice, and the
      rung's switch in it -- the **switch**, not the label: a retired label
      is refused outright, so the ablation cannot be expressed as a label
      and has to be expressed as the thing the label names. This has to
      travel in the rendered config: the launcher takes the flow's slice
      from the config it is given, so a switch written only into the host's
      config is overwritten and **both arms render identically**.
    * the **agent home** carries the model routing, because the host's model
      is reached by OAuth and a scratch home would otherwise have no route.
    """
    source = json.loads((AGENT_DIR / "config.json").read_text(encoding="utf-8"))
    flow = source["plugins"]["config"]["research-flow"]
    flow["market"] = {**flow.get("market", {}), "enabled": arm == "market-on"}
    # The rendered config also has to carry the model's route, because the
    # launcher refuses to start when the config it renders names a model it
    # cannot reach: the research agent's own config pins OpenRouter, and this
    # machine has no credit there. The route that works is the host's.
    source["agents"] = {
        "defaults": {
            "model": "minimax-global/MiniMax-M3",
            "provider": "minimax_global",
            "maxToolIterations": 40,
            "requestTimeoutSeconds": 600,
        }
    }
    home.mkdir(parents=True, exist_ok=True)
    # The file has to be named ``config.json``: the engine derives the
    # directories it keeps (the OAuth token, the ACP home, the cache) from the
    # config's own path, and a config named anything else is treated as a
    # directory -- which fails the turn with ``NotADirectoryError`` on the
    # first thing it tries to write.
    rendered = home / "config.json"
    rendered.write_text(json.dumps(source, indent=2), encoding="utf-8")
    (home / "config.json").write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {
                        "model": "minimax-global/MiniMax-M3",
                        "provider": "minimax_global",
                        "maxToolIterations": 40,
                        "requestTimeoutSeconds": 600,
                    }
                },
                "plugins": {"config": {"research-flow": flow}},
                "memory": {"backend": None},
                "playbooks": {"enabled": False},
                "sessionTitle": {"enabled": False},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return home


def acp_home() -> Path:
    """Where the product's ACP sessions live.

    The session file is named after the ACP session id and lives under the
    product's ACP home, which is deliberately outside the state root: the
    state root holds the product's work, the ACP home holds the engine's
    sessions. The product's name is not hard-coded here -- the launcher
    decides it, and the directory is found by shape.
    """
    for candidate in sorted((Path.home() / ".raven" / "subagent_sessions").glob("*/acp")):
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("no product ACP home under ~/.raven/subagent_sessions")


async def _run_arm(arm: str, repeat: int, task_prompt: str, root: Path) -> dict[str, Any]:
    """One run: serve the agent, drive one prompt, and collect what it wrote."""
    from raven.acp_client.acp_agent import AcpAgentBackend
    from raven.acp_client.pool import close_pool

    state = root / f"state-{arm}-{repeat}"
    traces = root / f"traces-{arm}-{repeat}"
    workspace = root / f"workspace-{arm}-{repeat}"
    workspace.mkdir(parents=True, exist_ok=True)
    traces.mkdir(parents=True, exist_ok=True)

    home = root / f"home-{arm}-{repeat}"
    config = arm_config(arm, home) / "config.json"
    backend = AcpAgentBackend(
        name=f"market-rung-{arm}-{repeat}",
        command=f"{sys.executable} {AGENT_DIR / 'run.py'} --config {config}",
        cwd=str(AGENT_DIR),
        env={
            "RAVEN_HOME": str(home),
            # The product's ACP home has to be a writable directory outside
            # the host Agent home, and one per arm so that the session a
            # grader reads belongs to this run and no other.
            "RESEARCH_NG_ACP_HOME": str(home / "acp-home"),
            "RESEARCH_NG_STATE_ROOT": str(state),
            "RAVEN_TRACING_DIR": str(traces),
            "MINIMAX_OAUTH_TOKEN_DIR": str(Path.home() / ".raven" / "oauth"),
            "RESEARCH_WEB_SEARCH_PROVIDER": os.environ.get("RESEARCH_WEB_SEARCH_PROVIDER", ""),
            "RESEARCH_TINYFISH_API_KEY": os.environ.get("RESEARCH_TINYFISH_API_KEY", ""),
        },
        ready_timeout_ms=180_000,
        timeout=3600,
    )
    try:
        reply = await backend.run(
            task_prompt,
            task_id=f"market-rung-{arm}-{repeat}",
            workspace=workspace,
            executor=None,
            session_key=f"rung:{arm}:{repeat}",
        )
    finally:
        await close_pool()

    # The run writes the report into the session's working directory. Move it
    # into this arm's own workspace so the grader reads it from a directory
    # that belongs to this run and to no other.
    produced = Path.home() / "market_research.md"
    target = workspace / "market_research.md"
    if produced.exists():
        shutil.move(str(produced), str(target))
    return {
        "arm": arm,
        "repeat": repeat,
        "workspace": str(workspace),
        "traces": str(traces),
        "home": str(home),
        "report": str(target) if target.exists() else None,
        "reply_tail": reply[-2000:],
    }


async def _main(args: argparse.Namespace) -> int:
    tasks = load_all_tasks(REPO / "benchmarks" / "pinchbench" / "tasks")
    task = next((t for t in tasks if t.file_path and Path(t.file_path).name == args.task), None)
    if task is None:
        print(f"no task file named {args.task}", file=sys.stderr)
        return 2
    print(f"task: {task.task_id} ({task.name}) [{task.grading_type}]")

    root = Path(args.out)
    root.mkdir(parents=True, exist_ok=True)
    results: list[ArmResult] = []
    details: list[dict[str, Any]] = []

    for repeat in range(1, args.repeats + 1):
        for arm in ARMS:
            print(f"--- run {repeat} {arm} ---", flush=True)
            try:
                detail = await _run_arm(arm, repeat, task.prompt, root)
            except Exception as exc:  # noqa: BLE001 - a failed run is a missing measurement
                print(f"    failed: {type(exc).__name__}: {exc}", file=sys.stderr)
                results.append(ArmResult(arm=arm, repeat=repeat, score=None, detail=str(exc)))
                details.append({"arm": arm, "repeat": repeat, "error": str(exc)})
                continue
            # A run that wrote no report is **unmeasured**, not a zero. The
            # task's own check answers 0.0 for a missing file, and folding
            # that in would score a dead run as a failing run - which is the
            # one thing a paired comparison must not do.
            produced = Path(detail["workspace"]) / "market_research.md"
            if not produced.exists():
                results.append(
                    ArmResult(
                        arm=arm,
                        repeat=repeat,
                        score=None,
                        detail="the run produced no report",
                    )
                )
                details.append({**detail, "score": None, "notes": "no report was produced"})
                print("    no report produced - UNMEASURED", flush=True)
                continue
            score, notes = automated_grade(
                task,
                detail["workspace"],
                home=Path(detail["home"]),
            )
            results.append(ArmResult(arm=arm, repeat=repeat, score=score, detail=notes))
            details.append({**detail, "score": score, "notes": notes})
            print(f"    score={score} {notes}", flush=True)
            (root / f"result-{arm}-{repeat}.json").write_text(
                json.dumps(details[-1], indent=2, ensure_ascii=False), encoding="utf-8"
            )

    verdict = paired_gate(
        [r for r in results if r.arm == "market-on"],
        [r for r in results if r.arm == "market-off"],
        k=args.k,
    )
    report = {
        "task": {"id": task.task_id, "file": args.task, "grading_type": task.grading_type},
        "grader": "the task's automated check (model-free); the LLM-judge component did not run",
        "results": [r.__dict__ for r in results],
        "verdict": verdict.to_dict(),
    }
    (root / "arms.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report["verdict"], indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--task", default="task_18_market_research.md")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--k", type=float, default=2.0)
    parser.add_argument("--out", default="/tmp/market-rung-arms")
    args = parser.parse_args()
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
