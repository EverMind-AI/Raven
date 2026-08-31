#!/usr/bin/env python
"""Host-side launcher for the Raven-Research-NG ACP server -- the B side.

The vendored Raven-Research (subagents/raven-research) carries a whole fork
checkout; this product carries none. It renders its config and execs the
installed raven's own ``raven acp``, so every turn runs through the same
assembly door (build_runtime) as the host's TUI and gateway. The machinery
of rendering lives in the launcher library
(``raven.config.product_render``); what remains here is this product's own
half -- its tables (which secrets go where, what its modes are called, which
overlay keys exist) and its judgement (how a mode's budget resolves, the
flow prompts, refusing to launch without a search key).

The rendered copy lands under the state root and the location is
load-bearing: raven derives its data dir from the config file's own parent,
so transcripts, cache and logs follow the rendered file. The workspace is
pinned under the same root -- the schema default is the host raven's own
workspace, which this agent must not share.

Three deliberate differences from the vendored launcher, all of them the new
architecture doing the job the fork used to:

* the home is answered by the path paper (``raven.home``), never re-derived;
* identity is a workspace asset -- raven reads
  ``agent_memory/profile/soul.md``, so ``soul.md`` beside this file is
  copied there on first launch -- not a config override;
* modes are overlays: ``modes/*.json`` diffs become ``acp.modes`` entries a
  client's picker shows, applied per session over the baseline flow config
  instead of being compiled into the loop the way the fork did.

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

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"
IDENTITY_SOURCE = HERE / "soul.md"
PLUGINS_DIR = HERE / "plugins"
FLOW_PLUGIN_DIR = PLUGINS_DIR / "research-flow"
FLOW_PLUGIN_ID = "research-flow"
MODES_DIR = HERE / "modes"
BASELINE_MODE = "fast"

# What a client's mode picker shows. The name and blurb live here rather than in
# the overlay files so a mode's label cannot drift from its diff.
MODE_LABELS = {
    "fast": (
        "Fast",
        "Bounded budget; converges as soon as the evidence answers the question. "
        "The default, and right for an ordinary question.",
    ),
    "deep": (
        "Deep",
        "Keeps searching for longer before the early-convergence gate is consulted. "
        "For a multi-faceted topic one pass of evidence will not settle.",
    ),
    "ultra": (
        "Ultra",
        "No early-convergence gate; exhaustive retrieval. For a survey where missing a source is the failure mode.",
    ),
}
OVERLAY_KEYS = frozenset({"drFlow", "agents"})

PRODUCT = "raven-research-ng"

# Where each secret belongs in the config raven loads. The paths are trunk
# raven's own config surface (tools.web.search.apiKey / tools.web.jinaApiKey),
# not the fork's vendor table; the loader-round-trip test pins that they stay
# real fields. Keys stay out of config.json because that file is published.
SECRET_SLOTS = {
    "RESEARCH_API_KEY": ("providers", "openrouter", "apiKey"),
    "RESEARCH_SERPER_API_KEY": ("tools", "web", "search", "apiKey"),
    "RESEARCH_JINA_API_KEY": ("tools", "web", "jinaApiKey"),
}

# The LLM key never inherits per-slot: its absence switches the whole LLM
# block to host inheritance (see render_config), so it stays out of the
# optional fallback loop.
REQUIRED_SECRETS = ("RESEARCH_API_KEY",)

# Not a secret, but resolved the same way: the proxy both web tools dial
# through. With none here and none in the host config they connect direct.
PROXY_ENV = "RESEARCH_WEB_PROXY"
PROXY_SLOT = ("tools", "web", "proxy")

# The research-flow plugin REPLACES web_search and web_fetch, and a plugin
# factory is handed its own config slice and nothing else -- it never sees
# tools.web. So every value resolved above has to reach that slice too, under
# the names the plugin reads (raven-plugin.toml documents them). Without this
# the key an operator put in .env configures only the built-ins the plugin
# shadows: the launch succeeds, the tool is advertised, and every search comes
# back "API key not configured".
PLUGIN_WEB_MIRROR = {
    ("tools", "web", "search", "apiKey"): ("search", "apiKey"),
    ("tools", "web", "jinaApiKey"): ("fetch", "apiKey"),
    PROXY_SLOT: ("proxy",),
}

# The env var the search tool itself falls back to at call time; pinned
# against raven/agent/tools/web.py by tests/test_agents_research_launcher.py.
SEARCH_ENV_VAR = "SERPER_API_KEY"


def env_value(name: str) -> str | None:
    """This product's settings lookup: the process environment, then ``.env``."""
    return render.env_value(name, env_file=HERE / ".env")


