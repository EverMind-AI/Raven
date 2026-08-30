#!/usr/bin/env python
"""Host-side launcher for the Raven-Research-NG ACP server -- the B side.

The vendored Raven-Research (subagents/raven-research) carries a whole fork
checkout; this product carries none. It renders its config and execs the
installed raven's own ``raven acp``, so every turn runs through the same
assembly door (build_runtime) as the host's TUI and gateway. What remains
here is exactly what cannot move into either side's config: merging ``.env``
secrets into a rendered copy, inheriting the host's LLM when this folder has
no key of its own, and seeding the product identity into the workspace once.

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
* modes are absent: installed raven declares ``session/set_mode`` as not yet
  built, and this product does not pretend otherwise.

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

from raven.contracts.path_policy import CONFIG_FILENAME
from raven.home import raven_home

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"
IDENTITY_SOURCE = HERE / "soul.md"

PRODUCT = "raven-research-ng"


def env_value(name: str) -> str | None:
    """Read a setting from the process environment, falling back to ``.env``.

    The environment wins so a caller can override one value without editing
    the file that holds the others.
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

# The env var the search tool itself falls back to at call time; pinned
# against raven/agent/tools/web.py by tests/test_agents_research_launcher.py.
SEARCH_ENV_VAR = "SERPER_API_KEY"


def state_root() -> Path:
    """Everything this product persists lands here, never in this folder."""
    override = env_value("RESEARCH_NG_STATE_ROOT")
    if override:
        return Path(override).expanduser()
    return raven_home() / "workspace" / "subagent_sessions" / PRODUCT


def log(message: str) -> None:
    """Record a diagnostic without contaminating the protocol stream."""
    print(message, file=sys.stderr, flush=True)


def host_config() -> dict:
    """The host raven's config, or an empty dict when there is none to read.

    Read as JSON through the path paper's answer -- the host propagates its
    ``RAVEN_HOME`` into this process (builtin_agents does), so ``raven_home()``
    here is the host's home.
    """
    try:
        return json.loads((raven_home() / CONFIG_FILENAME).read_text(encoding="utf-8"))
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


def inherit_llm(config: dict, host: dict) -> str:
    """Take the host raven's whole LLM configuration; return what was taken.

    Only reached when this product has no key of its own. The provider block
    is copied wholesale rather than matched by name -- two providers spelled
    the same can be two different endpoints. What is inherited is which
    brains are reachable, which one is chosen, and how a model name routes;
    deliberately not the rest of ``agents.defaults``, which are this
    product's own operating limits.
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


def require_search(config: dict) -> None:
    """Refuse to launch when search has no key anywhere.

    Search is what this agent is for; withheld, the tool is absent and a run
    answers from the model's own memory -- which reads as an ordinary run, in
    the one failure mode nobody inspects. The bare env var counts because the
    tool resolves its key at call time from the config value or that var.
    """
    key = dig(config, ("tools", "web", "search", "apiKey"))
    if key or os.environ.get(SEARCH_ENV_VAR):
        return
    raise SystemExit(
        f"error: search has no key; put RESEARCH_SERPER_API_KEY in {HERE / '.env'} "
        f"(see .env.example) or export {SEARCH_ENV_VAR}"
    )


def seed_identity(workspace: Path) -> None:
    """Copy the product identity into the workspace raven reads it from.

    Once: the workspace copy is the live one afterwards, and a product update
    must not silently overwrite what an operator tuned in place.
    """
    target = workspace / "agent_memory" / "profile" / "soul.md"
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(IDENTITY_SOURCE.read_text(encoding="utf-8"), encoding="utf-8")


def sweep_stale_renders(root: Path) -> None:
    """Remove rendered configs whose server is gone, by pid liveness."""
    for stale in root.glob(".config.rendered.*.json"):
        try:
            pid = int(stale.name.split(".")[3])
            os.kill(pid, 0)
        except (IndexError, ValueError, ProcessLookupError):
            stale.unlink(missing_ok=True)
        except PermissionError:
            continue


def render_config(source: Path) -> Path:
    """Write a copy of ``source`` with the secrets merged in, under the state root.

    The location is the mechanism: raven derives its data dir from the config
    file's own parent, so wherever this file goes, sessions and cache go too.
    """
    config = json.loads(source.read_text(encoding="utf-8"))
    host = host_config()

    for name, path in SECRET_SLOTS.items():
        value = env_value(name)
        if not value and name not in REQUIRED_SECRETS:
            value = dig(host, path)
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

    root.mkdir(parents=True, exist_ok=True)
    seed_identity(Path(defaults["workspace"]))
    sweep_stale_renders(root)

    rendered = root / f".config.rendered.{os.getpid()}.json"
    fd = os.open(rendered, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(config, stream, indent=2, ensure_ascii=False)
    return rendered


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
