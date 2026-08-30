"""The launcher library: render a product config and hand it to installed raven.

A product under ``agents/`` is a folder, not a fork: a published
``config.json``, a ``.env`` for secrets, and a launcher that renders the two
into a private copy and execs ``python -m raven acp``. What every such
launcher repeats is here -- reading a setting from the process environment
with a ``.env`` fallback, merging declared secret slots, inheriting the
host's LLM block when the product has no key of its own, pinning a state
root under the host's home, seeding a workspace file once, assembling the
``acp.modes`` catalogue from overlay files, sweeping stale renders by pid
liveness, and writing the rendered copy owner-only.

What stays in each product is its tables and its judgement: which secrets go
where, what its modes are called, how a mode's budget resolves, what must
refuse to launch. The split is deliberate -- the machine here is the trunk's
contract (config paths, the ``acp.modes`` shape, the rendered file's
location being load-bearing for the data dir), while every name in a table
is product vocabulary the trunk must not learn.

Importing this widens a launcher's import surface beyond the two kernel
symbols the first launcher deliberately limited itself to; that boundary was
given up knowingly (ruled 2026-08-31): a launcher execs this interpreter's
own raven anyway, so importing the library it is about to run adds no new
requirement.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Iterable

from raven.contracts.path_policy import CONFIG_FILENAME
from raven.home import raven_home


def env_value(name: str, *, env_file: Path | None = None) -> str | None:
    """Read a setting from the process environment, falling back to ``env_file``.

    The environment wins so a caller can override one value without editing
    the file that holds the others.
    """
    if value := os.environ.get(name):
        return value.strip()
    if env_file is not None and env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            if key.strip() == name and value.strip():
                return value.strip()
    return None


def host_config() -> dict:
    """The host raven's config, or an empty dict when there is none to read.

    Read as JSON through the path paper's answer -- the host propagates its
    ``RAVEN_HOME`` into a launcher process (builtin_agents does), so
    ``raven_home()`` here is the host's home.
    """
    try:
        return json.loads((raven_home() / CONFIG_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def dig(data: dict, path: tuple) -> str:
    """The string at a dotted path, or ``""`` for anything absent or non-string."""
    node: Any = data
    for part in path:
        if not isinstance(node, dict):
            return ""
        node = node.get(part)
    return node if isinstance(node, str) else ""


def put(data: dict, path: tuple, value: str) -> None:
    """Write ``value`` at a dotted path, creating the dicts on the way."""
    node = data
    for part in path[:-1]:
        node = node.setdefault(part, {})
    node[path[-1]] = value


def apply_secret_slots(
    config: dict,
    host: dict,
    *,
    slots: dict[str, tuple],
    required: Iterable[str],
    lookup: Callable[[str], str | None],
) -> None:
    """Merge each declared secret into its config path.

    ``slots`` is the product's table (env var name -> config path) and
    ``lookup`` its resolution order (typically :func:`env_value` over its own
    ``.env``). An optional secret the product does not set falls back to the
    same path in the host config; a ``required`` one never falls back
    per-slot -- its absence is the product's own signal (the first launcher
    switches the whole LLM block to host inheritance on it, see
    :func:`inherit_llm`).
    """
    required = set(required)
    for name, path in slots.items():
        value = lookup(name)
        if not value and name not in required:
            value = dig(host, path)
        if value:
            put(config, path, value)


def inherit_llm(config: dict, host: dict) -> str:
    """Take the host raven's whole LLM configuration; return what was taken.

    Only reached when the product has no key of its own. The provider block
    is copied wholesale rather than matched by name -- two providers spelled
    the same can be two different endpoints. What is inherited is which
    brains are reachable, which one is chosen, and how a model name routes;
    deliberately not the rest of ``agents.defaults``, which are the
    product's own operating limits. ``""`` when the host has no provider key
    to lend, which the caller treats as a refusal to launch.
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


def product_state_root(product: str, *, override: str | None = None) -> Path:
    """Where a product persists everything -- never in its own folder.

    The default seats every product's state under the host home's sub-agent
    sessions, one directory per product name; ``override`` is the resolved
    value of the product's own state-root variable, when it names one.
    """
    if override:
        return Path(override).expanduser()
    return raven_home() / "workspace" / "subagent_sessions" / product


def seed_once(target: Path, render: Callable[[], str]) -> bool:
    """Write ``render()`` to ``target`` unless it already exists.

    Once is the contract: the workspace copy is the live one afterwards, and
    a product update must not silently overwrite what an operator tuned in
    place. The render is not evaluated when the target exists. Returns
    whether it wrote.
    """
    if target.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(), encoding="utf-8")
    return True


def mode_catalogue(
    modes_dir: Path,
    labels: dict[str, tuple[str, str]],
    *,
    baseline: str,
    overlay_keys: frozenset[str],
    resolve: Callable[[dict], tuple[Any, dict]],
) -> dict:
    """The ``acp.modes`` block: one entry per mode, each carrying its own diff.

    Diffs, not merged blocks: the overlay reaches a product plugin's hooks
    through the session policy as ``ctx.metadata["mode_overlay"]``, merged
    over the baseline per session. The machine here owns the trunk contract
    -- loading ``<mode>.json`` overlays, refusing unknown top-level keys,
    skipping a labeled mode whose overlay file is absent, and stamping the
    resolved cap into both the entry and its overlay so every mode declares
    its own budget (the baseline included, whose diff is empty but whose cap
    is not). ``resolve(overlay)`` is the product's half: it returns the
    mode's iteration cap and the diff the plugin should see -- both sides of
    vocabulary the trunk must not learn. An empty dict when the product
    ships no modes directory, which leaves the rendered config without
    ``acp.modes`` and ``session/set_mode`` method-not-found.
    """
    if not modes_dir.is_dir():
        return {}
    catalogue: dict = {}
    for mode, (name, description) in labels.items():
        overlay: dict = {}
        if mode != baseline:
            overlay_file = modes_dir / f"{mode}.json"
            if not overlay_file.is_file():
                continue
            overlay = json.loads(overlay_file.read_text(encoding="utf-8"))
            unknown = sorted(set(overlay) - overlay_keys)
            if unknown:
                raise SystemExit(
                    f"{overlay_file}: unsupported top-level key(s) {', '.join(unknown)}; "
                    f"an overlay carries only {', '.join(sorted(overlay_keys))}"
                )
        cap, entry_overlay = resolve(overlay)
        if cap:
            entry_overlay["maxToolIterations"] = cap
        catalogue[mode] = {
            "name": name,
            "description": description,
            "maxToolIterations": cap,
            "overlay": entry_overlay,
        }
    return catalogue


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


def write_rendered(config: dict, root: Path) -> Path:
    """Write the rendered config under ``root``, owner-only, named by pid.

    The location is the mechanism: raven derives its data dir from the
    config file's own parent, so wherever this file goes, sessions and cache
    go too. Owner-only because the render is where the secrets landed; the
    pid in the name is what :func:`sweep_stale_renders` reads back.
    """
    rendered = root / f".config.rendered.{os.getpid()}.json"
    fd = os.open(rendered, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(config, stream, indent=2, ensure_ascii=False)
    return rendered
