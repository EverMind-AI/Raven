#!/usr/bin/env python3
"""Run Raven on ClawBench as one persistent streaming session.

This runner intentionally lives under benchmarks/ and imports the runtime
package, not the other way around. It expects a local checkout of
https://github.com/claw-bench/claw-bench and uses ClawBench's own loader and
verifier.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
BENCHMARKS_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = BENCHMARKS_ROOT.parent
DEFAULT_TRACE_DIR = PROJECT_ROOT / "benchmarks" / "clawbench" / "results"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@dataclass
class PreparedTask:
    task: Any
    task_dir: Path
    workspace: Path
    prompt: str


class UsageTrackingProvider:
    """Small wrapper that records provider-reported usage per model call."""

    def __init__(self, inner):
        self._inner = inner
        self.accumulated: dict[str, int] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        self.per_call: list[dict[str, Any]] = []

    async def chat_with_retry(self, messages, tools=None, model=None, **kwargs):
        response = await self._inner.chat_with_retry(
            messages, tools=tools, model=model, **kwargs
        )
        usage = response.usage or {}
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        total_tokens = int(usage.get("total_tokens", prompt_tokens + completion_tokens) or 0)
        effective_model = model or self._inner.get_default_model()
        self.accumulated["prompt_tokens"] += prompt_tokens
        self.accumulated["completion_tokens"] += completion_tokens
        self.accumulated["total_tokens"] += total_tokens
        self.per_call.append(
            {
                "model": effective_model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "finish_reason": getattr(response, "finish_reason", None),
            }
        )
        return response

    def get_default_model(self) -> str:
        return self._inner.get_default_model()

    def __getattr__(self, name):
        return getattr(self._inner, name)


class RavenSession:
    def __init__(
        self,
        *,
        workspace: Path,
        session_id: str,
        model: str | None,
        provider_name: str | None,
        api_key: str | None,
        api_base: str | None,
        config_path: Path | None,
        max_iterations: int,
        context_window: int | None,
        context_engine: str,
        curator_model: str | None,
        restrict_to_workspace: bool,
    ) -> None:
        from raven.agent.loop import AgentLoop
        from raven.cli.commands import _make_provider
        from raven.config.raven import ContextConfig
        from raven.config.loader import load_config, set_config_path
        from raven.session.manager import SessionManager

        workspace.mkdir(parents=True, exist_ok=True)
        if config_path is not None:
            set_config_path(config_path)
        self.config = load_config(config_path)

        if model:
            self.config.agents.defaults.model = model
        if provider_name:
            self.config.agents.defaults.provider = provider_name
        if api_key:
            provider = self.config.get_provider(self.config.agents.defaults.model)
            if provider is None or provider_name == "custom":
                provider = self.config.providers.custom
                self.config.agents.defaults.provider = "custom"
            provider.api_key = api_key
            if api_base:
                provider.api_base = api_base
        elif api_base and provider_name == "custom":
            self.config.providers.custom.api_base = api_base

        self.config.agents.defaults.workspace = str(workspace.resolve())
        self.provider = UsageTrackingProvider(_make_provider(self.config))
        self.model = self.config.agents.defaults.model
        self.context_window = int(
            context_window or self.config.agents.defaults.context_window_tokens
        )
        self.curator_model = curator_model or self.model
        self.session_id = session_id
        self.previous_totals = dict(self.provider.accumulated)
        self.previous_call_count = 0

        context_config = ContextConfig(engine=context_engine, curator_model=self.curator_model)
        session_manager = SessionManager(workspace)
        self.agent = AgentLoop(
            provider=self.provider,
            workspace=workspace,
            model=self.model,
            max_iterations=max_iterations,
            context_window_tokens=self.context_window,
            brave_api_key=self.config.tools.web.search.api_key or None,
            jina_api_key=self.config.tools.web.jina_api_key or None,
            web_proxy=self.config.tools.web.proxy or None,
            exec_config=self.config.tools.exec,
            restrict_to_workspace=restrict_to_workspace,
            session_manager=session_manager,
            mcp_servers={},
            sandbox_config=self.config.tools.sandbox,
            channels_config=self.config.channels,
            everos_config=self.config.agents.defaults.everos,
            context_config=context_config,
            # Benchmarks are non-interactive batch runs — opt out of Bug2's
            # per-turn shadow-git checkpoint (no recovery channel to inject
            # into, and we don't want ``.raven/shadow.git`` in task workspaces).
            interactive=False,
        )

    @staticmethod
    def _delta(after: dict[str, int], before: dict[str, int]) -> dict[str, int]:
        return {key: int(after.get(key, 0)) - int(before.get(key, 0)) for key in after}

    async def run(self, message: str, *, task_id: str) -> tuple[str, dict[str, Any]]:
        before = dict(self.previous_totals)
        before_calls = self.previous_call_count
        from raven.spine import ChatType, Origin, Source, Text, TurnRequest

        _parts: list[str] = []

        async def _collect(ev: object) -> None:
            if isinstance(ev, Text):
                _parts.append(ev.content)

        await self.agent.run_turn(
            TurnRequest(
                origin=Origin.USER,
                source=Source(
                    channel="benchmark", chat_id=task_id, sender_id="user", chat_type=ChatType.DM
                ),
                text=message,
                conversation=self.session_id,
            ),
            _collect,
            lambda: [],
            stream=False,
        )
        final_text = "".join(_parts)
        after = dict(self.provider.accumulated)
        self.previous_totals = after
        self.previous_call_count = len(self.provider.per_call)
        calls = self.provider.per_call[before_calls:]
        last = calls[-1] if calls else {}
        context_used = int(last.get("prompt_tokens", 0) or 0) + int(
            last.get("completion_tokens", 0) or 0
        )
        return final_text or "", {
            "model_calls": len(calls),
            "call_usage_delta": self._delta(after, before),
            "session_usage_total": after,
            "last_call_usage": last,
            "context_window": self.context_window,
            "context_used": context_used,
            "context_remaining": self.context_window - context_used,
            "context_used_pct": round(context_used / self.context_window, 4)
            if self.context_window
            else None,
            "models_used": [call.get("model") for call in calls],
            "context_engine": self.agent.context_engine.name,
            "curator_model": self.curator_model
            if self.agent.context_engine.name == "curator"
            else None,
            "note": (
                "context_used is final call prompt_tokens plus completion_tokens; "
                "session_usage_total is cumulative across the streaming session"
            ),
        }

    async def close(self) -> None:
        await self.agent.close_mcp()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value).strip("_")


def copy_tree_contents(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)


def resolve_clawbench_root(value: str | None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    env_value = os.environ.get("CLAW_BENCH_ROOT") or os.environ.get("CLAWBENCH_ROOT")
    if env_value:
        return Path(env_value).expanduser().resolve()
    return (PROJECT_ROOT.parent / "claw-bench").resolve()


def load_clawbench(tasks_root: Path, args: argparse.Namespace) -> tuple[list[Any], dict[str, Path]]:
    clawbench_root = tasks_root.parent
    src_dir = clawbench_root / "src"
    if src_dir.exists() and str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))
    try:
        from claw_bench.core.task_loader import load_all_tasks
    except ImportError as exc:
        raise SystemExit(
            "Could not import claw_bench. Clone https://github.com/claw-bench/claw-bench "
            "and pass --clawbench-root or set CLAW_BENCH_ROOT."
        ) from exc

    tasks, task_dirs = load_all_tasks(
        tasks_root,
        domain=args.domain or None,
        level=args.level or None,
        track=args.track or None,
    )
    by_id = {task.id: task for task in tasks}
    if args.task:
        wanted: list[str] = []
        for item in args.task:
            wanted.extend(part.strip() for part in item.split(",") if part.strip())
        missing = [task_id for task_id in wanted if task_id not in by_id]
        if missing:
            raise SystemExit(f"Task ids are not loadable: {', '.join(missing)}")
        selected = [by_id[task_id] for task_id in wanted]
    else:
        selected = tasks
    if args.limit:
        selected = selected[: args.limit]
    return selected, task_dirs


def build_instruction(task: Any, task_dir: Path) -> str:
    instruction_path = task_dir / "instruction.md"
    if instruction_path.exists():
        return instruction_path.read_text(encoding="utf-8").strip()
    return str(getattr(task, "description", "")).strip()


def rewrite_workspace_refs(text: str, workspace: Path) -> str:
    abs_workspace = str(workspace.resolve())
    for prefix in ("`", " ", "\n", "(", "[", "'", '"'):
        text = text.replace(f"{prefix}workspace/", f"{prefix}{abs_workspace}/")
    return text


def build_prompt(task: Any, task_dir: Path, workspace: Path, index: int, total: int) -> str:
    instruction = rewrite_workspace_refs(build_instruction(task, task_dir), workspace)
    return "\n\n".join(
        [
            f"ClawBench streaming task {index}/{total}: {task.id}",
            "Keep the same long-lived session context, but solve only the current task.",
            f"Task metadata: domain={task.domain}, level={task.level}, track={task.track}.",
            f"Workspace: {workspace.resolve()}",
            "Write all required output files into that workspace. Use shell commands when needed.",
            "User task:",
            instruction,
        ]
    )


def prepare_task(
    *, task: Any, task_dir: Path, workspace_root: Path, index: int, total: int
) -> PreparedTask:
    workspace = workspace_root / f"{index:03d}_{safe_name(task.id)}"
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    copy_tree_contents(task_dir / "environment" / "data", workspace)
    setup_sh = task_dir / "environment" / "setup.sh"
    if setup_sh.exists():
        subprocess.run(
            ["bash", str(setup_sh), str(workspace.resolve())],
            cwd=str(task_dir),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        fallback_workspace = task_dir / "workspace"
        if fallback_workspace.exists() and fallback_workspace.resolve() != workspace.resolve():
            copy_tree_contents(fallback_workspace, workspace)

    return PreparedTask(
        task=task,
        task_dir=task_dir,
        workspace=workspace,
        prompt=build_prompt(task, task_dir, workspace, index, total),
    )


def grade_task(task_dir: Path, workspace: Path) -> dict[str, Any]:
    from claw_bench.core.verifier import verify_task

    result = verify_task(task_dir, workspace)
    score = result.weighted_score
    if score is None:
        score = result.checks_passed / max(result.checks_total, 1)
    return {
        "passed": result.passed,
        "score": round(float(score), 4),
        "checks_total": result.checks_total,
        "checks_passed": result.checks_passed,
        "details": result.details,
    }


async def run_one_task(
    *,
    args: argparse.Namespace,
    raven: RavenSession,
    task: Any,
    task_dir: Path,
    index: int,
    total: int,
    workspace_root: Path,
    transcripts_dir: Path,
) -> dict[str, Any]:
    prepared = prepare_task(
        task=task,
        task_dir=task_dir,
        workspace_root=workspace_root,
        index=index,
        total=total,
    )
    (transcripts_dir / f"{index:03d}_{safe_name(task.id)}.prompt.txt").write_text(
        prepared.prompt, encoding="utf-8"
    )

    started = time.monotonic()
    final_text = ""
    token_stats: dict[str, Any] = {}
    error = None
    try:
        final_text, token_stats = await asyncio.wait_for(
            raven.run(prepared.prompt, task_id=task.id),
            timeout=args.timeout or task.timeout,
        )
    except Exception as exc:
        error = str(exc)
    (transcripts_dir / f"{index:03d}_{safe_name(task.id)}.final.txt").write_text(
        final_text, encoding="utf-8"
    )
    if error:
        (transcripts_dir / f"{index:03d}_{safe_name(task.id)}.error.txt").write_text(
            error, encoding="utf-8"
        )

    grade = (
        grade_task(task_dir, prepared.workspace)
        if args.grade
        else {
            "passed": False,
            "score": 0.0,
            "checks_total": 0,
            "checks_passed": 0,
            "details": "grading skipped",
        }
    )
    delta = token_stats.get("call_usage_delta") or {}
    return {
        "task_id": task.id,
        "title": task.title,
        "domain": task.domain,
        "level": task.level,
        "track": task.track,
        "task_dir": str(task_dir),
        "workspace": str(prepared.workspace),
        "raven_error": error,
        "wall_time_s": round(time.monotonic() - started, 2),
        "tokens_input": int(delta.get("prompt_tokens", 0)),
        "tokens_output": int(delta.get("completion_tokens", 0)),
        "token_stats": token_stats,
        **grade,
    }


def write_tokens_csv(path: Path, results: list[dict[str, Any]]) -> None:
    fields = [
        "task_id",
        "model_calls",
        "input_delta",
        "output_delta",
        "total_delta",
        "session_input",
        "session_output",
        "session_total",
        "last_input",
        "last_output",
        "last_total",
        "context_window",
        "context_used",
        "context_remaining",
        "context_used_pct",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for result in results:
            stats = result.get("token_stats") or {}
            delta = stats.get("call_usage_delta") or {}
            total = stats.get("session_usage_total") or {}
            last = stats.get("last_call_usage") or {}
            writer.writerow(
                {
                    "task_id": result.get("task_id", ""),
                    "model_calls": stats.get("model_calls", 0),
                    "input_delta": delta.get("prompt_tokens", 0),
                    "output_delta": delta.get("completion_tokens", 0),
                    "total_delta": delta.get("total_tokens", 0),
                    "session_input": total.get("prompt_tokens", 0),
                    "session_output": total.get("completion_tokens", 0),
                    "session_total": total.get("total_tokens", 0),
                    "last_input": last.get("prompt_tokens", 0),
                    "last_output": last.get("completion_tokens", 0),
                    "last_total": last.get("total_tokens", 0),
                    "context_window": stats.get("context_window", 0),
                    "context_used": stats.get("context_used", 0),
                    "context_remaining": stats.get("context_remaining", 0),
                    "context_used_pct": stats.get("context_used_pct", ""),
                }
            )


def write_markdown(
    path: Path,
    results: list[dict[str, Any]],
    args: argparse.Namespace,
    model: str,
    summary_path: Path | None,
) -> None:
    total = len(results)
    passed = sum(1 for r in results if r.get("passed"))
    avg_score = sum(float(r.get("score", 0.0)) for r in results) / max(total, 1)
    lines = [
        "# ClawBench Raven Streaming Results",
        "",
        f"- Date: {now_iso()}",
        "- Runner: `benchmarks/clawbench/stream.py`",
        f"- Model: `{model}`",
        f"- Session: `{args.session_id}`",
        f"- Context engine: `{args.context_engine}`",
        f"- Curator model: `{args.curator_model}`"
        if args.context_engine == "curator"
        else "- Curator model: n/a",
        f"- Max iterations: `{args.max_iterations}`",
        f"- Summary JSON: `{summary_path}`" if summary_path else "- Summary JSON: pending",
        f"- Tasks completed: {total}",
        f"- Passed: {passed}/{total}",
        f"- Average score: {avg_score:.4f}",
        "",
        "| # | Task | Domain | Level | Passed | Score | Checks | Final Call Tokens | Model Calls | Task Tokens |",
        "|---:|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for idx, result in enumerate(results, start=1):
        stats = result.get("token_stats") or {}
        delta = stats.get("call_usage_delta") or {}
        lines.append(
            "| {idx} | `{task}` | {domain} | {level} | {passed} | {score:.4f} | {checks_passed}/{checks_total} | {context_used} | {calls} | {tokens} |".format(
                idx=idx,
                task=result.get("task_id", ""),
                domain=result.get("domain", ""),
                level=result.get("level", ""),
                passed="yes" if result.get("passed") else "no",
                score=float(result.get("score", 0.0)),
                checks_passed=int(result.get("checks_passed", 0)),
                checks_total=int(result.get("checks_total", 0)),
                context_used=int(stats.get("context_used", 0)),
                calls=int(stats.get("model_calls", 0)),
                tokens=int(delta.get("total_tokens", 0)),
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Raven against ClawBench sequentially with one persistent session."
    )
    parser.add_argument("--clawbench-root", default="")
    parser.add_argument("--tasks-root", default="")
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--domain", default="")
    parser.add_argument("--level", default="")
    parser.add_argument("--track", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--session-id", default="clawbench-stream-raven")
    parser.add_argument("--trace-dir", default=str(DEFAULT_TRACE_DIR))
    parser.add_argument("--config", default="", help="Raven config path")
    parser.add_argument("--model", default=os.environ.get("RAVEN_BENCH_MODEL", ""))
    parser.add_argument("--provider", default=os.environ.get("RAVEN_BENCH_PROVIDER", ""))
    parser.add_argument("--api-key", default=os.environ.get("OPENROUTER_API_KEY", ""))
    parser.add_argument("--api-base", default=os.environ.get("OPENROUTER_API_BASE", ""))
    parser.add_argument("--context-window", type=int, default=0)
    parser.add_argument("--context-engine", choices=["legacy", "curator"], default="legacy")
    parser.add_argument("--curator-model", default="deepseek-v4-flash")
    parser.add_argument("--max-iterations", type=int, default=40)
    parser.add_argument("--timeout", type=int, default=0)
    parser.add_argument("--no-grade", dest="grade", action="store_false")
    parser.add_argument("--results-md", default="")
    parser.set_defaults(grade=True)
    return parser.parse_args()


async def amain() -> int:
    args = parse_args()
    clawbench_root = resolve_clawbench_root(args.clawbench_root)
    tasks_root = Path(args.tasks_root).expanduser().resolve() if args.tasks_root else clawbench_root / "tasks"
    tasks, task_dirs = load_clawbench(tasks_root, args)
    if not tasks:
        raise SystemExit("No ClawBench tasks selected.")

    trace_dir = Path(args.trace_dir).expanduser().resolve()
    trace_dir.mkdir(parents=True, exist_ok=True)
    run_stamp = int(time.time())
    run_dir = trace_dir / f"run_{run_stamp}"
    workspace_root = run_dir / "workspaces"
    transcripts_dir = run_dir / "transcripts"
    workspace_root.mkdir(parents=True, exist_ok=True)
    transcripts_dir.mkdir(parents=True, exist_ok=True)

    config_path = Path(args.config).expanduser().resolve() if args.config else None
    raven = RavenSession(
        workspace=run_dir,
        session_id=args.session_id,
        model=args.model or None,
        provider_name=args.provider or None,
        api_key=args.api_key or None,
        api_base=args.api_base or None,
        config_path=config_path,
        max_iterations=args.max_iterations,
        context_window=args.context_window or None,
        context_engine=args.context_engine,
        curator_model=args.curator_model,
        restrict_to_workspace=True,
    )

    results: list[dict[str, Any]] = []
    summary_path: Path | None = None
    results_md = Path(args.results_md).expanduser().resolve() if args.results_md else trace_dir / "results.md"
    try:
        for index, task in enumerate(tasks, start=1):
            print(
                f"[{now_iso()}] task {index}/{len(tasks)}: {task.id} ({task.domain}, {task.level})",
                flush=True,
            )
            result = await run_one_task(
                args=args,
                raven=raven,
                task=task,
                task_dir=task_dirs[task.id],
                index=index,
                total=len(tasks),
                workspace_root=workspace_root,
                transcripts_dir=transcripts_dir,
            )
            results.append(result)
            print(
                json.dumps({k: v for k, v in result.items() if k != "details"}, ensure_ascii=False),
                flush=True,
            )
            (run_dir / "partial_results.json").write_text(
                json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            write_markdown(results_md, results, args, raven.model, summary_path)
    finally:
        await raven.close()

    summary_path = trace_dir / f"raven_clawbench_stream_{run_stamp}.json"
    summary = {
        "run_started_at": run_stamp,
        "runner": "benchmarks/clawbench/stream.py",
        "model": raven.model,
        "session_id": args.session_id,
        "context_engine": args.context_engine,
        "curator_model": args.curator_model if args.context_engine == "curator" else None,
        "trace_run_dir": str(run_dir),
        "tasks_total": len(results),
        "tasks_passed": sum(1 for r in results if r.get("passed")),
        "average_score": round(
            sum(float(r.get("score", 0.0)) for r in results) / max(len(results), 1), 4
        ),
        "results": results,
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    tokens_csv = trace_dir / f"raven_clawbench_stream_{run_stamp}.tokens.csv"
    write_tokens_csv(tokens_csv, results)
    write_markdown(results_md, results, args, raven.model, summary_path)
    print(f"summary: {summary_path}", flush=True)
    print(f"tokens_csv: {tokens_csv}", flush=True)
    print(f"results_md: {results_md}", flush=True)
    return 0


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    raise SystemExit(main())
