"""Per-benchmark configuration loader.

Each benchmark owns a yaml living at::

    runners/benchmarks/<name>/<name>.yaml

Paths inside are resolved relative to that yaml's directory (claweval-style).
Users can override fields without editing the tracked file by dropping a
``<name>.local.yaml`` next to the default — fields in the local file win.

Lookup precedence for a given benchmark ``<name>``:

    1. explicit path passed to get_benchmark_config(name, explicit_path=...)
    2. $PROACTIVITY_EVAL_BENCHMARK_<NAME> env var (uppercased)
    3. runners/benchmarks/<name>/<name>.local.yaml  (gitignored)
    4. runners/benchmarks/<name>/<name>.yaml       (tracked default)

Path resolution rules (mirrors claweval/src/config.py::_resolve_dict):
- Absolute paths pass through unchanged.
- Paths starting with ``.`` or ``/`` are resolved against the yaml dir.
- ``~`` and ``$VAR`` are expanded at load time.
- URLs (containing ``://``) pass through.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

_THIS_DIR = Path(__file__).resolve().parent
_BENCHMARKS_DIR = _THIS_DIR.parent / "benchmarks"

_ENV_SUB_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(val: str) -> str:
    """Expand ~ and ${VAR} / $VAR inside a string."""
    out = os.path.expanduser(val)
    out = _ENV_SUB_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), out)
    return os.path.expandvars(out)


def _looks_like_path(val: str) -> bool:
    return "/" in val or val.startswith(".") or val.startswith("~") or val.startswith("$")


def _resolve_value(val: Any, base_dir: Path) -> Any:
    if isinstance(val, str):
        if "://" in val:
            return val
        if not _looks_like_path(val):
            return val
        expanded = _expand(val)
        p = Path(expanded)
        if p.is_absolute():
            return str(p)
        return str((base_dir / p).resolve())
    if isinstance(val, dict):
        return {k: _resolve_value(v, base_dir) for k, v in val.items()}
    if isinstance(val, list):
        return [_resolve_value(v, base_dir) for v in val]
    return val


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _candidate_paths(name: str, explicit: str | Path | None) -> list[Path]:
    out: list[Path] = []
    if explicit:
        out.append(Path(explicit).expanduser())
    env = os.environ.get(f"PROACTIVITY_EVAL_BENCHMARK_{name.upper()}")
    if env:
        out.append(Path(env).expanduser())
    base = _BENCHMARKS_DIR / name
    out.append(base / f"{name}.local.yaml")
    out.append(base / f"{name}.yaml")
    return out


_CACHE: dict[tuple[str, str | None], dict[str, Any]] = {}


def get_benchmark_config(name: str, explicit_path: str | Path | None = None) -> dict[str, Any]:
    """Return the resolved benchmark config dict.

    Merges tracked + local override, then resolves any path-looking value
    against the yaml's directory. Raises FileNotFoundError if no yaml found.
    """
    cache_key = (name, str(explicit_path) if explicit_path else None)
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    candidates = _candidate_paths(name, explicit_path)
    primary: Path | None = None
    for c in reversed(candidates):  # primary = lowest-precedence existing file
        if c.exists():
            primary = c
            break
    if primary is None:
        raise FileNotFoundError(
            f"No benchmark config for '{name}'. Expected one of: " + ", ".join(str(p) for p in candidates)
        )

    def _load(p: Path) -> dict[str, Any]:
        try:
            data = yaml.safe_load(p.read_text()) or {}
        except yaml.YAMLError as exc:
            raise RuntimeError(f"Failed to parse {p}: {exc}")
        if not isinstance(data, dict):
            raise RuntimeError(f"{p} must be a yaml mapping at top level")
        return data

    merged = _load(primary)
    base_dir = primary.resolve().parent

    # Apply higher-precedence overrides on top of `merged`.
    for c in candidates:
        if c == primary or not c.exists():
            continue
        merged = _merge(merged, _load(c))

    resolved = _resolve_value(merged, base_dir)
    resolved["_config_path"] = str(primary)
    resolved["_base_dir"] = str(base_dir)
    _CACHE[cache_key] = resolved
    return resolved


def reset_benchmark_config_cache() -> None:
    _CACHE.clear()


__all__ = ["get_benchmark_config", "reset_benchmark_config_cache"]
