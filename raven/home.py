"""Where raven keeps everything.

One environment variable and one default, and no machinery import -- only
the layout paper this module implements (``contracts/path_policy``): it
answers an address, not a question about configuration. It sits in the
kernel set because the kernel has to find its own settings -- a raven-core wheel
that could not locate ``config.json`` without the config shelf would not be the
closure it claims to be -- and because the answer steers the installer, the node
runtime lookup, the trace directory, the cron store and the serve state file
alike, which is more consumers than any one shelf should own.

``raven.config.loader`` re-exports all three names, so the shelves and the
entrances keep reading them from where they always have.
"""

from __future__ import annotations

import os
from pathlib import Path

from raven.contracts.path_policy import CONFIG_FILENAME, DEFAULT_HOME_DIRNAME, HOME_ENV_VAR

__all__ = ["get_config_path", "raven_home", "set_config_path"]

#: Set by a caller that runs against a config file of its own (tests, and a
#: second instance). Wins over the environment and the default.
_current_config_path: Path | None = None


def set_config_path(path: Path) -> None:
    """Point every later read at ``path`` instead of the resolved default."""
    global _current_config_path
    _current_config_path = path


def raven_home() -> Path:
    """The directory raven keeps everything in.

    ``RAVEN_HOME`` decides where config.json, the cron store and every runtime
    subdirectory live, the same way it steers the installer, the node runtime
    lookup, the tracing directory and the serve state file.
    """
    home = os.environ.get(HOME_ENV_VAR, "").strip()
    return Path(home).expanduser() if home else Path.home() / DEFAULT_HOME_DIRNAME


def get_config_path() -> Path:
    """The configuration file raven reads and writes."""
    if _current_config_path:
        return _current_config_path
    return raven_home() / CONFIG_FILENAME
