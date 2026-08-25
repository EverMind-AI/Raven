"""Configuration for raven's in-tree tracing.

Kept light and side-effect-free: read at process startup (from the CLI
``main()`` callback) to decide whether to install instrumentation. Environment
variables are explicit overrides; otherwise the ``[tracing]`` section of the
raven config file drives behavior, defaulting to on.
"""

from __future__ import annotations

import contextlib
import os
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator

_OFF = {"0", "false", "off", "no"}

# Task-local suppression, checked by ``enabled()`` before any global switch.
# A context variable rather than an env var or module flag on purpose: a
# suppressor (e.g. a trajectory replay) may run concurrently with real turns
# in one process, and only its own task tree — including tasks spawned inside
# it, which snapshot the context — must go quiet.
_SUPPRESSED: ContextVar[bool] = ContextVar("raven_tracing_suppressed", default=False)


@contextlib.contextmanager
def suppress() -> Iterator[None]:
    """Disable tracing for the current task tree until the block exits."""
    token = _SUPPRESSED.set(True)
    try:
        yield
    finally:
        _SUPPRESSED.reset(token)


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
    """On by default. Task-local ``suppress()`` wins over everything; then the
    ``RAVEN_TRACING`` env; else ``[tracing].enabled``."""
    if _SUPPRESSED.get():
        return False
    env = os.environ.get("RAVEN_TRACING")
    if env is not None:
        return env.strip().lower() not in _OFF
    return bool(_config_section().get("enabled", True))


def state_dir() -> Path:
    """Trace state dir (``~/.raven/traces``). Spans land at ``<dir>/logs/audit-spans.log``.

    Overridable with ``RAVEN_TRACING_DIR`` (absolute) or ``RAVEN_HOME``.
    """
    override = os.environ.get("RAVEN_TRACING_DIR")
    if override:
        return Path(override).expanduser()
    home = os.environ.get("RAVEN_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".raven"
    return base / "traces"


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
