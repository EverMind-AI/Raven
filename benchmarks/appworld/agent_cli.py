"""Run ONE AppWorld task through a minimal Raven AgentLoop, then grade.

Cross-venv design (see tool.py): AppWorld (pydantic v1) runs as an HTTP
``environment`` server; this runner (Raven, pydantic v2) never imports
appworld — it drives the task purely over HTTP:

  POST {env}/initialize {task_id}      -> instruction + supervisor (loads world)
  agent's `execute` tool -> POST {env}/execute {task_id, code}
  POST {env}/evaluate {task_id}        -> TestTracker dict (success = pass@1)
  POST {env}/task_completed, /close

One env server holds one world at a time, so the batch runner gives each
concurrent task its own server/port (--env-url). The harness is plain vanilla:
the agent gets only the `execute` tool and the base AppWorld prompt.

Usage::

    python -m benchmarks.appworld.agent_cli \
        --task-id 82e2fac_1 --env-url http://127.0.0.1:8100 \
        --config <subject_cfg.json> --out result.json [--experiment vanilla]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

from benchmarks.appworld.evolve import grade

APPWORLD_PROMPT = """You are an autonomous agent that completes a digital task for your supervisor by writing and running Python code.

You act ONLY through the `execute` tool: you write Python, it runs in a stateful REPL (variables, imports and logins persist across calls, like a Jupyter notebook), and you get back stdout. You MUST print() anything you want to observe.

How to work:
- See the apps:            print(apis.api_docs.show_app_descriptions())
- List an app's APIs:      print(apis.api_docs.show_api_descriptions(app_name='spotify'))
- Read one API's doc:      print(apis.api_docs.show_api_doc(app_name='spotify', api_name='login'))
- Your supervisor's data:  apis.supervisor.show_profile(), show_account_passwords(), show_addresses(), show_payment_cards()
- Most APIs need an access_token. Log in first, e.g.:
      pwds = apis.supervisor.show_account_passwords()
      token = apis.spotify.login(username=..., password=...)["access_token"]
- Go step by step: inspect first, then act, then verify before finalizing.

When the task is complete, call exactly once:
- A question  ->  apis.supervisor.complete_task(answer=<your answer>)
- An action   ->  apis.supervisor.complete_task()

Your supervisor: {supervisor}

Your task:
{instruction}
"""


def _build_agent(args):
    from raven.agent.loop import AgentLoop
    from raven.cli._helpers import load_runtime_config, make_provider
    from raven.config.raven import load_raven_config
    from raven.session.manager import SessionManager

    from benchmarks.appworld.tool import AppWorldExecuteTool

    config = load_runtime_config(args.config, args.workspace)
    ec_config = load_raven_config()
    provider = make_provider(config)

    # AppWorld is pure API/code: disable every default tool, give only `execute`.
    disabled = [
        "read_file", "write_file", "edit_file", "list_dir",
        "exec", "web_search", "web_fetch", "message", "spawn", "cron",
    ]
    agent = AgentLoop(
        provider=provider,
        workspace=config.workspace_path,
        model=args.model or config.agents.defaults.model,
        max_iterations=config.agents.defaults.max_tool_iterations,
        context_window_tokens=config.agents.defaults.context_window_tokens,
        exec_config=config.tools.exec,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        session_manager=SessionManager(config.workspace_path),
        context_config=ec_config.context,
        hooks=None,
        disabled_tools=disabled,
    )
    exec_tool = AppWorldExecuteTool(args.env_url, args.task_id)
    agent.tools.register(exec_tool)
    return agent, exec_tool


async def _run(args) -> dict:
    # Grading, infra classification, and the result write live in
    # benchmarks.appworld.evolve.grade (the evolver-immutable scorer);
    # this file is the editable agent surface and must never grade itself.
    t0 = time.time()
    result: dict = {"task_id": args.task_id, "experiment": args.experiment}
    try:
        init = grade.post(args.env_url, "/initialize",
                     # unique experiment_name per attempt -> isolated experiments/outputs/<...>/tasks/<task>/
                     # dir, so concurrent K-trials of the same task never collide on model_hashes.json (Errno 22).
                     {"task_id": args.task_id, "experiment_name": (args.session or args.experiment)})
        prompt = APPWORLD_PROMPT.format(
            supervisor=init.get("supervisor"), instruction=init.get("instruction"))
        agent, exec_tool = _build_agent(args)
        skey = args.session or args.task_id
        try:
            # Raven's AgentLoop is spine-driven (no process_direct): drive one headless
            # turn through run_turn with no-op emit/drain and capture the final reply via
            # text_sink. AppWorld success is judged by the env oracle (/evaluate), not this
            # text; it is kept only for transport-error detection and the result record.
            from raven.spine import ChatType, Origin, Source, TurnRequest

            async def _emit(_event):
                return None

            def _drain():
                return []

            sink: dict = {}
            req = TurnRequest(
                origin=Origin.USER,
                source=Source(channel="cli", chat_id="direct",
                              sender_id="user", chat_type=ChatType.DM),
                # Raven's SessionManager splits the conversation key on ':' into
                # <channel>/<chat_id>.jsonl. Prefix a fixed channel so the per-
                # attempt transcript lands at a clean flat path the evolver reads:
                # ws/sessions/appworld/<tid>_<exp>_k<k>.jsonl.
                text=prompt, conversation=f"appworld:{skey}",
            )
            await agent.run_turn(req, _emit, _drain, stream=False, text_sink=sink)
            response = sink.get("text") or ""
        finally:
            await agent.close_executor()
            client = getattr(getattr(agent, "provider", None), "_client", None)
            closer = getattr(client, "close", None)
            if closer is not None:
                try:
                    await closer()
                except Exception:
                    pass
        grade.grade_and_record(
            result, env_url=args.env_url, task_id=args.task_id,
            response=response, config_path=args.config, t0=t0,
        )
    except BaseException as e:
        grade.record_infra(result, e, t0=t0)
    finally:
        try:
            grade.post(args.env_url, "/close", {"task_id": args.task_id})
        except Exception:
            pass
    return result


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="appworld-agent")
    p.add_argument("--task-id", required=True)
    p.add_argument("--env-url", required=True, help="AppWorld environment server base URL.")
    p.add_argument("--config", required=True, help="Raven runtime config JSON.")
    p.add_argument("--out", required=True, help="Where to write the result JSON.")
    p.add_argument("--workspace",
                   default=os.path.expanduser("~/workspace/appworld-run/ws"))
    p.add_argument("--model", default=None)
    p.add_argument("--experiment", default="vanilla")
    p.add_argument("--session", default=None,
                   help="Session key (jsonl stem). Default task_id; pass a per-attempt "
                        "key to retain all K trajectories instead of overwriting.")
    args = p.parse_args(argv)

    import contextlib
    try:
        import litellm
        litellm.suppress_debug_info = True
    except Exception:
        pass

    with contextlib.redirect_stdout(sys.stderr):
        result = asyncio.run(_run(args))

    grade.write_result(args.out, result)
    sys.stderr.write(f"[appworld] {args.task_id} success={result.get('success')} "
                     f"infra={result.get('infra_error')} t={result.get('elapsed_s')}s\n")
    return 0


if __name__ == "__main__":
    import os
    os._exit(main())
