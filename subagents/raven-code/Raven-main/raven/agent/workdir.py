"""Resolve the working directory a turn runs in.

Agent home (user memory, skills, transcripts) is global and separate; this
module only decides where a turn reads and writes files.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

from loguru import logger

from raven.utils.helpers import safe_path_segment

# Subtrees of agent home the agent must not be able to adopt as a working
# directory: it would then write artifacts over its own memory and skills.
# ``sessions`` covers the subagent history too -- it lives under each session's
# own directory there (raven/agent/subagent_history.py).
_PROTECTED_SUBTREES = ("user_memory", "skills", "sessions")

_CURRENT: ContextVar[Path | None] = ContextVar("raven_session_workdir", default=None)


def default_channel_root(agent_home: Path) -> Path:
    """Root of the per-channel working directories, a sibling of agent home.

    With the default agent home (``~/.raven/workspace``) that is
    ``~/.raven/tmp``. Derived from agent home rather than from ``$HOME`` so an
    instance started with ``--home`` keeps its scratch directories next to that
    instance instead of reaching back into the real one.

    A sibling, not a child: these directories hold whatever a chat happens to
    produce, which is not part of the agent's own memory and skills. One shared
    root also lets a single sandbox mount cover every channel.

    The one case a plain sibling gets wrong is a home directly under the
    filesystem root -- ``--home /workspace``, a common container mount, would
    make the root the system ``/tmp``: shared with every other process,
    world-writable, and cleared on reboot. There the sibling is named after the
    home instead, which cannot collide with an existing system directory.
    """
    home = Path(agent_home).expanduser()
    if home.parent == Path(home.anchor) or not home.name:
        return home.parent / f"{home.name or 'raven'}-tmp"
    return home.parent / "tmp"


class WorkdirPolicy(str, Enum):
    """How an entrypoint picks a working directory when nothing overrides it."""

    LAUNCH_DIR = "launch_dir"
    PER_CHANNEL = "per_channel"


def current() -> Path | None:
    """The working directory bound for the running turn, if any."""
    return _CURRENT.get()


@contextmanager
def bind(path: Path) -> Iterator[None]:
    """Bind ``path`` as the working directory for the enclosing block."""
    token = _CURRENT.set(path)
    try:
        yield
    finally:
        _CURRENT.reset(token)


# The per-session resolver a runtime may register: ``callable(session_key) ->
# Path | None``. Module state on purpose - the turn scheduler that binds a
# working directory around a turn must not import the engine, and the engine
# that knows where sessions keep their pinned workdir must not import the
# scheduler. ``None`` (unset, or resolver answering None) makes ``bind_for`` a
# no-op, which is every context that never pins a workdir: evals, plain CLI.
_RESOLVER: Any = None


def set_resolver(resolver: Any) -> None:
    """Register (or clear, with None) the session-key workdir resolver."""
    global _RESOLVER
    _RESOLVER = resolver


@contextmanager
def bind_for(session_key: str) -> Iterator[None]:
    """Bind the registered resolver's answer for ``session_key``, or nothing.

    The resolver must never fail a turn: any error is logged and the turn runs
    unbound, exactly as it would have before resolvers existed.
    """
    path: Path | None = None
    if _RESOLVER is not None and session_key:
        try:
            answer = _RESOLVER(session_key)
            path = Path(answer) if answer else None
        except Exception as exc:  # noqa: BLE001 - a turn must not die on this
            logger.warning("workdir resolver failed for {}: {}", session_key, exc)
    if path is None:
        logger.debug("workdir: no pin for {}; turn runs unbound", session_key)
        yield
        return
    logger.debug("workdir: {} bound to {}", session_key, path)
    with bind(path):
        yield


def repoint(path: Path) -> None:
    """Repoint the RUNNING context at ``path``, with no reset.

    For a mid-turn rebind (the workspace gate moving a session into a git
    worktree the user just approved): the new binding must hold for the rest
    of the turn's task context and die with it. Outside a turn this leaks
    nothing - the contextvar is task-local.
    """
    _CURRENT.set(Path(path))


def is_within(path: Path, root: Path) -> bool:
    """Whether ``path`` sits inside ``root``, comparing physical paths.

    A persisted override is resolved through symlinks (``validate_override``
    calls ``.resolve()``); ``mount_root()`` and ``workspace`` generally are
    not. Comparing an unresolved and a resolved path with ``is_relative_to``
    can disagree about a directory that is, on disk, the very same place.
    """
    return path.resolve().is_relative_to(root.resolve())


def validate_override(value: str | Path, agent_home: Path) -> Path:
    """Check a user-supplied working directory, returning it resolved."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"working directory must be an absolute path, got {value!r}")
    resolved = path.resolve()
    home = Path(agent_home).expanduser().resolve()
    # Agent home itself is rejected for the same reason its subtrees are, and
    # more strongly: from there every protected subtree is one relative path
    # away, so an ordinary relative write lands on the agent's own memory.
    if resolved == home:
        raise ValueError(f"working directory must not be the agent home directory itself ({home})")
    # An ancestor is worse still. `~/.raven` is not merely agent home's parent,
    # it is the instance data directory -- config.json (provider keys), oauth/
    # (provider tokens), cron/, sentinel/, logs/. The per-turn checkpoint runs
    # `add -A` over the working directory, and the `.raven/` default exclude
    # cannot help when `.raven` *is* the work-tree root, so pointing a session
    # here would commit every credential into a shadow git repo.
    if resolved in home.parents:
        raise ValueError(f"working directory must not contain the agent home directory ({home})")
    for subtree in _PROTECTED_SUBTREES:
        candidate = home / subtree
        if resolved == candidate or candidate in resolved.parents:
            raise ValueError(f"working directory must not be inside the agent's {subtree} tree ({candidate})")
    return resolved


