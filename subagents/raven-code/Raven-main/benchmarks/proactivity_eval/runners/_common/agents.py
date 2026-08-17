"""Per-agent configuration loader (mirrors ``benchmarks.py``).

Each agent owns a yaml living at::

    runners/agents/<name>/<name>.yaml

with agent-specific knobs (thinking level, max iterations, timeouts, …).
Local override at ``<name>.local.yaml`` (gitignored) takes precedence.

Usage::

    from _common import get_agent_config
    cfg = get_agent_config("openclaw")
    thinking = cfg.get("thinking", "medium")
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from .benchmarks import _merge, _resolve_value  # reuse helpers

_THIS_DIR = Path(__file__).resolve().parent
_AGENTS_DIR = _THIS_DIR.parent / "agents"


def _candidate_paths(name: str, explicit: str | Path | None) -> list[Path]:
    out: list[Path] = []
    if explicit:
        out.append(Path(explicit).expanduser())
    env = os.environ.get(f"PROACTIVITY_EVAL_AGENT_{name.upper()}")
    if env:
        out.append(Path(env).expanduser())
    base = _AGENTS_DIR / name
    out.append(base / f"{name}.local.yaml")
    out.append(base / f"{name}.yaml")
    return out


_CACHE: dict[tuple[str, str | None], dict[str, Any]] = {}


def get_agent_config(name: str, explicit_path: str | Path | None = None) -> dict[str, Any]:
    """Return the resolved agent config dict. Merges local overrides + env."""
    cache_key = (name, str(explicit_path) if explicit_path else None)
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    candidates = _candidate_paths(name, explicit_path)
    primary: Path | None = None
    for c in reversed(candidates):
        if c.exists():
            primary = c
            break
    if primary is None:
        raise FileNotFoundError(
            f"No agent config for '{name}'. Expected one of: " + ", ".join(str(p) for p in candidates)
        )

    def _load(p: Path) -> dict[str, Any]:
        data = yaml.safe_load(p.read_text()) or {}
        if not isinstance(data, dict):
            raise RuntimeError(f"{p} must be a yaml mapping at top level")
        return data

    merged = _load(primary)
    base_dir = primary.resolve().parent
    for c in candidates:
        if c == primary or not c.exists():
            continue
        merged = _merge(merged, _load(c))
    resolved = _resolve_value(merged, base_dir)
    resolved["_config_path"] = str(primary)
    resolved["_base_dir"] = str(base_dir)
    _CACHE[cache_key] = resolved
    return resolved


def reset_agent_config_cache() -> None:
    _CACHE.clear()


__all__ = ["get_agent_config", "reset_agent_config_cache"]
