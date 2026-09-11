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

from raven.agent.tools.filesystem import resolve_path
from raven.agent.workdir import default_channel_root
from raven.config import load_config

# A viewer is not a download manager: past this size no renderer in the page
# does anything useful with the bytes, and the read would stall the event loop.
MAX_VIEW_BYTES = 25 * 1024 * 1024

# The largest file `fs.upload` accepts. It sits beside the viewer's ceiling
# because the two are one story told in both directions, and here rather than
# in the method that enforces it because the WebSocket transport has to size
# its own frame ceiling from it: an upload rides as base64 inside one JSON-RPC
# frame, so a transport ceiling below this one rejects the upload before the
# method can explain why, and the page sees a dropped socket instead of a
# reason. See `frame_ceiling_for_upload`.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

# Room for the JSON-RPC envelope around a maximal upload: the method name, the
# session key and the file name, plus escaping. Two orders of magnitude more
# than those need, because the cost of it being too large is nothing and the
# cost of it being too small is the silent disconnect this whole constant
# exists to prevent.
_FRAME_ENVELOPE_SLACK = 64 * 1024


def frame_ceiling_for_upload(limit: int = MAX_UPLOAD_BYTES) -> int:
    """The smallest WebSocket frame ceiling that can carry a maximal upload.

    base64 costs four bytes for every three, so a transport sized at the upload
    limit itself stops roughly a quarter short of it -- which is what made a
    3 MB attachment kill the socket while the method advertised 25 MB.
    """
    return -(-limit // 3) * 4 + _FRAME_ENVELOPE_SLACK


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

    Two, not one, by design: home-level state (serve.json, the tui runtime
    dir) anchors on ``raven_home()``, while the credential store hangs off
    ``get_config_path().parent`` -- ``mcp_oauth._credentials_dir()`` is
    ``get_runtime_subdir("credentials")``. With no ``--config`` override they
    are the same directory and the difference never shows; a second instance
    diverges them on purpose, and a fence anchored on either one alone leaves
    the other's contents -- the OAuth access and refresh tokens, or the token
    that mints session nonces -- as ordinary paths a viewer will serve.
    """
    from raven.config.paths import get_data_dir
    from raven.home import raven_home

    roots = [raven_home().resolve()]
    try:
        roots.append(get_data_dir().resolve())
    except OSError:
        # A state dir that cannot be resolved is not a reason to stop refusing
        # the one that can.
        pass
    return tuple(dict.fromkeys(roots))


def in_state_dir(resolved: Path, workspace: str | Path, *also_written: str | Path) -> bool:
    """Whether ``resolved`` is inside raven's state directory and not exempt.

    One fence, not one per surface. The viewer, ``fs.reveal`` and the ``fs.*``
    panel all have to answer alike for a given file: two of them refusing while
    the one that renders bytes allowed it is how ``serve.json`` reached the page.

    ``workspace`` is agent home, not a session's working directory. The two
    exemptions are derived from it, and both exist for one reason: they are
    where the agent itself writes. The workspace lives *at* ``~/.raven/workspace``
    by default, and a turn's working directory at ``~/.raven/tmp/<channel>`` -- a
    sibling of the workspace since agent home and the working directory were
    split apart. Exempting only the first left the second denied, which is every
    artifact a chat has produced since that split: the page drew the delivery and
    then refused to open it. Anchoring on the working directory instead broke
    the other way: the derived exemptions then covered ``~/.raven/tmp/<channel>``
    and a sibling that does not exist, and every file the DAG runner writes under
    ``~/.raven/workspace/sessions/`` -- a sub-agent's report among them -- read
    as state. The state directory's secrets (``config.json``, ``oauth/``,
    ``serve.json``) are siblings of both, never inside either.

    ``also_written`` adds a session's pinned working directory when a caller has
    one, so a session bound to a directory under the state root keeps its files.
    Every exemption, derived or added, is held to the same direction check: only
    a subtree the state root contains earns one. A directory that IS the state
    root, or sits above it, would otherwise ride the exemption and expose
    ``serve.json`` through it.

    Two roots rather than one, because two are in use and they do not always
    agree: ``RAVEN_HOME`` moves ``serve.json`` and the runtime dir, while the
    credential store hangs off ``get_config_path().parent``. Unset they are the
    same directory; set, a fence anchored on either one leaves the other's OAuth
    tokens as ordinary paths.
    """
    ws = Path(workspace).resolve()
    written = (ws, default_channel_root(ws).resolve(), *(Path(p).resolve() for p in also_written))
    for home in _raven_state_roots():
        if resolved != home and home not in resolved.parents:
            continue
        if not any(home in own.parents and (resolved == own or own in resolved.parents) for own in written):
            return True
    return False


def resolve_readable(raw: str, *, workspace: Path | None = None) -> Path:
    """Resolve a viewer request to a real file, or raise.

    ``tools.restrict_to_workspace`` decides how far a *file* request may reach,
    and it defaults to off -- the agent is meant to read the project it was
    pointed at. Raven's own state directory is excluded regardless, because it
    is not project data: it holds provider credentials, the OAuth token store,
    and ``serve.json``, whose token mints session nonces. Serving that file to a
    page would hand a cookie-holder the shared secret the cookie is not supposed
    to be worth, collapsing the split between the two.

    ``workspace`` is the session's working directory when the caller has one:
    it anchors relative paths and the ``restrict_to_workspace`` fence. The
    state-directory fence is anchored on agent home regardless, because its
    exemptions are derived from where the agent writes, and a working directory
    handed to it in that role turned every sub-agent report under
    ``~/.raven/workspace/sessions/`` into a refusal.

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
    home = cfg.workspace_path
    workspace = workspace or home
    # A tuple of roots, which is what the fence takes since the agent's working
    # directory was split out of agent home. A single path silently became a
    # TypeError *inside* the check, so the viewer answered 500 where it meant 403
    # -- refusing either way, but by crashing rather than by deciding.
    allowed = (workspace,) if cfg.tools.restrict_to_workspace else ()
    resolved = resolve_path(raw.strip(), workspace, allowed)
    # After resolve_path, so a symlink pointing into the state dir is caught by
    # where it lands rather than by how it was spelled.
    if in_state_dir(resolved, home, workspace):
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
    driven and renders a blank document without it -- the origin stays opaque
    either way, since ``allow-same-origin`` is never granted. This header is
    honoured by the viewer in a tab and in a frame alike; what the viewer
    refuses is a frame that carries the ``sandbox`` *attribute*, which is why
    the page frames a PDF without one. HTML and SVG stay script-free: a report
    the agent wrote is readable without running code, and not running it is
    the safer default.
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
