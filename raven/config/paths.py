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


def get_workspace_path(workspace: str | None = None) -> Path:
    """Resolve and ensure the agent workspace path."""
    path = Path(workspace).expanduser() if workspace else Path.home() / ".raven" / "workspace"
    return ensure_dir(path)


def get_cli_history_path() -> Path:
    """Return the shared CLI history file path."""
    return Path.home() / ".raven" / "history" / "cli_history"


def get_bridge_install_dir() -> Path:
    """Return the shared WhatsApp bridge installation directory."""
    return Path.home() / ".raven" / "bridge"


def get_oauth_dir() -> Path:
    """Return the directory every OAuth provider's credentials live in.

    One home, so that writing, reading, reporting and deleting a credential all
    derive the same path. Letting each client keep its own default is what let
    ``openai_codex`` be written by one name and read by another, reporting a
    signed-in provider as unauthenticated forever.

    Owner-only, and tightened when it is not. The clients that write in here
    create their files with a plain ``open()``, so under a normal umask the
    tokens land world-readable -- and only some of the writers are ours to fix.
    The directory is what holds for the rest, including a writer added later.
    """
    path = Path.home() / ".raven" / "oauth"
    path.mkdir(parents=True, exist_ok=True)
    if path.stat().st_mode & 0o077:
        path.chmod(0o700)
    return path


def restrict_to_owner(*paths: Path) -> None:
    """Make credential files readable only by their owner. Absent ones are skipped.

    Called once per sign-in rather than per write: a driver rewriting its own
    credential opens the existing path for truncation, which keeps the mode it
    finds, so this holds across every refresh after it. A rewrite that replaces
    the file instead has to set the mode on the replacement itself -- there is no
    mode left to keep.
    """
    for path in paths:
        if path.exists():
            path.chmod(0o600)


def get_legacy_sessions_dir() -> Path:
    """Return the legacy global session directory used for migration fallback."""
    return Path.home() / ".raven" / "sessions"
