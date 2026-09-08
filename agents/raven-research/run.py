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
from typing import get_args

from raven.config import product_render as render
from raven.config.schema import WEB_VENDOR_ENV_VARS, WebFetchProvider, WebSearchProvider

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"
IDENTITY_SOURCE = HERE / "soul.md"
PLUGINS_DIR = HERE / "plugins"
FLOW_PLUGIN_DIR = PLUGINS_DIR / "research-flow"
FLOW_PLUGIN_ID = "research-flow"
MODES_DIR = HERE / "modes"
BASELINE_MODE = "medium"

# What a client's mode picker shows. The name and blurb live here rather than in
# the overlay files so a mode's label cannot drift from its diff.
MODE_LABELS = {
    "medium": (
        "Medium",
        "May answer settled general knowledge without searching, after an independent "
        "check; otherwise stops as soon as an independent judge finds the search results "
        "or the pages already decide the question. The default, and right for an ordinary question.",
    ),
    "high": (
        "High",
        "No early stop: every draft is reviewed against its evidence, and a rejection "
        "buys a revision and a deeper round of retrieval, up to three times. A draft the "
        "reviewer still faults after that ships with the verdict on record, and an "
        "unavailable reviewer never blocks the reply. For a multi-faceted topic one pass "
        "of evidence will not settle.",
    ),
    "max": (
        "Max",
        "High's review, over an evidence floor: a draft resting on too few readable "
        "pages or too few distinct sites is sent back to research, twice at most, "
        "before it is reviewed. For a survey where missing a source is the failure mode.",
    ),
}
OVERLAY_KEYS = frozenset({"drFlow", "agents"})

PRODUCT = "raven-research-ng"

# Where each secret belongs in the config raven loads. The paths are trunk
# raven's own config surface (tools.web.search.apiKey / tools.web.jinaApiKey),
# not the fork's vendor table; the loader-round-trip test pins that they stay
# real fields. Keys stay out of config.json because that file is published.
# The two vendors that predate the vendor table and keep a leaf of their own,
# which trunk raven still reads after the vendor slot. Their RESEARCH_* names
# stay pointed at the leaf so an operator's existing .env keeps its meaning.
LEGACY_VENDOR_SLOTS = {
    "serper": ("tools", "web", "search", "apiKey"),
    "jina": ("tools", "web", "jinaApiKey"),
}

SECRET_SLOTS = {
    "RESEARCH_API_KEY": ("providers", "openrouter", "apiKey"),
    "RESEARCH_SERPER_API_KEY": LEGACY_VENDOR_SLOTS["serper"],
    "RESEARCH_JINA_API_KEY": LEGACY_VENDOR_SLOTS["jina"],
    # One credential per vendor that has no leaf of its own, at the same path
    # the host holds it. Optional slots, so a vendor this product does not
    # configure inherits the host raven's key at that path -- which is how a
    # deployment configures a vendor once, centrally, instead of pasting it
    # into every product folder.
    **{
        f"RESEARCH_{env_var}": ("tools", "web", "providers", vendor, "apiKey")
        for vendor, env_var in WEB_VENDOR_ENV_VARS.items()
        if vendor not in LEGACY_VENDOR_SLOTS
    },
}

DEFAULT_SEARCH_VENDOR = "serper"
DEFAULT_FETCH_VENDOR = "jina"

# The names trunk raven's config accepts, read off the schema so a vendor the
# kernel cannot route is refused here rather than advertised and failing.
SEARCH_VENDORS = frozenset(get_args(WebSearchProvider))
FETCH_VENDORS = frozenset(get_args(WebFetchProvider))

# Which backend each half calls. Not secrets, so they resolve the way the proxy
# does: this product's setting, then its own config, then the host's choice.
SEARCH_PROVIDER_ENV = "RESEARCH_WEB_SEARCH_PROVIDER"
SEARCH_PROVIDER_SLOT = ("tools", "web", "search", "provider")
FETCH_PROVIDER_ENV = "RESEARCH_WEB_FETCH_PROVIDER"
FETCH_PROVIDER_SLOT = ("tools", "web", "fetch", "provider")

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
# The keys are NOT mirrored by path: the slice carries one key per tool, the
# SELECTED vendor's, and mirroring a fixed path would hand a Tavily-configured
# run the leftover Serper key to send to Tavily's endpoint. ``mirror_web``
# resolves the vendor first.
PLUGIN_WEB_MIRROR = {
    PROXY_SLOT: ("proxy",),
}

# The env var the search tool itself falls back to at call time for the default
# vendor; pinned against raven/agent/tools/web.py by
# tests/test_agents_research_launcher.py.
SEARCH_ENV_VAR = WEB_VENDOR_ENV_VARS[DEFAULT_SEARCH_VENDOR]


def vendor_key(config: dict, vendor: str) -> str:
    """One vendor's credential, in trunk raven's own resolution order.

    The vendor slot first, then the pre-vendor leaf for the two vendors that
    still have one. Restated here rather than imported because the launcher
    reads plain JSON, not the schema objects ``WebToolsConfig.vendor_key``
    walks; the order is pinned against it by the launcher tests.
    """
    if key := render.dig(config, ("tools", "web", "providers", vendor, "apiKey")):
        return key
    legacy = LEGACY_VENDOR_SLOTS.get(vendor)
    return render.dig(config, legacy) if legacy else ""


