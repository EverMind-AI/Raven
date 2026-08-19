"""Runtime path helpers derived from the active config context."""

from __future__ import annotations

from pathlib import Path

from raven.config.loader import get_config_path
from raven.utils.helpers import ensure_dir


def get_data_dir() -> Path:
    """Return the instance-level runtime data directory."""
    return ensure_dir(get_config_path().parent)


def get_runtime_subdir(name: str) -> Path:
    """Return a named runtime subdirectory under the instance data dir."""
    return ensure_dir(get_data_dir() / name)


def get_media_dir(channel: str | None = None) -> Path:
    """Return the media directory, optionally namespaced per channel."""
    base = get_runtime_subdir("media")
    return ensure_dir(base / channel) if channel else base


def get_cron_dir() -> Path:
    """Return the cron storage directory."""
    return get_runtime_subdir("cron")


def get_sentinel_dir() -> Path:
    """Return the Sentinel state directory (cross-process persisted state)."""
    return get_runtime_subdir("sentinel")


def get_cache_dir() -> Path:
    """Return the disposable, refetchable on-disk cache directory."""
    return get_runtime_subdir("cache")


def get_ops_home() -> Path:
    """Return the ops root: one directory per campaign, holding its ledger,
    events, decisions, recorded facts, meta and reports.

    Resolved per call, and not created here. It used to be a module-level
    constant bound from ``Path.home()`` at import, which cannot follow
    ``--config`` because the config path is known only at startup. The
    consequence was not a misplaced file: a call that omitted an explicit ledger
    landed in the default home, in a directory holding no ``meta.json``, so the
    report gate had no starting value to compare against and accepted a report it
    could not check. "Nothing wrong" and "nothing to check" both came out as
    ``Accepted``.

    With no ``--config`` this is ``~/.raven/ops`` exactly as before, so an
    existing single-instance install does not find its campaigns moved.
    """
    return get_data_dir() / "ops"


def get_sandbox_dir(backend: str) -> Path:
    """Return the sandbox runtime home directory for the given backend.

    e.g. backend='boxlite' → <data_dir>/sandbox/boxlite (used as boxlite's
    home_dir so its DB, images, and layers live under raven's data dir
    instead of ~/.boxlite).
    """
    return ensure_dir(get_runtime_subdir("sandbox") / backend)


def get_logs_dir() -> Path:
    """Return the logs directory."""
    return get_runtime_subdir("logs")


def derived_workspace() -> Path:
    """The workspace this config file implies, without creating it.

    Falls back to ``~/.raven/workspace`` when no config path can be resolved: a
    workspace is needed to run at all, so it must never be the thing that fails.
    With no ``--config`` the derived value IS that constant, so nothing moves for
    anyone who never passed one.
    """
    try:
        return Path(get_config_path()).expanduser().parent / "workspace"
    except Exception:
        return Path.home() / ".raven" / "workspace"


def get_workspace_path(workspace: str | None = None) -> Path:
    """Resolve and ensure the agent workspace path."""
    path = Path(workspace).expanduser() if workspace else derived_workspace()
    return ensure_dir(path)


def get_cli_history_path() -> Path:
    """Return the shared CLI history file path."""
    return Path.home() / ".raven" / "history" / "cli_history"


def get_bridge_install_dir() -> Path:
    """Return the shared WhatsApp bridge installation directory."""
    return Path.home() / ".raven" / "bridge"


def get_legacy_sessions_dir() -> Path:
    """Return the legacy global session directory used for migration fallback."""
    return Path.home() / ".raven" / "sessions"
