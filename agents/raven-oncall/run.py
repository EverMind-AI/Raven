#!/usr/bin/env python
"""Host-side launcher for the Raven-Oncall ACP server -- the B side.

The vendored Raven-Oncall (subagents/raven-oncall) carries a whole fork
checkout; this product carries none. It renders its config and execs the
installed raven's own ``raven acp``, so every turn runs through the same
assembly door (build_runtime) as the host's TUI and gateway. The machinery
of rendering lives in the launcher library
(``raven.config.product_render``); what remains here is this product's own
half -- its tables (which secrets go where) and its judgement (refusing to
launch without any LLM key, pointing the machine registry at the owner's
file, seeding the on-call guide into the workspace).

The rendered copy lands under the state root and the location is
load-bearing: raven derives its data dir from the config file's own parent,
so transcripts, cache and logs follow the rendered file. The workspace is
pinned under the same root -- the schema default is the host raven's own
workspace, which this agent must not share.

What the vendored launcher also hosted does not come back here: the
resident wake shell's duties pass to resident trunk hosts and the product
plugin's contributions, and the one-turn CLI adjudication path retires with
its only callers (the wake shell and the fork benchmarks). This launcher
serves ACP, nothing else.

After rendering, this process execs ``python -m raven acp`` on its own
interpreter (the row's ``{PYTHON}`` resolves at install time to one that
imports raven), so the server inherits this pid, process group and stdio.
stdout belongs to the protocol; every diagnostic goes to stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from raven.config import product_render as render
from raven.home import raven_home

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"
PLUGINS_DIR = HERE / "plugins"
FLOW_PLUGIN_ID = "oncall-flow"
GUIDE_SECTION = PLUGINS_DIR / FLOW_PLUGIN_ID / "prompts" / "TOOLS_ONCALL.md"
MODES_DIR = HERE / "modes"
# The shipped config.json IS the high profile; modes/ holds the other two as
# diffs. Measured 2026-09-04 on deepseek-v4-flash over openrouter: the model
# does not tell low, medium and high apart (200-340 thinking tokens, 6-17 s),
# so the felt steps are thinking off, thinking on, and thinking at max
# (2300-3300 tokens, about 70 s a call). Three profiles, one real knob.
BASELINE_MODE = "high"
MODE_LABELS = {
    "medium": (
        "Medium",
        "Thinking off: answers in seconds. For routine looks -- reading a status, polling a queue.",
    ),
    "high": (
        "High",
        "Thinking on, the shipped default: a few seconds to a quarter minute a call.",
    ),
    "max": (
        "Max",
        "Thinking at maximum effort: about a minute a call, ten times the reasoning. For a judgement that "
        "decides a round -- what to submit, whether to kill.",
    ),
}
# An overlay may touch the agent's own generation defaults and the flow
# plugin's slice; anything else is a different file's business.
OVERLAY_KEYS = frozenset({"agents", "plugins"})

PRODUCT = "raven-oncall"

# Where each secret belongs in the config raven loads. The paths are trunk
# raven's own config surface; keys stay out of config.json because that file
# is published.
SECRET_SLOTS = {
    "ONCALL_API_KEY": ("providers", "custom", "apiKey"),
    "ONCALL_SERPER_API_KEY": ("tools", "web", "search", "apiKey"),
    "ONCALL_JINA_API_KEY": ("tools", "web", "jinaApiKey"),
}

# The LLM key never inherits per-slot: its absence switches the whole LLM
# block to host inheritance (see render_config), so it stays out of the
# optional fallback loop.
REQUIRED_SECRETS = ("ONCALL_API_KEY",)

# The env var trunk's raven.ops.connections honours as its store override; a
# literal so the launcher imports nothing beyond the launcher library and the
# path paper. Pinned against raven.ops.connections.CONNECTIONS_ENV by
# tests/test_agents_oncall_launcher.py.
CONNECTIONS_ENV = "RAVEN_CONNECTIONS"


def env_value(name: str) -> str | None:
    """This product's settings lookup: the process environment, then ``.env``."""
    return render.env_value(name, env_file=HERE / ".env")


def state_root() -> Path:
    """Everything this product persists lands here, never in this folder."""
    return render.product_state_root(PRODUCT, override=env_value("ONCALL_STATE_ROOT"))


def log(message: str) -> None:
    """Record a diagnostic without contaminating the protocol stream."""
    print(message, file=sys.stderr, flush=True)


