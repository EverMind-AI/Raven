"""Inherit provider creds + model defaults from ~/.hermes/.

Both Raven and Hermes benchmarks use the LAN vLLM configured in
~/.hermes/config.yaml; keeping that one source means benchmarks auto-track
the user's actual onboarded config instead of drifting.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


def _hermes_home_dir() -> Path:
    """Resolve which HERMES_HOME-style dir to read from.

    Precedence:
      1. $HERMES_HOME_OVERRIDE  (for per-run model-tier ablations)
      2. ~/.hermes
    """
    override = os.environ.get("HERMES_HOME_OVERRIDE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".hermes"


def load_env_from_hermes_home() -> None:
    """Read <hermes-home>/.env into process env (only keys not already set)."""
    env_path = _hermes_home_dir() / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def load_config_from_hermes_home() -> dict[str, Any]:
    """Read <hermes-home>/config.yaml; return {} if missing or unreadable."""
    cfg_path = _hermes_home_dir() / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        return yaml.safe_load(cfg_path.read_text()) or {}
    except yaml.YAMLError:
        return {}


__all__ = ["load_env_from_hermes_home", "load_config_from_hermes_home"]
