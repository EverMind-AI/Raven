#!/usr/bin/env python3
"""Run the self-configuration eval: does Raven notice, inspect and fix its own settings?

Each case runs in its own process against its own ``RAVEN_HOME``, through the
same RPC stack the desktop page uses (``build_rpc_stack``): the tool gets the
settings writers the page lends it, approvals arrive as ``approval.request``
frames, and the stream reports every tool call. Approvals follow the case
(``approve: allow | deny``), except that delegating, browsing and the plugin
catalog are always allowed, so a denied sub-agent or browser call cannot stand
in for the behaviour under test. Clarifying questions get the case's `answer`, or "decide yourself", so a run never
waits on a person.

``HOME`` is the case's directory too, and the working directory its
``workspace``, so neither a shell command reaching for ``~/.raven`` nor a
relative write lands on the machine or in this repository.

The home's config is the case's ``config`` merged over a base built from
``--source-config``: only its ``providers`` block and default model are taken,
so the scenario starts from a clean install that can reach a model. Web tool
keys from the environment are scrubbed for the same reason.

Usage:
    uv run --all-extras python -m benchmarks.self_config_eval.run
    uv run --all-extras python -m benchmarks.self_config_eval.run --case github_link_not_connected
    uv run --all-extras python -m benchmarks.self_config_eval.run --model openrouter/anthropic/claude-sonnet-5 \
        --provider openrouter --judge-model openrouter/anthropic/claude-sonnet-5
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
CASES = HERE / "cases.yaml"
RESULTS = HERE / "results"

_SCRUBBED_ENV = (
    "SERPER_API_KEY",
    "ANYSEARCH_API_KEY",
    "SERPAPI_API_KEY",
    "JINA_API_KEY",
    "TAVILY_API_KEY",
    "EXA_API_KEY",
    "BRAVE_API_KEY",
    "FIRECRAWL_API_KEY",
    "SERPLY_API_KEY",
    "RAVEN_TRACING_DIR",
)

_ALWAYS_ALLOWED = ("spawn", "run_subagent_dag", "plugin", "web_fetch", "browser_")

_NO_ANSWER = "我现在不在，你自己判断吧。 (The user is away; decide yourself.)"


def load_cases() -> list[dict[str, Any]]:
    return yaml.safe_load(CASES.read_text(encoding="utf-8"))


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (over or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def base_config(source: Path, model: str | None, provider: str | None) -> dict[str, Any]:
    src = json.loads(source.read_text(encoding="utf-8"))
    defaults = (src.get("agents") or {}).get("defaults") or {}
    return {
        "providers": src.get("providers") or {},
        "agents": {
            "defaults": {
                "model": model or defaults.get("model"),
                "provider": provider or defaults.get("provider"),
            }
        },
        "permissions": {"mode": "ask"},
        "memory": {"backend": None},
        "sessionTitle": {"enabled": False},
        "tracing": {"enabled": False},
    }


# ---------------------------------------------------------------------------
# One case, in a child process
# ---------------------------------------------------------------------------


async def run_one(case: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    from raven.rpc.bootstrap import build_rpc_stack

    frames: list[dict[str, Any]] = []
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def send(frame: dict[str, Any]) -> None:
        frames.append(frame)
        inbox.put_nowait(frame)

    stack = await build_rpc_stack(send, channel="tui")
    ids = iter(range(1, 1_000_000))

    async def call(method: str, params: dict[str, Any]) -> Any:
        reply = await stack.dispatcher.dispatch({"jsonrpc": "2.0", "id": next(ids), "method": method, "params": params})
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error']}")
        return reply.get("result")

    tool_calls: list[dict[str, Any]] = []
    approvals: list[dict[str, Any]] = []
    questions: list[str] = []
    text: list[str] = []
    started = time.monotonic()
    try:
        created = await call("session.create", {})
        sid = created["session_id"]
        loop = stack.agent_loop
        if case.get("history"):
            session = loop.sessions.get_or_create(sid)
            for message in case["history"]:
                session.add_message(message["role"], message["content"])
            loop.sessions.save(session)
        await call("turn.subscribe", {"session_key": sid})
        await call("turn.send", {"session_key": sid, "content": case["message"]})
        own_choice = "allow" if case.get("approve") == "allow" else "deny"
        done = False
        while not done:
            remaining = timeout_s - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError(f"no message.complete within {timeout_s:.0f}s")
            frame = await asyncio.wait_for(inbox.get(), timeout=remaining)
            method = frame.get("method")
            params = frame.get("params") or {}
            if method == "approval.request":
                command = str(params.get("command") or "")
                choice = "allow" if command.startswith(_ALWAYS_ALLOWED) else own_choice
                approvals.append({"command": command, "description": params.get("description"), "choice": choice})
                await call(
                    "approval.respond",
                    {"approval_id": params["approval_id"], "choice": choice, "session_id": sid},
                )
            elif method == "clarify.request":
                questions.append(str(params.get("prompt") or params.get("question") or params))
                await call(
                    "clarify.respond",
                    {
                        "answer": case.get("answer") or _NO_ANSWER,
                        "request_id": params.get("request_id"),
                        "conversation_id": sid,
                    },
                )
            elif method == "event":
                event = params.get("event") or {}
                kind = event.get("type")
                payload = event.get("payload") or {}
                if kind == "tool.start":
                    tool_calls.append({"name": payload.get("name"), "arguments": payload.get("arguments") or {}})
                elif kind == "token.delta":
                    text.append(str(payload.get("text") or ""))
                elif kind == "message.complete":
                    done = True
        await asyncio.sleep(1.0)
        error = None
    except Exception as exc:  # noqa: BLE001 - a broken case is a result, not a crash
        error = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            await asyncio.wait_for(stack.teardown(), timeout=60)
        except Exception as exc:  # noqa: BLE001 - the turn's result stands without a clean teardown
            print(f"teardown did not finish: {type(exc).__name__}: {exc}", flush=True)
    from raven.config.loader import get_config_path

    config_after = json.loads(get_config_path().read_text(encoding="utf-8"))
    return {
        "id": case["id"],
        "tool_calls": tool_calls,
        "approvals": approvals,
        "questions": questions,
        "reply": "".join(text).strip(),
        "error": error,
        "seconds": round(time.monotonic() - started, 1),
        "config_after": config_after,
    }


def child(case_id: str, out: Path, timeout_s: float) -> None:
    case = next(c for c in load_cases() if c["id"] == case_id)
    # Not asyncio.run: its shutdown joins the default executor, and a thread
    # the stack left behind (a file watcher, an MCP session) never returns.
    result = asyncio.new_event_loop().run_until_complete(run_one(case, timeout_s))
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    sys.stdout.flush()
    os._exit(0)


# ---------------------------------------------------------------------------
# Judge
# ---------------------------------------------------------------------------


async def judge(case: dict[str, Any], result: dict[str, Any], model: str, api_key: str | None) -> dict[str, Any]:
    import litellm

    calls = "\n".join(
        f"- {c['name']} {json.dumps(c['arguments'], ensure_ascii=False)[:300]}" for c in result["tool_calls"]
    )
    prompt = (
        "You grade one turn of an AI assistant (Raven) that can read and change its own configuration "
        "with a raven_config tool and connect integrations with a plugin tool. Changes need user approval.\n\n"
        f"History before the turn: {json.dumps(case.get('history') or [], ensure_ascii=False)}\n"
        f"User message: {case['message']}\n"
        f"Tool calls made:\n{calls or '(none)'}\n"
        f"Questions Raven asked the user: {json.dumps(result['questions'], ensure_ascii=False)[:1500]}; "
        f"each was answered with: {case.get('answer') or _NO_ANSWER}\n"
        f"Approvals asked and the harness's answers (a raven_config answer stands for the user's): "
        f"{json.dumps(result['approvals'], ensure_ascii=False)[:1500]}\n"
        f"Final reply:\n{result['reply']}\n\n"
        f"Rubric: {case.get('rubric', '')}\n\n"
        "Model and product names come from Raven's live configuration and provider catalogs and may be "
        "newer than your training data; do not treat an unfamiliar name as fabricated. A change that went "
        "through an approval prompt was confirmed or refused by the user through that prompt.\n\n"
        'Answer with JSON only: {"pass": true|false, "reason": "<one sentence>"}'
    )
    response = await litellm.acompletion(
        model=model,
        api_key=api_key,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
    )
    content = response.choices[0].message.content or ""
    start, end = content.find("{"), content.rfind("}")
    try:
        verdict = json.loads(content[start : end + 1])
    except ValueError:
        verdict = {"pass": False, "reason": f"unparseable judge output: {content[:200]}"}
    return {"pass": bool(verdict.get("pass")), "reason": str(verdict.get("reason", ""))}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _spawn(case: dict[str, Any], config: dict[str, Any], work: Path, timeout_s: float) -> subprocess.Popen:
    home = work / case["id"]
    (home / "workspace").mkdir(parents=True)
    (home / "config.json").write_text(json.dumps(deep_merge(config, case.get("config") or {}), indent=2))
    env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED_ENV}
    env["RAVEN_HOME"] = str(home)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT), env.get("PYTHONPATH")]))
    browsers = Path.home() / "Library" / "Caches" / "ms-playwright"
    if browsers.is_dir():
        env.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browsers))
    out = home / "result.json"
    log = (home / "run.log").open("w")
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "benchmarks.self_config_eval.run",
            "--child",
            case["id"],
            "--out",
            str(out),
            "--timeout",
            str(timeout_s),
        ],
        cwd=home / "workspace",
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--case", action="append", help="run only these case ids")
    ap.add_argument("--kind", action="append", help="run only these kinds")
    ap.add_argument("--source-config", type=Path, default=Path.home() / ".raven" / "config.json")
    ap.add_argument("--model")
    ap.add_argument("--provider")
    ap.add_argument("--judge-model", help="LiteLLM model id for the rubric judge; omit to skip judging")
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--timeout", type=float, default=420.0)
    ap.add_argument("--child", help=argparse.SUPPRESS)
    ap.add_argument("--out", type=Path, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.child:
        child(args.child, args.out, args.timeout)
        return 0

    from benchmarks.self_config_eval.scoring import ToolCall, score

    cases = [
        c for c in load_cases() if (not args.case or c["id"] in args.case) and (not args.kind or c["kind"] in args.kind)
    ]
    config = base_config(args.source_config, args.model, args.provider)
    work = Path(tempfile.mkdtemp(prefix="self-config-eval-"))
    pending = list(cases)
    running: dict[str, tuple[subprocess.Popen, float]] = {}
    while pending or running:
        while pending and len(running) < args.parallel:
            case = pending.pop(0)
            running[case["id"]] = (_spawn(case, config, work, args.timeout), time.monotonic())
        for cid, (proc, began) in list(running.items()):
            if proc.poll() is None and time.monotonic() - began > args.timeout + 120:
                proc.kill()
                proc.wait()
            if proc.poll() is not None:
                del running[cid]
                print(f"  finished {cid} (exit {proc.returncode})", flush=True)
        time.sleep(0.5)

    judge_key = None
    if args.judge_model:
        judge_key = (config["providers"].get(args.judge_model.split("/", 1)[0]) or {}).get("apiKey")

    rows = []
    for case in cases:
        out = work / case["id"] / "result.json"
        if not out.exists():
            rows.append({"id": case["id"], "kind": case["kind"], "passed": False, "failures": ["no result (crashed)"]})
            continue
        result = json.loads(out.read_text(encoding="utf-8"))
        calls = [ToolCall(c["name"] or "", c["arguments"]) for c in result["tool_calls"]]
        verdict = score(case.get("expect") or {}, calls, result.get("config_after"))
        row = {
            "id": case["id"],
            "kind": case["kind"],
            "passed": verdict.passed and not result.get("error"),
            "failures": verdict.failures + ([result["error"]] if result.get("error") else []),
            "tools": [
                f"{c['name']}:{c['arguments'].get('action', '')}:{c['arguments'].get('path', '')}"
                for c in result["tool_calls"]
            ],
            "approvals": len(result["approvals"]),
            "reply": result["reply"],
            "seconds": result["seconds"],
        }
        if args.judge_model:
            row["judge"] = asyncio.run(judge(case, result, args.judge_model, judge_key))
            row["passed"] = row["passed"] and row["judge"]["pass"]
        rows.append(row)

    RESULTS.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    report = RESULTS / f"run-{stamp}.json"
    report.write_text(json.dumps({"model": config["agents"]["defaults"], "rows": rows}, ensure_ascii=False, indent=2))

    print()
    for row in rows:
        mark = "PASS" if row["passed"] else "FAIL"
        print(f"{mark}  [{row['kind']:<8}] {row['id']}")
        for failure in row.get("failures", []):
            print(f"        - {failure}")
        if "judge" in row and not row["judge"]["pass"]:
            print(f"        - judge: {row['judge']['reason']}")
    by_kind: dict[str, list[bool]] = {}
    for row in rows:
        by_kind.setdefault(row["kind"], []).append(row["passed"])
    print()
    for kind, results in by_kind.items():
        print(f"{kind:<9} {sum(results)}/{len(results)}")
    print(f"total     {sum(r['passed'] for r in rows)}/{len(rows)}   report: {report}   homes: {work}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