def connections_registry() -> Path:
    """The machine registry this install reads: the owner's, not a copy of it.

    Without an answer, ``raven.ops.connections`` resolves its path beside the
    rendered config -- which is the state root here -- so the on-call tools
    would see no machines at all, and a task naming a remote path would be
    read as naming a local one. A pointer rather than a copy: a copy is wrong
    from the first machine the owner adds, with nothing to say so. An install
    that predates this and keeps its own list stays on it.
    """
    host = raven_home() / "connections.json"
    if host.is_file():
        return host
    own = state_root() / "connections.json"
    return own if own.is_file() else host


def seed_guide(workspace: Path) -> None:
    """Seed the workspace TOOLS.md with the on-call section already in place.

    The vendored twin appended TOOLS_ONCALL.md to the workspace TOOLS.md on
    every sync while its gate was on; here the composed file is seeded once,
    before first launch, so trunk's own template sync finds it existing and
    leaves it alone. Once is the contract: the workspace copy is the live one
    afterwards, and a product update must not silently overwrite what an
    operator tuned in place. The composition mirrors the twin's append --
    template body, blank line, section -- so the model-visible text is
    byte-equal to what the fork produced on a fresh workspace.
    """
    render.seed_once(workspace / "TOOLS.md", _guide_text)


def _guide_text() -> str:
    from raven import templates

    base = (Path(templates.__file__).resolve().parent / "TOOLS.md").read_text(encoding="utf-8")
    return base.rstrip("\n") + "\n\n" + GUIDE_SECTION.read_text(encoding="utf-8")


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
    # The engine's Agent home must sit OUTSIDE the host Agent home (the host
    # hands its home over as the session cwd, and the runtime refuses a cwd
    # that contains the engine's home), so the default comes from the shared
    # placement helper -- the raven data directory, ONCALL_ACP_HOME overrides.
    # The shipped config spells the default as the literal "workspace" (the
    # fork's own spelling); that sentinel means "the default", wherever it
    # should live today. An operator's own value is honoured as written
    # (relative paths keep resolving under the state root, their documented
    # shape); the state root itself still holds the work (oncall_flow store,
    # rendered configs).
    configured = defaults.get("workspace")
    if configured and configured != "workspace":
        workspace = Path(configured)
        if not workspace.is_absolute():
            workspace = (root / workspace).resolve()
    else:
        workspace = render.product_acp_home(PRODUCT, override=env_value("ONCALL_ACP_HOME"))
    defaults["workspace"] = str(workspace)

    plugins = config.setdefault("plugins", {})
    plugins["dirs"] = [str(PLUGINS_DIR)]
    flow_slice = plugins.setdefault("config", {}).setdefault(FLOW_PLUGIN_ID, {})
    flow_slice.setdefault("stateRoot", str(root / "oncall_flow"))

    catalogue = render.mode_catalogue(
        MODES_DIR,
        MODE_LABELS,
        baseline=BASELINE_MODE,
        overlay_keys=OVERLAY_KEYS,
        # The loop reads agents.defaults off the overlay itself and the plugin's
        # hooks read their slice, so the entry carries the whole diff; the cap
        # is the overlay's when it names one, else the shipped default.
        resolve=lambda overlay: (
            ((overlay.get("agents") or {}).get("defaults") or {}).get(
                "maxToolIterations", defaults.get("maxToolIterations")
            ),
            overlay,
        ),
    )
    if catalogue:
        acp = config.setdefault("acp", {})
        acp["modes"] = catalogue
        acp["defaultMode"] = BASELINE_MODE
        log(f"[run] modes: {', '.join(catalogue)} (default {BASELINE_MODE})")

    root.mkdir(parents=True, exist_ok=True)
    seed_guide(workspace)
    render.sweep_stale_renders(root)
    return render.write_rendered(config, root)


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve Raven-Oncall over ACP on stdio.")
    # The roster row's command carries --acp, kept byte-identical to the
    # vendored row; ACP is this launcher's only hosting, so the flag selects
    # nothing.
    parser.add_argument("--acp", action="store_true", help="serve ACP on stdio (the only hosting)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    args = parser.parse_args()

    rendered = render_config(Path(args.config).resolve())
    os.environ.setdefault(CONNECTIONS_ENV, str(connections_registry()))
    log(f"[run] exec {sys.executable} -m raven acp (config {rendered})")
    os.execv(sys.executable, [sys.executable, "-m", "raven", "acp", "--config", str(rendered)])
    raise AssertionError("unreachable: execv does not return")


if __name__ == "__main__":
    sys.exit(main())
