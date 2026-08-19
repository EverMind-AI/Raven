"""Configuration for raven's in-tree tracing.

Kept light and side-effect-free: read at process startup (from the CLI
``main()`` callback) to decide whether to install instrumentation. Environment
variables are explicit overrides; otherwise the ``[tracing]`` section of the
raven config file drives behavior, defaulting to on.
"""

from __future__ import annotations

import os
from pathlib import Path

_OFF = {"0", "false", "off", "no"}


def _config_section() -> dict:
    """Read the ``[tracing]`` block from the raven config file (best-effort).

    Uses raven's own config-path resolver so a ``--config`` override is honored
    once set. Never raises — tracing must not break startup.
    """
    try:
        import json

        from raven.config.loader import get_config_path

        path = get_config_path()
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        section = data.get("tracing")
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def enabled() -> bool:
    """On by default. ``RAVEN_TRACING`` env wins; else ``[tracing].enabled``."""
    env = os.environ.get("RAVEN_TRACING")
    if env is not None:
        return env.strip().lower() not in _OFF
    return bool(_config_section().get("enabled", True))


def state_dir() -> Path:
    """Trace state dir (``~/.raven/traces``). Spans land at ``<dir>/logs/audit-spans.log``.

    Overridable with ``RAVEN_TRACING_DIR`` (absolute) or ``RAVEN_HOME``; with
    neither, it follows the config file, the way the cron store, the logs and the
    ops ledgers already do. Two instances started with different ``--config`` were
    otherwise appending to one span log, and that file is where an arm's token
    count comes from -- so the count silently became the sum of both arms.

    Deriving it changes nothing for anyone who does not pass ``--config``: the
    config path is then ``~/.raven/config.json`` and its parent is the constant
    this replaces.
    """
    override = os.environ.get("RAVEN_TRACING_DIR")
    if override:
        return Path(override).expanduser()
    home = os.environ.get("RAVEN_HOME")
    if home:
        return Path(home).expanduser() / "traces"
    try:
        from raven.config.paths import get_config_path

        return Path(get_config_path()).expanduser().parent / "traces"
    except Exception:
        # Tracing must never be the reason a run cannot start.
        return Path.home() / ".raven" / "traces"


def port() -> int:
    """Dashboard viewer port. ``TRACING_UI_PORT`` env wins; else ``[tracing].port``."""
    env = os.environ.get("TRACING_UI_PORT")
    if env is not None:
        try:
            return int(env)
        except ValueError:
            return 4318
    try:
        return int(_config_section().get("port", 4318))
    except (ValueError, TypeError):
        return 4318


def preview_len() -> int:
    """Max chars kept inline on a span; full payloads go to artifacts."""
    env = os.environ.get("RAVEN_TRACING_PREVIEW")
    if env is not None:
        try:
            return max(0, int(env))
        except ValueError:
            return 500
    try:
        return max(0, int(_config_section().get("previewLen", 500)))
    except (ValueError, TypeError):
        return 500
