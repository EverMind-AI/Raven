"""Path policy for the front end's file viewer.

The viewer may render exactly what the agent may read: the resolver below is the
same one the filesystem tools use, given the same workspace and the same
``tools.restrict_to_workspace`` switch. Inventing a second policy here would
mean a file the agent just wrote could be unopenable, or -- worse -- a file the
agent is forbidden to read being served to the page.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from raven.agent.tools.filesystem import _resolve_path
from raven.config import load_config

# A viewer is not a download manager: past this size no renderer in the page
# does anything useful with the bytes, and the read would stall the event loop.
MAX_VIEW_BYTES = 25 * 1024 * 1024

# Extensions the page renders as text even though the system would call them
# something else (or nothing at all).
_TEXT_SUFFIXES = {
    ".c",
    ".cfg",
    ".conf",
    ".cpp",
    ".css",
    ".diff",
    ".env",
    ".go",
    ".h",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsonl",
    ".jsx",
    ".kt",
    ".log",
    ".lua",
    ".m",
    ".md",
    ".mdx",
    ".mjs",
    ".patch",
    ".php",
    ".pl",
    ".py",
    ".pyi",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".vue",
    ".yaml",
    ".yml",
    ".zsh",
}


def _raven_state_roots() -> tuple[Path, ...]:
    """Every directory raven keeps its own state in -- never viewable.

    Two, not one, because two are in use and they do not always agree.
    ``RAVEN_HOME`` moves ``serve.json`` (serve_commands) and the tui runtime
    dir; the credential store does not consult it at all --
    ``mcp_oauth._credentials_dir()`` is ``get_runtime_subdir("credentials")``,
    which hangs off ``get_config_path().parent``. Unset, they are the same
    directory and the difference never shows. Set ``RAVEN_HOME`` and they
    diverge, and a fence anchored on either one leaves the other's contents --
    the OAuth access and refresh tokens, or the token that mints session nonces
    -- as ordinary paths a viewer will serve.
    """
    import os

    from raven.config.paths import get_data_dir

    roots = []
    home = os.environ.get("RAVEN_HOME")
    roots.append((Path(home) if home else Path.home() / ".raven").expanduser().resolve())
    try:
        roots.append(get_data_dir().resolve())
    except OSError:
        # A state dir that cannot be resolved is not a reason to stop refusing
        # the one that can.
        pass
    return tuple(dict.fromkeys(roots))


def resolve_readable(raw: str) -> Path:
    """Resolve a viewer request to a real file, or raise.

    ``tools.restrict_to_workspace`` decides how far a *file* request may reach,
    and it defaults to off -- the agent is meant to read the project it was
    pointed at. Raven's own state directory is excluded regardless, because it
    is not project data: it holds provider credentials, the OAuth token store,
    and ``serve.json``, whose token mints session nonces. Serving that file to a
    page would hand a cookie-holder the shared secret the cookie is not supposed
    to be worth, collapsing the split between the two.

    Raises:
        ValueError: the request names nothing.
        PermissionError: the path resolves outside the allowed directory, or
            inside raven's state directory.
        FileNotFoundError: nothing is there.
        IsADirectoryError: it is there but is not a regular file.
    """
    if not raw or not raw.strip():
        raise ValueError("path is required")
    cfg = load_config()
    # `workspace_path`, not the raw config string: one derivation of where the
    # workspace is, so a fence cannot end up pointing somewhere the agent never
    # writes.
    workspace = cfg.workspace_path
    # A tuple of roots, which is what the fence takes since the agent's working
    # directory was split out of agent home. A single path silently became a
    # TypeError *inside* the check, so the viewer answered 500 where it meant 403
    # -- refusing either way, but by crashing rather than by deciding.
    allowed = (workspace,) if cfg.tools.restrict_to_workspace else ()
    resolved = _resolve_path(raw.strip(), workspace, allowed)
    # After _resolve_path, so a symlink pointing into a state dir is caught by
    # where it lands rather than by how it was spelled.
    for home in _raven_state_roots():
        if resolved == home or home in resolved.parents:
            raise PermissionError(f"{resolved} is inside raven's state directory")
    if not resolved.exists():
        raise FileNotFoundError(str(resolved))
    if not resolved.is_file():
        raise IsADirectoryError(str(resolved))
    return resolved


def sandbox_for(path: Path) -> str:
    """The CSP sandbox value for a viewable file.

    Every response is sandboxed, which is what gives it an opaque origin and so
    keeps an artifact away from the page's cookie and RPC socket. PDFs get
    ``allow-scripts`` on top, because the browser's own PDF viewer is script-
    driven and renders a blank frame without it -- the origin stays opaque
    either way, since ``allow-same-origin`` is never granted. HTML and SVG stay
    script-free: a report the agent wrote is readable without running code, and
    not running it is the safer default.
    """
    if path.suffix.lower() == ".pdf":
        return "sandbox allow-scripts"
    return "sandbox"


def content_type_for(path: Path) -> str:
    """The type to serve a viewable file as.

    Anything the page renders as text is served as UTF-8 text so a mislabelled
    source file does not arrive as a download.
    """
    if path.suffix.lower() in _TEXT_SUFFIXES:
        return "text/plain; charset=utf-8"
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed is None:
        return "application/octet-stream"
    if guessed.startswith("text/"):
        return f"{guessed}; charset=utf-8"
    return guessed
