#!/usr/bin/env python
"""Host-side launcher for the My-Agent ACP server.

Renders this agent's config.json (secrets merged, LLM inherited from the
host when the agent holds no key of its own, state root and plugin roots
pinned) and execs the installed raven's own ``raven acp``, so every turn runs
through the same assembly door as the host's TUI and gateway. stdout belongs
to the protocol; every diagnostic goes to stderr.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

from raven.config import product_render as render
from raven.contracts.path_policy import WORKSPACE_DEFAULT_SENTINEL

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"
PLUGINS_DIR = HERE / "plugins"
FLOW_PLUGIN_ID = "my-agent-flow"
AGENT = "my-agent"

# The import package this agent's harness ships as when it ships as a wheel
# (the raven-design / raven-ppt shape) instead of the plugins/ directory.
# Empty means the plugins/ directory IS the harness; the scaffold's
# --engine-wheel variant fills it in.
ENGINE_PACKAGE = ""

# Where each secret belongs in the config raven loads. Keys stay out of
# config.json because that file is published.
SECRET_SLOTS = {
    "MY_AGENT_API_KEY": ("providers", "custom", "apiKey"),
}

# The LLM key never inherits per-slot: its absence switches the whole LLM
# block to host inheritance (see render_config).
REQUIRED_SECRETS = ("MY_AGENT_API_KEY",)


def env_value(name: str) -> str | None:
    """This agent's settings lookup: the process environment, then ``.env``."""
    return render.env_value(name, env_file=HERE / ".env")


def state_root() -> Path:
    """Everything this agent persists lands here, never in this folder."""
    return render.product_state_root(AGENT, override=env_value("MY_AGENT_STATE_ROOT"))


def log(message: str) -> None:
    """Record a diagnostic without contaminating the protocol stream."""
    print(message, file=sys.stderr, flush=True)


def render_config(source: Path) -> Path:
    """Write a copy of ``source`` with the secrets merged in, under the state root."""
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

    root = state_root()
    defaults = config.setdefault("agents", {}).setdefault("defaults", {})
    # The engine's Agent home must sit OUTSIDE the host Agent home; the shared
    # placement helper answers that. The schema-default spelling is the
    # paper's WORKSPACE_DEFAULT_SENTINEL and means "the default, wherever it
    # should live today" -- carried explicitly it resolves exactly as when
    # omitted, never as a literal path into the host Agent home.
    configured = str(defaults.get("workspace") or "").strip()
    if configured and configured != WORKSPACE_DEFAULT_SENTINEL:
        # An explicit path is the operator's own placement call, honored
        # verbatim: the containment guard (never inside the host Agent home)
        # lives in product_acp_home's default branch below -- the same shape
        # as the shipped launchers. An explicit value inside the host Agent
        # home will make every dispatch refuse at runtime.
        workspace = Path(configured).expanduser()
        if not workspace.is_absolute():
            workspace = (root / workspace).resolve()
    else:
        workspace = render.product_acp_home(AGENT, override=env_value("MY_AGENT_ACP_HOME"))
    defaults["workspace"] = str(workspace)

    plugins = config.setdefault("plugins", {})
    if not ENGINE_PACKAGE:
        # A wheel-shaped harness is found through the raven.plugins
        # entry-point group; only the directory-shaped one needs a root.
        plugins["dirs"] = [str(PLUGINS_DIR)]
    flow_slice = plugins.setdefault("config", {}).setdefault(FLOW_PLUGIN_ID, {})
    flow_slice.setdefault("stateRoot", str(root / FLOW_PLUGIN_ID.replace("-", "_")))

    root.mkdir(parents=True, exist_ok=True)
    render.sweep_stale_renders(root)
    return render.write_rendered(config, root)


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve My-Agent over ACP on stdio.")
    # The roster row's command carries --acp; ACP is this launcher's only
    # hosting, so the flag selects nothing.
    parser.add_argument("--acp", action="store_true", help="serve ACP on stdio (the only hosting)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    args = parser.parse_args()

    if ENGINE_PACKAGE and importlib.util.find_spec(ENGINE_PACKAGE) is None:
        raise SystemExit(
            f"error: the {FLOW_PLUGIN_ID} engine wheel is not installed in this environment "
            f"({sys.executable}); install it where raven is installed "
            f"(pip install -e <the {AGENT}-engine checkout>), then restart"
        )

    rendered = render_config(Path(args.config).resolve())
    log(f"[run] exec {sys.executable} -m raven acp (config {rendered})")
    os.execv(sys.executable, [sys.executable, "-m", "raven", "acp", "--config", str(rendered)])
    raise AssertionError("unreachable: execv does not return")


if __name__ == "__main__":
    sys.exit(main())
