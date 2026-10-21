#!/usr/bin/env python3
"""Drive the research agent headlessly, over ACP, and print what it answered.

The research agent already had a headless runner: ``run.py`` serves it over
ACP on stdio. What it did not have was a way to **ask it something** without
a human at the keyboard, which is what a benchmark arm needs.

This is that way. It renders the agent's config, serves the agent, drives one
``session/prompt`` over the ACP client, and prints the answer. The agent is
the real one - the same launcher, the same gates, the same report shape - so
what comes back is what a person would have got.

    python3 run_research.py --task "what does X cost, and in what unit?"
    python3 run_research.py --task "..." --mode max --traces /tmp/traces

The search vendor is whatever the agent's own configuration names, and the
launcher refuses to start without a key for it. ``RESEARCH_WEB_SEARCH_PROVIDER``
picks it; the key comes from ``RESEARCH_<VENDOR>_API_KEY``, for whichever
vendor is picked - TinyFish among them.

The frames are journalled, so a run leaves the same trace a person-driven
turn leaves, and the corpus builder can attest to it like any other.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType

from raven.contracts.path_policy import HOME_ENV_VAR
from raven.home import raven_home

HERE = Path(__file__).resolve().parent
#: The launcher this drives. It is the agent's own entry point; nothing here
#: reimplements it.
LAUNCHER = HERE / "run.py"
#: Where the agent's rendered config, workspace and flow state land.
#:
#: **Not inside the repository.** The launcher writes the rendered config,
#: the workspace and the flow's own state under this root, and a run's
#: state is not source. The launcher's own default is the product's slot
#: under the host home; this keeps that, and lets ``RESEARCH_NG_STATE_ROOT``
#: override it for an isolated run.
DEFAULT_STATE_ROOT = Path(
    os.environ.get("RESEARCH_NG_STATE_ROOT")
    or (Path.home() / ".raven" / "workspace" / "subagent_sessions" / "raven-research-ng")
)


@functools.lru_cache(maxsize=1)
def launcher() -> ModuleType:
    """The launcher, loaded as a module.

    The launcher is a script beside this one, and the driver reads the names
    of the settings it honours from it rather than keeping a list of its own:
    a vendor added to the launcher's table then reaches the agent without an
    edit here.
    """
    spec = importlib.util.spec_from_file_location("raven_research_launcher", LAUNCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def agent_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment the agent process is given.

    The launch starts from the login shell's environment, which holds what
    the user's profile exports and nothing a caller exported in the process
    that started the driver. So the driver hands over the settings the
    launcher reads, and the launcher's own tables say which ones those are:
    the secret slots (one name per web vendor, so the SELECTED vendor's key
    arrives with the selection), the provider and proxy names, and this
    product's own state names. No vendor is named here, so no vendor is
    special.

    The host's home travels too, because the launcher inherits the host's
    model from it, and the OAuth token directory follows it: a relocated
    home finds the token the host signed in with.
    """
    env = {name: os.environ[name] for name in launcher().LAUNCH_ENV_VARS if os.environ.get(name)}
    if home := os.environ.get(HOME_ENV_VAR, "").strip():
        env[HOME_ENV_VAR] = home
    env["MINIMAX_OAUTH_TOKEN_DIR"] = str(raven_home() / "oauth")
    env.update(extra or {})
    return env


def _write_config(path: Path) -> Path:
    """A config that names the search vendor the caller asked for.

    The launcher reads the vendor from the environment, so this is only the
    fallback for a caller that sets it in the config instead.
    """
    source = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(source, indent=2), encoding="utf-8")
    return path


async def _drive(task: str, env: dict[str, str], traces: Path, mode: str | None) -> str:
    from raven.acp_client.acp_agent import AcpAgentBackend
    from raven.acp_client.pool import close_pool

    backend = AcpAgentBackend(
        name="research-agent",
        command=f"{sys.executable} {LAUNCHER}",
        cwd=str(HERE),
        env=env,
        ready_timeout_ms=180_000,
        timeout=3600,
    )
    workspace = DEFAULT_STATE_ROOT / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        return await backend.run(
            task,
            task_id="research-run",
            workspace=workspace,
            executor=None,
            session_key="research:run",
            mode=mode,
        )
    finally:
        await close_pool()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--task", required=True, help="the question to ask")
    parser.add_argument("--mode", default=None, help="the agent's mode: medium, high or max")
    parser.add_argument("--traces", default="/tmp/research-agent-traces", help="where the frames are journalled")
    args = parser.parse_args()

    traces = Path(args.traces)
    traces.mkdir(parents=True, exist_ok=True)
    os.environ["RAVEN_TRACING_DIR"] = str(traces)

    env = agent_env({"RAVEN_TRACING_DIR": str(traces)})
    env = {k: v for k, v in env.items() if v}

    print(asyncio.run(_drive(args.task, env, traces, args.mode)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
