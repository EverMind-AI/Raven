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
    "RESEARCH_SERPER_API_KEY": ("tools", "web", "search", "apiKey"),
    "RESEARCH_JINA_API_KEY": ("tools", "web", "jinaApiKey"),
}
# The LLM key is the only one whose absence is fatal: Serper failing degrades a
# run to no search, and Jina is optional by design (unauthenticated r.jina.ai
# works at a lower rate limit, and a dead key is worse than none - it 402s).
REQUIRED_SECRETS = ("RESEARCH_API_KEY",)

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


def put(data: dict, path: tuple, value: str) -> None:
    node = data
    for part in path[:-1]:
        node = node.setdefault(part, {})
    node[path[-1]] = value


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


def render_config(source: Path) -> Path:
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
    host = host_config()

    # Each optional key falls back on its own: a missing Serper or Jina key is a
    # degradation, not a failure, and the host's is better than nothing.
    for name, path in SECRET_SLOTS.items():
        value = env_value(name) or ("" if name in REQUIRED_SECRETS else dig(host, path))
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

    # One workspace for the whole server, pinned under STATE_ROOT: the schema
    # default is the host raven's own `~/.raven/workspace`, and sessions are
    # kept apart below it by their protocol-minted ids.
    defaults = config.setdefault("agents", {}).setdefault("defaults", {})
    if not defaults.get("workspace"):
        defaults["workspace"] = str(STATE_ROOT / "workspace")

    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    sweep_stale_renders()

    rendered = STATE_ROOT / f".config.rendered.{os.getpid()}.json"
    # Create it unreadable to anyone else before a single secret byte is in it.
    fd = os.open(rendered, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(config, stream, indent=2, ensure_ascii=False)
    return rendered


def main() -> int:
    ap = argparse.ArgumentParser(description="Serve the Raven-X research agent over ACP on stdio.")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--raven-x", default=str(RAVEN_X))
    args = ap.parse_args()

    root = Path(args.raven_x).expanduser().resolve()
    # The console script, not `python -m raven.cli`: raven.cli is a package with
    # no __main__, so the module form cannot be executed.
    raven_bin = root / ".venv" / "bin" / "raven"
    if not raven_bin.is_file():
        raise SystemExit(f"error: Raven-X venv missing at {raven_bin}; run `uv sync` in {root}")

    rendered = render_config(Path(args.config).resolve())
    log(f"[run] exec {raven_bin} acp (config {rendered}, state under {STATE_ROOT})")
    os.chdir(root)
    os.execv(str(raven_bin), [str(raven_bin), "acp", "--config", str(rendered)])
    raise AssertionError("unreachable: execv does not return")


if __name__ == "__main__":
    sys.exit(main())
