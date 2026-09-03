"""Loading the runtime config for an entrance, with the launch-time overrides.

``--config`` pins the file (sticky in the loader for the process), ``--home``
overrides the agent home. A missing file is a FileNotFoundError here; the
surface decides how to say so.
"""

from __future__ import annotations

from pathlib import Path

from raven.config.schema import Config


def load_runtime_config(config: str | None = None, home: str | None = None) -> Config:
    """Load config and optionally override agent home."""
    from raven.config.loader import load_config, set_config_path

    config_path = None
    if config:
        config_path = Path(config).expanduser().resolve()
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")
        set_config_path(config_path)

    loaded = load_config(config_path)
    if home:
        loaded.agents.defaults.workspace = home
    return loaded


__all__ = ["load_runtime_config"]