class WorkdirResolver:
    """Maps a session key to the directory that session's turns work in."""

    def __init__(
        self,
        policy: WorkdirPolicy,
        *,
        agent_home: Path,
        launch_dir: Path | None = None,
        session_root: Path | None = None,
        sessions: Any = None,
        explicit_workdir: Path | None = None,
        channel_workspaces: dict[str, str] | None = None,
    ) -> None:
        self._policy = policy
        self._agent_home = Path(agent_home).expanduser()
        self._launch_dir = Path(launch_dir) if launch_dir else Path.cwd()
        self._session_root = Path(session_root) if session_root else default_channel_root(self._agent_home)
        self._sessions = sessions
        self._explicit_workdir = Path(explicit_workdir) if explicit_workdir else None
        # channel name -> configured directory, from `channels.<name>.workspace`.
        # A channel with no entry (or an empty one) falls back to
        # `<session_root>/<channel>`.
        #
        # Validated here, at construction, and not where it is used. This value
        # arrives from a config file or the web UI's channels form, which are
        # the two places nothing else checks it -- every other way of naming a
        # working directory (`-w`, the per-session override) already goes
        # through `validate_override`. Without this, `channels.x.workspace =
        # <agent home>` is accepted and the per-turn `add -A` then runs over
        # user_memory/ and sessions/.
        #
        # A bad entry is dropped with a warning rather than raised: the value
        # may have been typed days ago in a browser, so failing the turn that
        # happens to use it reports the problem far from where it was made,
        # and falling back to `<session_root>/<channel>` is always usable.
        self._channel_workspaces: dict[str, Path] = {}
        for name, configured in (channel_workspaces or {}).items():
            if not configured:
                continue
            try:
                self._channel_workspaces[name] = validate_override(configured, self._agent_home)
            except ValueError as exc:
                logger.warning(
                    "channels.{}.workspace is not a usable working directory ({}); falling back to {}",
                    name,
                    exc,
                    self._session_root / safe_path_segment(name),
                )

    def resolve(self, session_key: str, *, create: bool = True) -> Path:
        """Resolve the working directory for ``session_key``, creating it by default.

        ``create=False`` is for callers that only want to know the path -- a
        read must not leave a directory behind on disk.
        """
        path = self._explicit_workdir or self._persisted(session_key) or self._default(session_key)
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def _persisted(self, session_key: str) -> Path | None:
        if self._sessions is None or not session_key:
            return None
        stored = self._sessions.get_or_create(session_key).metadata.get("workdir")
        if not stored:
            return None
        return validate_override(stored, self._agent_home)

    def _default(self, session_key: str) -> Path:
        """The policy default: the launch directory, or the channel's directory.

        Under ``PER_CHANNEL`` every chat on a channel shares one directory --
        the channel is what the user configures, and its conversations are the
        same kind of work. A single session can still be pinned elsewhere
        through the persisted override, which is checked before this.
        """
        if self._policy is WorkdirPolicy.LAUNCH_DIR:
            return self._launch_dir
        name = session_key.partition(":")[0]
        configured = self._channel_workspaces.get(name)
        if configured is not None:
            return configured
        return self._session_root / (safe_path_segment(name) or "_")

    def mount_root(self) -> Path:
        """The directory a sandbox VM must mount to cover every session.

        The default per-channel directories share one root, so a single mount
        serves them all; a launch-directory process only ever has the one. A
        channel pointed somewhere else by config, or an explicit override,
        falls outside it -- ``check_workdir_mounted`` refuses those rather than
        letting a turn run against a directory the VM cannot see.

        ``explicit_workdir`` wins here exactly as it does in ``resolve()``:
        when set, every session resolves to it regardless of policy, so the
        mount must follow it too.
        """
        if self._explicit_workdir is not None:
            return self._explicit_workdir
        if self._policy is WorkdirPolicy.LAUNCH_DIR:
            return self._launch_dir
        return self._session_root
