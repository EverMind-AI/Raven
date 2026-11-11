"""The isolated ``RAVEN_HOME`` an agent folder is activated and dispatched in.

The home's config holds the model under test with its provider, the operator's
``providers`` block and the ``tools.web`` / ``tools.media`` blocks a launcher inherits,
and nothing else from the operator's config; ``memory.backend: null`` is the host's off
switch, which an agent's launcher carries into its rendered config, so the run writes
to no long-term store. Every child process gets an environment without the
operator's ``RAVEN_*`` variables and without the agent's own env-prefixed settings:
an agent key there would switch the agent to own-key mode and bypass the model
under test.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from evolver.validator.children import child_env
from evolver.validator.report import ValidatorEnvironmentError
from evolver.validator.snapshot import Snapshot

# The host blocks an agent's launcher inherits at render time besides ``providers``:
# the web search vendor and key, and the image-generation section.
LENT_TOOL_BLOCKS = ("web", "media")


@dataclass(frozen=True)
class IsolatedHome:
    path: Path
    agent_dir: Path
    env: dict[str, str]
    provider: str = ""


def provider_for(model: str, host: dict) -> str:
    """The provider the isolated home names for ``model``, chosen the way the host would.

    Named explicitly because an agent's own ``agents.defaults.provider`` survives the
    render whenever the host config names none, and would route the model under test
    through the agent's provider instead.
    """
    from raven.providers.registry import split_model_id

    providers = host.get("providers") or {}
    defaults = (host.get("agents") or {}).get("defaults") or {}
    if defaults.get("model") == model and defaults.get("provider") in providers:
        return str(defaults["provider"])
    for name, block in providers.items():
        if isinstance(block, dict) and model in (block.get("models") or []):
            return str(name)
    head = split_model_id(model)[0]
    return head if head in providers else ""


def _route_targets(snap: Snapshot, shipped_root: Path | None) -> list[Path]:
    """Shipped folders the snapshot routes to: discovery disables a folder whose target is absent."""
    try:
        routes = json.loads((snap.root / "subagent.json").read_text(encoding="utf-8")).get("routes") or []
    except (OSError, ValueError, AttributeError):
        return []
    wanted = {r.get("to") for r in routes if isinstance(r, dict)}
    if not wanted or shipped_root is None or not shipped_root.is_dir():
        return []
    found = []
    for folder in sorted(p for p in shipped_root.iterdir() if p.is_dir() and p.name != snap.source.name):
        try:
            name = json.loads((folder / "subagent.json").read_text(encoding="utf-8")).get("name")
        except (OSError, ValueError, AttributeError):
            continue
        if name in wanted:
            found.append(folder)
    return found


def scrubbed_env(base: Mapping[str, str], *, folder_name: str, home: Path) -> dict[str, str]:
    from raven.agent.subagent.vendored_agents import env_prefix

    own = env_prefix(folder_name) + "_"
    env = {key: value for key, value in base.items() if not key.startswith("RAVEN_") and not key.startswith(own)}
    env["RAVEN_HOME"] = str(home)
    return env


def _write_private(path: Path, data: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        json.dump(data, handle, indent=2)


def build_isolated_home(
    run_dir: Path, snap: Snapshot, *, host_config: Path, model: str, shipped_root: Path | None = None
) -> IsolatedHome:
    host_path = Path(host_config)
    try:
        host = json.loads(host_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidatorEnvironmentError(f"host config not found: {host_path}") from exc
    except (OSError, ValueError) as exc:
        raise ValidatorEnvironmentError(f"host config unreadable: {host_path}: {exc}") from exc
    providers = host.get("providers") if isinstance(host, dict) else None
    if not isinstance(providers, dict) or not providers:
        raise ValidatorEnvironmentError(f"host config {host_path} has no providers to lend the agent")

    home = Path(run_dir) / "home"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True, mode=0o700)
    provider = provider_for(model, host)
    defaults = {"model": model, **({"provider": provider} if provider else {})}
    config: dict = {"agents": {"defaults": defaults}, "providers": providers, "memory": {"backend": None}}
    host_tools = host.get("tools") if isinstance(host.get("tools"), dict) else {}
    lent = {key: host_tools[key] for key in LENT_TOOL_BLOCKS if key in host_tools}
    if lent:
        config["tools"] = lent
    _write_private(home / "config.json", config)
    agent_dir = home / "agents" / snap.source.name
    shutil.copytree(snap.root, agent_dir, symlinks=True)
    for target in _route_targets(snap, shipped_root):
        shutil.copytree(
            target, home / "agents" / target.name, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
    env = child_env(scrubbed_env(os.environ, folder_name=snap.source.name, home=home))
    return IsolatedHome(path=home, agent_dir=agent_dir, env=env, provider=provider)