def selected_vendors(config: dict) -> tuple[str, str]:
    """``(search vendor, fetch vendor)`` this render selects.

    Refused, not degraded, when a name is not one trunk raven routes. The
    plugin behind this launcher degrades an unknown vendor to its default,
    which is right for a slice nothing validates; carried through here the
    same typo had ``require_search`` demand a key for a vendor that does not
    exist while the working one sat in the config. The launcher is the one
    place that can say what is actually wrong before anything is served.
    """
    search = render.dig(config, SEARCH_PROVIDER_SLOT) or DEFAULT_SEARCH_VENDOR
    fetch = render.dig(config, FETCH_PROVIDER_SLOT) or DEFAULT_FETCH_VENDOR
    for name, known, half in ((search, SEARCH_VENDORS, "search"), (fetch, FETCH_VENDORS, "fetch")):
        if name not in known:
            raise SystemExit(f"error: unknown web {half} vendor {name!r}; one of {sorted(known)}")
    return search, fetch


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
    vendor, _ = selected_vendors(config)
    env_var = WEB_VENDOR_ENV_VARS.get(vendor, SEARCH_ENV_VAR)
    if vendor_key(config, vendor) or os.environ.get(env_var):
        return
    raise SystemExit(
        f"error: search has no key for {vendor}; put RESEARCH_{env_var} in "
        f"{HERE / '.env'} (see .env.example) or export {env_var}"
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


def validate_overlays(base_flow: dict, catalogue: dict) -> None:
    """Refuse to launch on a mode overlay the flow would not read as written.

    The vendored twin validates every declared mode against its schema at
    startup and fails naming the mode; the plugin's ``FlowConfig`` ignores
    unknown keys in its base slice, so without this door a typo in
    ``modes/high.json`` would merge clean and run the base value under the high
    label. Same import seam as the prompt render: the plugin owns the schema.
    """
    if str(FLOW_PLUGIN_DIR) not in sys.path:
        sys.path.insert(0, str(FLOW_PLUGIN_DIR))
    from research_flow.config import FlowConfig

    base = FlowConfig.from_slice(base_flow)
    for mode, entry in catalogue.items():
        diff = (entry.get("overlay") or {}).get("drFlow") or {}
        if not diff:
            continue
        try:
            base.with_overlay(diff)
        except Exception as exc:  # noqa: BLE001 - every failure here is a refusal to launch
            raise SystemExit(f"error: {MODES_DIR / f'{mode}.json'}: drFlow overlay does not validate: {exc}") from exc


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
    declining the flow's override, which is how ``max`` asks to run to its own
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

    # A host configured AFTER the vendor table landed wrote these two to the
    # vendor slot, not to the leaf, and the leaf is the only path the slot table
    # inherits for them. Read the vendor slot too, or a deployment that
    # configured Serper centrally on a current raven launches keyless.
    for vendor in LEGACY_VENDOR_SLOTS:
        slot = ("tools", "web", "providers", vendor, "apiKey")
        if not render.dig(config, slot) and (inherited := render.dig(host, slot)):
            render.put(config, slot, inherited)

    for env_name, slot in ((SEARCH_PROVIDER_ENV, SEARCH_PROVIDER_SLOT), (FETCH_PROVIDER_ENV, FETCH_PROVIDER_SLOT)):
        if chosen := (env_value(env_name) or render.dig(config, slot) or render.dig(host, slot)):
            render.put(config, slot, chosen)

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
    # The engine's Agent home must sit OUTSIDE the host Agent home (the host
    # hands its home over as the session cwd, and the runtime refuses a cwd
    # that contains the engine's home): the default comes from the shared
    # placement helper -- the raven data directory, RESEARCH_NG_ACP_HOME
    # overrides (the NG spelling matches this product's own state variable).
    # The state root keeps the work (research_flow store, rendered configs).
    if not defaults.get("workspace"):
        defaults["workspace"] = str(render.product_acp_home(PRODUCT, override=env_value("RESEARCH_NG_ACP_HOME")))

    plugins = config.setdefault("plugins", {})
    plugins["dirs"] = [str(PLUGINS_DIR)]
    flow_slice = plugins.setdefault("config", {}).setdefault(FLOW_PLUGIN_ID, {})
    flow_slice.setdefault("stateRoot", str(root / "research_flow"))
    for trunk_path, slice_path in PLUGIN_WEB_MIRROR.items():
        if value := render.dig(config, trunk_path):
            render.put(flow_slice, slice_path, value)
    # The vendor and its key together: the slice has no vendor table to consult,
    # so a mismatched pair here sends one vendor's credential to another's
    # endpoint and every call comes back unauthorized.
    search_vendor, fetch_vendor = selected_vendors(config)
    render.put(flow_slice, ("search", "provider"), search_vendor)
    render.put(flow_slice, ("fetch", "provider"), fetch_vendor)
    slots = [(search_vendor, ("search", "apiKey")), (fetch_vendor, ("fetch", "apiKey"))]
    if fetch_vendor != DEFAULT_FETCH_VENDOR:
        # The reader a keyed vendor falls back to when no key resolves for it.
        # Carried whenever the selection is not already the default, rather than
        # only when the fallback will fire: the rule that decides that lives in
        # WebFetchTool.effective_provider, and a launcher second-guessing it
        # here would be the copy that drifts. Without this the host's own
        # configured Jina key reaches raven's built-in reader and not this one,
        # so the same deployment reads pages authenticated on one path and
        # anonymously on the other.
        slots.append((DEFAULT_FETCH_VENDOR, ("fetch", "fallbackApiKey")))
    for vendor, slice_path in slots:
        if key := vendor_key(config, vendor):
            render.put(flow_slice, slice_path, key)
    log(f"[run] web: search={search_vendor} fetch={fetch_vendor}")
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
        validate_overlays(base_flow, catalogue)
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