def state_root() -> Path:
    """Everything this product persists lands here, never in this folder."""
    return render.product_state_root(PRODUCT, override=env_value("RESEARCH_NG_STATE_ROOT"))


def log(message: str) -> None:
    """Record a diagnostic without contaminating the protocol stream."""
    print(message, file=sys.stderr, flush=True)


def require_search(config: dict) -> None:
    """Refuse to launch when search has no key anywhere.

    Search is what this agent is for; withheld, the tool is absent and a run
    answers from the model's own memory -- which reads as an ordinary run, in
    the one failure mode nobody inspects. The bare env var counts because the
    tool resolves its key at call time from the config value or that var.
    """
    key = render.dig(config, ("tools", "web", "search", "apiKey"))
    if key or os.environ.get(SEARCH_ENV_VAR):
        return
    raise SystemExit(
        f"error: search has no key; put RESEARCH_SERPER_API_KEY in {HERE / '.env'} "
        f"(see .env.example) or export {SEARCH_ENV_VAR}"
    )


def seed_identity(workspace: Path, flow_slice: dict) -> None:
    """Seed the product identity into the workspace raven reads it from.

    Rendered, not copied: the flow inserts its measured guidance into the
    identity text the way the vendored twin did at prompt-build time, so the
    workspace copy is the model-visible text. Once (``seed_once``'s
    contract): the workspace copy is the live one afterwards, and a product
    update must not silently overwrite what an operator tuned in place.
    """
    render.seed_once(
        workspace / "agent_memory" / "profile" / "soul.md",
        lambda: rendered_identity(flow_slice),
    )


def _flow_prompts(flow_slice: dict):
    """Both prompt halves, rendered by the plugin that owns their text.

    The launcher only asks the plugin to render for this product's flow
    config, so what the model reads is what the gates enforce. Imported from
    the plugin directory the same way the runtime will import it once
    ``plugins.dirs`` names that directory.
    """
    if str(FLOW_PLUGIN_DIR) not in sys.path:
        sys.path.insert(0, str(FLOW_PLUGIN_DIR))
    from research_flow.config import FlowConfig
    from research_flow.prompts import render_identity_and_contract

    merged = dict(flow_slice)
    merged["identityOverride"] = IDENTITY_SOURCE.read_text(encoding="utf-8").rstrip("\n")
    return render_identity_and_contract(FlowConfig.from_slice(merged))


def rendered_identity(flow_slice: dict) -> str:
    """The identity as the model reads it: soul.md with the flow's insertions."""
    return _flow_prompts(flow_slice)[0]


def seed_contract(workspace: Path, flow_slice: dict) -> None:
    """Write the contract beside the identity, once, as ``agent.md``.

    Bootstrap renders both files in order: the identity from ``soul.md``, the
    contract from ``agent.md`` -- the two halves of the segment the fork built
    in code. Same once-only rule as the identity.
    """
    render.seed_once(
        workspace / "agent_memory" / "profile" / "agent.md",
        lambda: _flow_prompts(flow_slice)[1],
    )


def iteration_cap(base_flow: dict, base_cap, overlay: dict):
    """One mode's iteration budget, resolved the way the fork's loop resolved it.

    The fork let ``drFlow.maxIterations`` overwrite the loop's own cap
    (``AgentLoop.__init__``: ``self.max_iterations = self._dr_flow.max_iterations``),
    so one number both bounded the ReAct loop and told the model how much budget
    was left. Here they are two settings with two readers -- the loop enforces
    the mode's ``maxToolIterations``, the flow's budget note and spin breaker
    divide by ``drFlow.maxIterations`` -- and nothing joins them, so the model
    was told ``iteration 3/20`` on a turn the loop would let run to 40.
    Resolving it here ships one number per mode that both sides read.

    An explicit ``null`` arrives as a present key holding ``None``: a mode
    declining the flow's override, which is how ``ultra`` asks to run to its own
    ``maxToolIterations`` rather than the baseline's 20.
    """
    dr = overlay.get("drFlow") or {}
    flow_cap = dr["maxIterations"] if "maxIterations" in dr else base_flow.get("maxIterations")
    cap = ((overlay.get("agents") or {}).get("defaults") or {}).get("maxToolIterations") or base_cap
    return flow_cap or cap


