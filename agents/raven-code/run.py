#!/usr/bin/env python
"""Host-side launcher for the Raven-Code ACP server -- the B side.

The vendored Raven-Code (subagents/raven-code) carries a whole fork
checkout; this product carries its assets only. It renders its config the
way every agents/ product does -- secrets merged into a 0600 copy whose
parent decides the data dir, the workspace pinned under the state root --
but the process it starts is still the fork checkout's own engine. The
trunk runtime cannot serve this product yet: the first-write approval
gate, the exec workbench and the rest of the fork's coding conduct land
as the code-flow plugin over the tool-gate, session-observer and
compaction seams, and until that plugin and its ported gate tests are
green, swapping the exec target would install a coding agent with no
approval gate at all. The swap to ``python -m raven acp`` is its own
later wave; the rendered config already loads on both engines so that
swap changes one exec line, not this file's shape.

The launch contract is the fork launcher's: refuse without any LLM key,
arm the fork's first-write workspace gate through the environment, seed
the fork's TOOLS.md wording into the workspace once, hand stdio to the
engine untouched. stdout belongs to the protocol; every diagnostic goes
to stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from raven.config import product_render as render
from raven.home import raven_home

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"
# The fork engine this launcher still hosts; retired at the exec-target swap.
DEFAULT_CHECKOUT = HERE.parent.parent / "subagents" / "raven-code" / "Raven-main"
FLOW_PLUGIN_ID = "code-flow"
GUIDE = HERE / "TOOLS_CODE.md"

PRODUCT = "raven-code"

# Where each secret belongs in the config the engine loads. The paths spell
# the same slots on the fork and trunk schemas; keys stay out of config.json
# because that file is published.
SECRET_SLOTS = {
    "CODE_API_KEY": ("providers", "custom", "apiKey"),
    "CODE_SERPER_API_KEY": ("tools", "web", "search", "apiKey"),
    "CODE_JINA_API_KEY": ("tools", "web", "jinaApiKey"),
}

# The LLM key never inherits per-slot: its absence switches the whole LLM
# block to host inheritance (see render_config), so it stays out of the
# optional fallback loop.
REQUIRED_SECRETS = ("CODE_API_KEY",)

_DEFAULT_HOST_WORKSPACE = "~/.raven/workspace"


def env_value(name: str) -> str | None:
    """This product's settings lookup: the process environment, then ``.env``."""
    return render.env_value(name, env_file=HERE / ".env")


def host_agent_home() -> Path:
    """The Agent home the host raven is configured to use.

    Read from the host config rather than assumed: an operator who moved
    ``agents.defaults.workspace`` moved every product's state with it, and a
    launcher pinning ``raven_home()/workspace`` regardless would silently
    relocate this product's sessions. The schema-default spelling resolves to
    the place it means.
    """
    raw = ((render.host_config().get("agents") or {}).get("defaults") or {}).get("workspace")
    if isinstance(raw, str) and raw.strip():
        configured = raw.strip()
        if configured == _DEFAULT_HOST_WORKSPACE:
            return raven_home() / "workspace"
        return Path(configured).expanduser()
    return raven_home() / "workspace"


def state_root() -> Path:
    """Everything this product persists lands here, never in this folder."""
    raw = env_value("CODE_STATE_ROOT")
    if raw:
        override = Path(raw).expanduser()
        return override if override.is_absolute() else host_agent_home() / override
    return host_agent_home() / "subagent_sessions" / PRODUCT


def log(message: str) -> None:
    """Record a diagnostic without contaminating the protocol stream."""
    print(message, file=sys.stderr, flush=True)


def seed_guide(workspace: Path) -> None:
    """Seed the workspace TOOLS.md with the fork's own wording, once.

    Both engines write workspace templates only for files still missing, so
    seeding first changes nothing today -- the fork engine would have written
    the same bytes -- and preserves the fork's tool guidance (exec sessions,
    background jobs, the 30k spill) across the exec-target swap, when the
    trunk template that would otherwise land says none of it.
    """
    render.seed_once(workspace / "TOOLS.md", lambda: GUIDE.read_text(encoding="utf-8"))


