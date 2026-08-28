#!/usr/bin/env python
"""Host-side launcher for the Raven-X deep-research agent's ACP server.

The host raven spawns this once per connection and speaks ACP on its
stdin/stdout; one process serves every conversation, and turns, sessions and
answers are all protocol business handled inside Raven-X. What remains of the
old per-turn launcher is the one job that cannot move into either side's
config: merging the `.env` secrets into a config file Raven-X can load.
`config.json` ships without them, and Raven-X's loader does no environment
substitution, so a rendered copy is the only way to get them in.

The rendered copy lands under STATE_ROOT, and the location is load-bearing:
Raven-X derives its runtime data dir from the config file's own parent, so
transcripts, cache and logs follow the rendered file out of this folder. The
workspace is pinned under the same root too - the schema's default is
`~/.raven/workspace`, the host raven's own, which this agent must not share.

After rendering, this process *execs* the checkout's own `raven acp`: the
server inherits this pid, process group and stdio, so the host's
process-group kill reaches it and nothing sits between the client and the
frames. The host tears the group down with SIGKILL, so no cleanup here could
ever run at shutdown; leftover rendered configs are swept by pid-liveness on
the next launch instead. stdout belongs to the protocol - every diagnostic
line goes to stderr, which the host journals.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The checkout lives beside this launcher, per the subagents/ convention.
RAVEN_X = HERE / "Raven-X"
DEFAULT_CONFIG = HERE / "config.json"
# Budget overlays over the baseline config. `config.json` is the complete
# default (fast) profile; each overlay carries only the knobs that differ, so
# the identity prompt and provider wiring exist in exactly one place.
MODES_DIR = HERE / "modes"

# What a client's mode picker shows. The name and blurb live here rather than in
# the overlay files because they are product copy about the choice, not config
# the agent reads; the overlay beside each one carries the knobs.
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
        "No early-convergence gate; exhaustive retrieval. "
        "Only when the user has explicitly asked for exhaustive research.",
    ),
}
# The baseline IS the fast profile, so it needs no overlay file; the others do.
BASELINE_MODE = "fast"


def env_value(name: str) -> str | None:
    """Read a setting from the process environment, falling back to `.env`.

    The environment wins so a caller can override one value without editing a
    file that holds the others. Parsed by hand rather than with python-dotenv:
    this launcher must stay importable under a bare `python3`, since that is
    what the subagent entry invokes.
    """
    if value := os.environ.get(name):
        return value.strip()
    env_file = HERE / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            if key.strip() == name and value.strip():
                return value.strip()
    return None


# Where each secret belongs in the config Raven-X loads. The keys stay out of
# `config.json` because that file is published; they are merged back in at
# launch (see `render_config`). Raven-X reads none of them from the
# environment - its config loader does no variable substitution - so a rendered
# file is the only way to get them in.
SECRET_SLOTS = {
    "RESEARCH_API_KEY": ("providers", "openrouter", "apiKey"),
    "RESEARCH_SERPER_API_KEY": ("tools", "web", "providers", "serper", "apiKey"),
    "RESEARCH_ANYSEARCH_API_KEY": ("tools", "web", "providers", "anysearch", "apiKey"),
    "RESEARCH_SERPAPI_API_KEY": ("tools", "web", "providers", "serpapi", "apiKey"),
    "RESEARCH_JINA_API_KEY": ("tools", "web", "providers", "jina", "apiKey"),
}

# Where the same credential sits in a config on the pre-vendor layout. The host
# raven is a separate checkout that still holds web keys per tool, so its keys
# arrive in the old shape indefinitely - this is not an upgrade path that can
# eventually be deleted. Tried after the slot's own path, so a host that does
# adopt the vendor layout is read from there first.
HOST_LEGACY_PATHS = {
    "RESEARCH_SERPER_API_KEY": (("tools", "web", "search", "apiKey"),),
    "RESEARCH_JINA_API_KEY": (("tools", "web", "jinaApiKey"),),
}

# The LLM key is the only one resolved differently: it never inherits from the
# host, which is what keeps it out of the per-slot fallback in `render_config`.
# Jina stays optional by design (unauthenticated r.jina.ai works at a lower rate
# limit, and a dead key is worse than none - it 402s). The search key is
# required, but not through this tuple - see `require_search`.
REQUIRED_SECRETS = ("RESEARCH_API_KEY",)

# Which backend a rendered config can select, and where its key comes from: the
# bare env var the tool itself falls back to, and this launcher's prefixed slot.
# The config field is not listed because it is derived - every credential lives
# at `tools.web.providers.<vendor>.apiKey`, since one AnySearch account serves
# both web tools.
#
# A copy of `SEARCH_PROVIDERS` / `FETCH_PROVIDERS` in Raven-X's
# `agent/tools/web.py`, because this launcher deliberately imports nothing from
# the checkout it launches - that checkout is replaced wholesale on every
# upstream zip. `tests/test_subagent_raven_launcher.py` reads both and fails
# when they drift.
SEARCH_PROVIDERS = {
    "serper": ("SERPER_API_KEY", "RESEARCH_SERPER_API_KEY"),
    "anysearch": ("ANYSEARCH_API_KEY", "RESEARCH_ANYSEARCH_API_KEY"),
    "serpapi": ("SERPAPI_API_KEY", "RESEARCH_SERPAPI_API_KEY"),
}

# The third element is whether the backend can read a page without a key. Jina
# can, at a lower rate limit, which is why `web_fetch` has always been offered
# on a bare checkout and must keep being offered.
FETCH_PROVIDERS = {
    "jina": ("JINA_API_KEY", "RESEARCH_JINA_API_KEY", False),
    "anysearch": ("ANYSEARCH_API_KEY", "RESEARCH_ANYSEARCH_API_KEY", True),
}

# Everything the runtime persists - transcripts above all - lands here rather
# than in this folder. See `render_config` for why writing the config here is
# what moves them.
STATE_ROOT = Path(
    env_value("RESEARCH_STATE_ROOT")
    or Path.home() / ".raven" / "workspace" / "subagent_sessions" / "raven-research"
)

# The host raven's config file, read for the fallbacks below. Read as JSON, never
# imported from raven: this launcher is standard-library only and has to run
# under a bare python3 that may not have the runtime installed at all.
HOST_CONFIG = Path(os.environ.get("RAVEN_HOME", "").strip() or Path.home() / ".raven") / "config.json"


def log(message: str) -> None:
    """Record a diagnostic without contaminating the protocol stream."""
    print(message, file=sys.stderr, flush=True)


def host_config() -> dict:
    """The host raven's config, or an empty dict when there is none to read."""
    try:
        return json.loads(HOST_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def dig(data: dict, path: tuple) -> str:
    for part in path:
        if not isinstance(data, dict):
            return ""
        data = data.get(part)
    return data if isinstance(data, str) else ""


def dig_any(data: dict, paths: tuple) -> str:
    """The first non-empty value among several places one setting may live."""
    for path in paths:
        if value := dig(data, path):
            return value
    return ""


def vendor_key(web: dict, vendor: str) -> str:
    """The credential a rendered config holds for one web vendor."""
    providers = web.get("providers")
    block = providers.get(vendor) if isinstance(providers, dict) else None
    return block.get("apiKey") or "" if isinstance(block, dict) else ""


def put(data: dict, path: tuple, value: str) -> None:
    node = data
    for part in path[:-1]:
        node = node.setdefault(part, {})
    node[path[-1]] = value


OVERLAY_KEYS = frozenset({"drFlow", "agents"})
"""What a `modes/*.json` overlay may carry at its top level.

Only these two are read below, so anything else would be dropped in silence --
the failure `AcpModeConfig` promises not to have. Widening the pair means
teaching the reader the new key in the same change.
"""


def mode_catalogue() -> dict:
    """The `acp.modes` block: one entry per mode, each carrying its own diff.

    Declared rather than applied. The overlays used to be merged here, which
    fixed a connection's budget at launch; the agent composes them per session
    now, so the same three files become a catalogue a client picks from over
    `session/set_mode` and `--mode` becomes only which entry a session starts
    in. Diffs, not merged blocks: a merged one would carry `identityOverride`,
    the whole identity prompt, once per mode.

    An empty dict when this folder ships no `modes/` directory, which leaves the
    rendered config without an `acp.modes` key and the agent's `session/set_mode`
    method-not-found - the pre-modes behaviour, unchanged.
    """
    if not MODES_DIR.is_dir():
        return {}
    catalogue = {}
    for mode, (name, description) in MODE_LABELS.items():
        overlay = {}
        if mode != BASELINE_MODE:
            overlay_file = MODES_DIR / f"{mode}.json"
            if not overlay_file.is_file():
                continue
            overlay = json.loads(overlay_file.read_text(encoding="utf-8"))
            unknown = sorted(set(overlay) - OVERLAY_KEYS)
            if unknown:
                raise SystemExit(
                    f"{overlay_file}: unsupported top-level key(s) {', '.join(unknown)}; "
                    f"an overlay carries only {', '.join(sorted(OVERLAY_KEYS))}"
                )
        catalogue[mode] = {
            "name": name,
            "description": description,
            "drFlow": overlay.get("drFlow", {}),
            "maxToolIterations": (overlay.get("agents") or {}).get("defaults", {}).get("maxToolIterations"),
        }
    return catalogue


def recommended_llm() -> str:
    """What this folder's manifest says this agent is tuned for."""
    try:
        rec = json.loads((HERE / "subagent.json").read_text(encoding="utf-8")).get("recommendedLlm") or {}
    except (OSError, ValueError):
        return "unrecorded"
    return f"{rec.get('model', '?')} via {rec.get('apiBase') or rec.get('provider', '?')}"


def inherit_llm(config: dict, host: dict) -> str:
    """Take the host raven's whole LLM configuration; return what was taken.

    Only reached when this agent has no key of its own. The host's provider block
    is copied wholesale rather than matched by name: a provider called `custom`
    here and one called `custom` there can be two different endpoints, so picking
    by name would silently point this agent at a gateway its model is not served
    on - a failure that looks like a bad answer rather than an error.

    What is inherited is which brains are reachable, which one is chosen, and how
    a model name routes to a provider. Deliberately not the rest of
    `agents.defaults`: the token ceiling, the tool-iteration cap and the timeouts
    are this agent's operating limits, tuned for its own job, and they have
    nothing to do with whose key is paying.
    """
    providers = host.get("providers") or {}
    if not any(isinstance(p, dict) and p.get("apiKey") for p in providers.values()):
        return ""
    for key in ("providers", "routing"):
        if key in host:
            config[key] = host[key]
    defaults = config.setdefault("agents", {}).setdefault("defaults", {})
    host_defaults = (host.get("agents") or {}).get("defaults") or {}
    for key in ("provider", "model"):
        if key in host_defaults:
            defaults[key] = host_defaults[key]
    return f"provider={defaults.get('provider')} model={defaults.get('model')}"


def sweep_stale_renders() -> None:
    """Remove rendered configs whose server is gone.

    The exec below hands this pid to the server, so the pid in a rendered
    file's name is the pid of the server that read it. The host tears servers
    down by SIGKILLing the process group, which leaves no chance to clean up
    from inside - so each launch sweeps for the previous ones. Liveness rather
    than age: an ACP server legitimately outlives any fixed cutoff, and its
    config file has to survive with it.
    """
    for stale in STATE_ROOT.glob(".config.rendered.*.json"):
        try:
            pid = int(stale.name.split(".")[3])
            os.kill(pid, 0)
        except (IndexError, ValueError, ProcessLookupError):
            stale.unlink(missing_ok=True)
        except PermissionError:
            continue


def require_search(config: dict) -> None:
    """Refuse to launch when the selected search backend has no key.

    Search is what this agent is for. Withheld, the tool is simply absent from
    the schema and the run answers from the model's own memory - which reads as
    an ordinary run, in the one failure mode nobody inspects. Failing at launch
    is the only place the absence is visible before it costs a batch.

    Three things this deliberately gets right:

    * The bare env var counts. The tool resolves its key at call time from the
      config value *or* its provider's variable, so a deploy that exports
      `SERPER_API_KEY` and configures nothing is configured. Reading the config
      alone would refuse to start a runtime that would have worked.
    * A corpus endpoint is a full exemption. It IS the search source on a
      fixed-corpus benchmark and needs no key; refusing there would break every
      BrowseComp-Plus run.
    * AnySearch's anonymous tier does not count. It works without a key at a
      lower rate limit, so accepting it would trade a loud launch failure for a
      run that is silently rate-limited mid-batch - the trade this check exists
      to refuse.
    """
    web = (config.get("tools") or {}).get("web") or {}
    if web.get("corpusEndpoint"):
        return
    provider = ((web.get("search") or {}).get("provider")) or "serper"
    env_var, slot = SEARCH_PROVIDERS.get(provider, SEARCH_PROVIDERS["serper"])
    if vendor_key(web, provider) or os.environ.get(env_var):
        return
    raise SystemExit(
        f"error: search provider '{provider}' has no key; put {slot} in {HERE / '.env'} "
        f"(see .env.example), export {env_var}, or set tools.web.corpusEndpoint to run "
        f"against a fixed corpus"
    )


def require_fetch(config: dict) -> None:
    """Refuse to launch when a selected or fallback page reader has no key.

    Weaker than `require_search` by design: the default reader works without a
    key, so this fires only for a backend that cannot. Two cases, one rule -
    the selected provider is what every fetch goes through, and a fallback entry
    that cannot run is worse than none, because it reads as insurance while
    being unreachable at the moment it is needed.

    A corpus endpoint is a full exemption for the same reason it is one for
    search: it serves the pages itself.
    """
    web = (config.get("tools") or {}).get("web") or {}
    if web.get("corpusEndpoint"):
        return
    fetch = web.get("fetch") or {}
    selected = fetch.get("provider") or "jina"
    chain = [selected, *(fetch.get("fallback") or [])]
    for provider in chain:
        if provider not in FETCH_PROVIDERS:
            continue
        env_var, slot, needs_key = FETCH_PROVIDERS[provider]
        if not needs_key or vendor_key(web, provider) or os.environ.get(env_var):
            continue
        role = "selected" if provider == selected else "fallback"
        raise SystemExit(
            f"error: {role} fetch provider '{provider}' has no key; put {slot} in "
            f"{HERE / '.env'} (see .env.example), export {env_var}, or drop it from "
            f"tools.web.fetch"
        )


def render_config(source: Path, mode: str | None = None) -> Path:
    """Write a copy of `source` with the `.env` secrets merged in, under STATE_ROOT.

    The location is the whole mechanism, not a detail. Raven-X derives
    `get_data_dir()` from the config file's own parent and offers no separate
    knob for the session directory, so wherever this file goes, `sessions/`,
    `cache/`, `cron/` and `ledger/` go with it. Writing it under STATE_ROOT is
    therefore the only way to keep conversation transcripts out of the project
    directory without patching the checkout - and the checkout is replaced
    wholesale on every upstream zip, so a patch would not survive.
    """
    config = json.loads(source.read_text(encoding="utf-8"))
    catalogue = mode_catalogue()
    if mode and mode not in catalogue:
        raise SystemExit(f"error: no overlay for mode {mode!r} at {MODES_DIR / f'{mode}.json'}")
    if catalogue:
        # The source stays the baseline every mode diffs against, so nothing is
        # merged into it here: `defaultMode` is the whole of what `--mode` does.
        acp = config.setdefault("acp", {})
        acp["modes"] = catalogue
        acp["defaultMode"] = mode or BASELINE_MODE
        log(f"[run] modes: {', '.join(catalogue)} (default {acp['defaultMode']})")
    host = host_config()

    # Each optional key falls back on its own: a missing Jina key is a
    # degradation, not a failure, and the host's is better than nothing. The
    # search key inherits the same way; whether its absence is fatal is decided
    # after the merge, by `require_search`, so the host's key still counts.
    for name, path in SECRET_SLOTS.items():
        value = env_value(name)
        if not value and name not in REQUIRED_SECRETS:
            value = dig_any(host, (path, *HOST_LEGACY_PATHS.get(name, ())))
        if value:
            put(config, path, value)

    llm_key = REQUIRED_SECRETS[0]
    if env_value(llm_key):
        defaults = config.get("agents", {}).get("defaults", {})
        log(f"[run] llm: own key (provider={defaults.get('provider')} model={defaults.get('model')})")
    else:
        taken = inherit_llm(config, host)
        if not taken:
            raise SystemExit(
                f"error: {llm_key} is not set and {HOST_CONFIG} has no provider key to inherit from; "
                f"put the key in {HERE / '.env'} (see .env.example), export it, or configure a "
                f"provider in the host raven"
            )
        log(f"[run] llm: inherited from {HOST_CONFIG} ({taken}); tuned for {recommended_llm()}")

    # After the LLM key, not before: that one is the more basic prerequisite, and
    # a deployment missing both should be told about it first rather than being
    # sent to fix search on a checkout that could not have answered anyway.
    require_search(config)
    require_fetch(config)

    # One workspace for the whole server, pinned under STATE_ROOT: the schema
    # default is the host raven's own `~/.raven/workspace`, and sessions are
    # kept apart below it by their protocol-minted ids.
    defaults = config.setdefault("agents", {}).setdefault("defaults", {})
    if not defaults.get("workspace"):
        defaults["workspace"] = str(STATE_ROOT / "workspace")

    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    sweep_stale_renders()

    rendered = STATE_ROOT / f".config.rendered.{os.getpid()}.json"
    # Unreadable to anyone else before a single secret byte is in it. The mode
    # argument applies only where `open` creates the file, and this name can
    # pre-exist: `sweep_stale_renders` keeps a render whose pid is alive, which
    # this process's own always is, so one left by an earlier process holding
    # this pid is truncated in place and would otherwise keep its old mode.
    fd = os.open(rendered, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(config, stream, indent=2, ensure_ascii=False)
    return rendered


def main() -> int:
    ap = argparse.ArgumentParser(description="Serve the Raven-X research agent over ACP on stdio.")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument(
        "--mode",
        choices=("fast", "deep", "ultra"),
        default=None,
        help="Which profile sessions start in; a client may switch a live "
        "session with session/set_mode. `fast` is the baseline as-is.",
    )
    ap.add_argument("--raven-x", default=str(RAVEN_X))
    args = ap.parse_args()

    root = Path(args.raven_x).expanduser().resolve()
    # The console script, not `python -m raven.cli`: raven.cli is a package with
    # no __main__, so the module form cannot be executed.
    raven_bin = root / ".venv" / "bin" / "raven"
    if not raven_bin.is_file():
        raise SystemExit(f"error: Raven-X venv missing at {raven_bin}; run `uv sync` in {root}")

    mode = None if args.mode == "fast" else args.mode
    rendered = render_config(Path(args.config).resolve(), mode=mode)
    log(f"[run] exec {raven_bin} acp (config {rendered}, state under {STATE_ROOT})")
    os.chdir(root)
    os.execv(str(raven_bin), [str(raven_bin), "acp", "--config", str(rendered)])
    raise AssertionError("unreachable: execv does not return")


if __name__ == "__main__":
    sys.exit(main())