def render_config(source: Path) -> Path:
    """Write a copy of ``source`` with the secrets merged in, under the state root.

    The composition is this product's; every machine it calls is the launcher
    library's. Order matters twice: the LLM check runs after the slot merge
    (its absence is what switches to inheritance), and the mode catalogue is
    assembled after the flow slice is final (a mode's budget resolves against
    the baseline flow config).
    """
    config = json.loads(source.read_text(encoding="utf-8"))
    host = render.host_config()

    render.apply_secret_slots(config, host, slots=SECRET_SLOTS, required=REQUIRED_SECRETS, lookup=env_value)
    if proxy := (env_value(PROXY_ENV) or render.dig(host, PROXY_SLOT)):
        render.put(config, PROXY_SLOT, proxy)

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

    require_search(config)

    root = state_root()
    defaults = config.setdefault("agents", {}).setdefault("defaults", {})
    if not defaults.get("workspace"):
        defaults["workspace"] = str(root / "workspace")

    plugins = config.setdefault("plugins", {})
    plugins["dirs"] = [str(PLUGINS_DIR)]
    flow_slice = plugins.setdefault("config", {}).setdefault(FLOW_PLUGIN_ID, {})
    flow_slice.setdefault("stateRoot", str(root / "research_flow"))
    for trunk_path, slice_path in PLUGIN_WEB_MIRROR.items():
        if value := render.dig(config, trunk_path):
            render.put(flow_slice, slice_path, value)
    # Same mirror, same reason as the web keys: the fork's assembly was CALLED
    # with the window the loop had resolved, so both observers that divide by it
    # quoted the model the turn actually ran on. A plugin factory sees its own
    # slice and nothing else. The loop's resolved window now reaches a turn's
    # hooks natively (``ctx.context_window_tokens``, hook surface v3) as the
    # fallback; the mirror stays because it pins the shipped numbers to the
    # config a reader audits, and config stays the word that wins.
    if window := defaults.get("contextWindowTokens"):
        flow_slice.setdefault("contextWindowTokens", window)

    base_flow = ((config.get("plugins") or {}).get("config") or {}).get(FLOW_PLUGIN_ID) or {}
    base_cap = defaults.get("maxToolIterations")
    catalogue = render.mode_catalogue(
        MODES_DIR,
        MODE_LABELS,
        baseline=BASELINE_MODE,
        overlay_keys=OVERLAY_KEYS,
        # The product's half of the catalogue: how a budget resolves, and which
        # overlay slice the plugin should see.
        resolve=lambda overlay: (
            iteration_cap(base_flow, base_cap, overlay),
            {"drFlow": overlay.get("drFlow", {})},
        ),
    )
    if catalogue:
        acp = config.setdefault("acp", {})
        acp["modes"] = catalogue
        acp["defaultMode"] = BASELINE_MODE
        log(f"[run] modes: {', '.join(catalogue)} (default {BASELINE_MODE})")

    root.mkdir(parents=True, exist_ok=True)
    seed_identity(Path(defaults["workspace"]), flow_slice)
    seed_contract(Path(defaults["workspace"]), flow_slice)
    render.sweep_stale_renders(root)
    return render.write_rendered(config, root)


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve Raven-Research-NG over ACP on stdio.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    args = parser.parse_args()

    rendered = render_config(Path(args.config).resolve())
    log(f"[run] exec {sys.executable} -m raven acp (config {rendered})")
    os.execv(sys.executable, [sys.executable, "-m", "raven", "acp", "--config", str(rendered)])
    raise AssertionError("unreachable: execv does not return")


if __name__ == "__main__":
    sys.exit(main())