def render_config(source: Path) -> Path:
    """Write a copy of ``source`` with the secrets merged in, under the acp partition."""
    config = json.loads(source.read_text(encoding="utf-8"))
    host = render.host_config()

    render.apply_secret_slots(config, host, slots=SECRET_SLOTS, required=REQUIRED_SECRETS, lookup=env_value)

    llm_key = REQUIRED_SECRETS[0]
    if env_value(llm_key):
        defaults = config.get("agents", {}).get("defaults", {})
        log(f"[run] llm: own key (provider={defaults.get('provider')} model={defaults.get('model')})")
    else:
        taken = render.inherit_llm(config, host)
        if not taken:
            raise SystemExit(
                f"error: {llm_key} is not set and the host config has no provider key to "
                f"inherit from; put the key in {HERE / '.env'} (see .env.example), export "
                f"it, or configure a provider in the host raven"
            )
        log(f"[run] llm: inherited from the host ({taken})")

    acp_state = (state_root() / "acp").resolve()
    defaults = config.setdefault("agents", {}).setdefault("defaults", {})
    # The Agent home is the state partition itself, the fork launcher's pin:
    # the data dir follows the rendered file's parent, and the ACP session
    # Working directory stays the separate path repository work happens in.
    defaults["workspace"] = str(acp_state)

    flow_slice = config.setdefault("plugins", {}).setdefault("config", {}).setdefault(FLOW_PLUGIN_ID, {})
    flow_slice.setdefault(
        "workspaceGate",
        {
            "allocBase": str(acp_state),
            "reposRoot": str(state_root() / "repos"),
            "stateBucket": "acp",
        },
    )
    # No plugins.dirs yet: the fork engine consuming this render refuses the
    # key outright (its plugins block forbids fields it does not know), and
    # there is no plugin directory to declare until the code-flow plugin
    # lands. The declaration boards with the exec-target swap.

    acp_state.mkdir(parents=True, exist_ok=True)
    seed_guide(acp_state)
    render.sweep_stale_renders(acp_state)
    return render.write_rendered(config, acp_state)


def serve(args: argparse.Namespace) -> int:
    """Host the fork engine's ``raven acp`` on this process's stdio.

    The venv precheck comes before the render, the fork launcher's order: a
    missing engine is the answer whoever installed this needs first, and no
    file holding merged secrets should exist for a run that cannot start.
    The three env vars are the fork's gate-arming contract -- without them
    the vendored first-write gate stays unarmed and Raven-Code writes
    wherever it lands; the plugin slice rendered beside them takes over when
    the gate becomes the code-flow plugin.
    """
    checkout = Path(args.checkout).expanduser().resolve()
    raven_bin = checkout / ".venv" / "bin" / "raven"
    if not raven_bin.is_file():
        raise SystemExit(f"error: venv missing at {raven_bin}; run `uv sync` in {checkout}")

    rendered = render_config(Path(args.config).resolve())
    env = dict(os.environ)
    env["RAVEN_WORKSPACE_ALLOC_BASE"] = str(rendered.parent)
    env["RAVEN_WORKSPACE_ALLOC_REPOS"] = str(state_root() / "repos")
    env.setdefault("RAVEN_WORKSPACE_STATE_BUCKET", "acp")
    log(f"[run] acp: serving on stdio under {rendered} (engine {raven_bin})")
    try:
        proc = subprocess.Popen([str(raven_bin), "acp", "--config", str(rendered)], cwd=str(checkout), env=env)
        return proc.wait()
    finally:
        # The rendered copy holds the merged secrets at 0600; a served
        # session ending must not leave it behind. A SIGKILLed process group
        # never reaches this line, which is what the pid-liveness sweep in
        # render_config is for.
        rendered.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve Raven-Code over ACP on stdio.")
    # The roster row's command carries --acp, kept byte-identical to the
    # vendored row; ACP is this launcher's only hosting, so the flag selects
    # nothing. The one-turn CLI adjudication hosting returns with the
    # exec-target swap, aimed at the installed raven's own CLI.
    parser.add_argument("--acp", action="store_true", help="serve ACP on stdio (the only hosting)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--checkout", default=str(DEFAULT_CHECKOUT), help="fork checkout hosting the engine")
    args = parser.parse_args()
    return serve(args)


if __name__ == "__main__":
    sys.exit(main())
