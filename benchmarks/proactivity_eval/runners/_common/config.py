"""Resolve runner paths/endpoints from a yaml config + env fallbacks.

No absolute paths live in Python source; everything comes from
`runners.config.yaml` (or its `.local` override) or environment variables.

Typical use:

    from _common import get_config
    cfg = get_config()          # cached singleton after first call
    hermes_src = cfg.hermes_src
    vllm_base = cfg.vllm_base_url

For tests / alternate configs:

    from _common import get_config, reset_config
    reset_config()
    cfg = get_config(explicit_path="/tmp/custom.yaml")
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

# proactivity-eval/ — two levels up from this file.
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent


def _expand(path: str | None) -> str | None:
    """Expand ~ and $VAR; return None if input is None or empty."""
    if not path:
        return None
    return os.path.expanduser(os.path.expandvars(path))


def resolve_path(raw: str | None, base: Path) -> Path | None:
    """Expand + make absolute, resolving relative paths against `base`."""
    expanded = _expand(raw)
    if expanded is None:
        return None
    p = Path(expanded)
    return p if p.is_absolute() else (base / p).resolve()


@dataclass(frozen=True)
class RunnersConfig:
    """Immutable view over the global yaml + env escape hatches.

    Dataset paths live in per-benchmark yamls (runners/benchmarks/<name>/<name>.yaml)
    and are read via ``_common.get_benchmark_config(name)`` — this object only
    holds cross-benchmark knobs (LLM endpoints, system locations).
    """

    config_path: Path

    # Systems
    hermes_src: Path | None
    openclaw_cmd: str

    # LLM
    vllm_base_url: str
    vllm_model_id: str
    vllm_api_key: str
    vllm_context_window: int
    vllm_max_tokens: int
    judge_base_url: str
    judge_model: str


def _candidate_config_paths(explicit: str | Path | None) -> list[Path]:
    out: list[Path] = []
    if explicit:
        out.append(Path(explicit).expanduser())
    env = os.environ.get("PROACTIVITY_EVAL_CONFIG")
    if env:
        out.append(Path(env).expanduser())
    out.append(_PACKAGE_ROOT / "runners.config.local.yaml")
    out.append(_PACKAGE_ROOT / "runners.config.yaml")
    return out


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Failed to parse runners config at {path}: {exc}")
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must be a yaml mapping at top level")
    return data


def _build(config_path: Path, data: dict[str, Any]) -> RunnersConfig:
    base = config_path.parent
    systems = data.get("systems") or {}
    llm = data.get("llm") or {}

    # Env var takes precedence over yaml for individual fields.
    hermes_src = os.environ.get("HERMES_AGENT_SRC") or systems.get("hermes_src")
    openclaw_cmd = (
        os.environ.get("OPENCLAW_CMD") or systems.get("openclaw_cmd") or "openclaw"
    )

    return RunnersConfig(
        config_path=config_path,
        hermes_src=resolve_path(hermes_src, base),
        openclaw_cmd=openclaw_cmd,
        vllm_base_url=os.environ.get("VLLM_BASE_URL") or llm.get("vllm_base_url")
            or "http://localhost:8000/v1",
        vllm_model_id=os.environ.get("VLLM_MODEL_ID") or llm.get("vllm_model_id")
            or "qwen3.5-27B",
        vllm_api_key=llm.get("vllm_api_key") or "EMPTY",
        vllm_context_window=int(llm.get("vllm_context_window") or 65536),
        vllm_max_tokens=int(llm.get("vllm_max_tokens") or 8192),
        judge_base_url=os.environ.get("JUDGE_BASE_URL") or llm.get("judge_base_url")
            or "http://localhost:8001",
        judge_model=os.environ.get("JUDGE_MODEL") or llm.get("judge_model")
            or "Qwen3.5-397B",
    )


_CACHED: RunnersConfig | None = None


def get_config(explicit_path: str | Path | None = None) -> RunnersConfig:
    """Load + cache the config. Explicit path forces a re-load."""
    global _CACHED
    if explicit_path is not None or _CACHED is None:
        for candidate in _candidate_config_paths(explicit_path):
            if candidate.exists():
                data = _load_yaml(candidate)
                _CACHED = _build(candidate, data)
                return _CACHED
        # No file found → build from env + defaults alone, anchored at package root
        _CACHED = _build(_PACKAGE_ROOT / "runners.config.yaml", {})
    return _CACHED


def reset_config() -> None:
    """Drop cached config (for tests / CLI that passes --config)."""
    global _CACHED
    _CACHED = None


__all__ = ["RunnersConfig", "get_config", "reset_config", "resolve_path"]
