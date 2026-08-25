"""Resolve the working directory a turn runs in.

Agent home (user memory, skills, transcripts) is global and separate; this
module only decides where a turn reads and writes files.

Today it holds the guard alone. A caller that lets somebody name a working
directory -- an editor opening a project, a flag on the command line -- has to
check it before anything runs in there, and the check is not obvious: the
dangerous answers are agent home itself, any ancestor of it, and three of its
subtrees, each for a different reason spelled out below. The per-channel default
roots and the per-turn binding that go with this in a full workdir feature are
not here yet, because nothing on this side asks where a turn should run.
"""

from __future__ import annotations

from pathlib import Path

# Subtrees of agent home the agent must not be able to adopt as a working
# directory: it would then write artifacts over its own memory and skills.
# ``sessions`` covers the subagent history too -- it lives under each session's
# own directory there.
_PROTECTED_SUBTREES = ("user_memory", "skills", "sessions")


def is_within(path: Path, root: Path) -> bool:
    """Whether ``path`` sits inside ``root``, comparing physical paths.

    A validated override is resolved through symlinks (``validate_override``
    calls ``.resolve()``) while a workspace root generally is not. Comparing an
    unresolved and a resolved path with ``is_relative_to`` can disagree about a
    directory that is, on disk, the very same place.
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
    # (provider tokens), cron/, sentinel/, logs/. A per-turn checkpoint that runs
    # `add -A` over the working directory, and a `.raven/` exclude that cannot
    # help when `.raven` *is* the work-tree root, would commit every credential
    # into a shadow git repo.
    if resolved in home.parents:
        raise ValueError(f"working directory must not contain the agent home directory ({home})")
    for subtree in _PROTECTED_SUBTREES:
        candidate = home / subtree
        if resolved == candidate or candidate in resolved.parents:
            raise ValueError(f"working directory must not be inside the agent's {subtree} tree ({candidate})")
    return resolved


__all__ = ["is_within", "validate_override"]
