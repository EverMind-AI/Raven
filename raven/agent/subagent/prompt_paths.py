# -*- coding: utf-8 -*-
"""Root-confinement guard for author-provided DAG file references."""

import os
import posixpath

from raven.agent.subagent.prompt_errors import DagValidationError

# Prefix that aims a reference at this session's node artifacts instead of the
# session working directory. They sit under the session's metadata directory
# (raven/agent/subagent/history.py), a protected subtree no working directory
# can ever be aimed at -- so a relative path from the workdir never reaches one,
# and without this prefix a graph has no short way to name another node's files.
NODES_PREFIX = "@nodes/"


def split_reference(path: str) -> tuple[str, str]:
    """Split an author-provided reference into its root and relative part.

    Args:
        path (`str`):
            The reference path taken from a node spec.

    Returns:
        `tuple[str, str]`:
            ``("nodes", <path under the node root>)`` for a ``@nodes/``-prefixed
            reference, else ``("workdir", path)``.
    """
    if path.startswith(NODES_PREFIX):
        return "nodes", path[len(NODES_PREFIX) :]
    return "workdir", path


def check_confined(path: str, *, what: str, roots: tuple[str, ...] | None = None) -> None:
    """Reject a reference that lands outside every root a node may read.

    A ``@nodes/`` reference is always confined to this session's own node
    artifacts and is checked lexically against that prefix alone --
    ``@nodes/../..`` is refused exactly like a bare ``../..``.

    Everything else is checked against ``roots``: the session working directory
    and ``<session_dir>/subagents/``, this conversation's sub-agent history. The
    second one is in the set so a graph can name an earlier run's output by
    absolute path, and reach the ``spawn`` records beside it, rather than only
    what ``@nodes/`` addresses.

    It stops there rather than at agent home on purpose. Agent home also holds
    ``user_memory/``, ``skills/``, and every *other* conversation's transcript
    and sub-agent history -- the three that ``workdir.py`` lists as
    ``_PROTECTED_SUBTREES`` and refuses to let a working directory be aimed at.
    A graph is LLM-authored and auto-run, and a ``ref`` renders file *contents*
    into a prompt handed to a third-party sub-agent, so a root spanning agent
    home would make those subtrees readable through the one path a
    ``[no-local-files]`` backend has. This root is inside the session that
    submitted the graph, which is the material it is already working on.

    Containment is decided on the path the reference resolves to on disk, not on
    the string the author wrote: a symlink inside a root can name a target
    outside every one of them, and the read follows the link. Both sides go
    through ``realpath``, so a root reached through a symlink still contains its
    own files. That makes this a local-filesystem question -- every caller reads
    through a local backend today; a remote one would have to ask its own side.

    ``roots`` is optional because the two callers know different things: the
    tool checks a graph before dispatching anything and knows both roots, while
    a caller that has neither (a direct ``validate_and_order``) gets the
    stricter shape-only rule -- relative, and not escaping its base.

    Args:
        path (`str`):
            The reference path taken from a node spec.
        what (`str`):
            A short label used in the error message (e.g. ``"ref"``).
        roots (`tuple[str, ...] | None`):
            Absolute directories the reference may resolve into; the first is
            the base a relative path resolves against. ``None`` checks the
            shape only.

    Raises:
        `DagValidationError`:
            When ``path`` is empty, or resolves outside every root.
    """
    root, relative = split_reference(path)
    if not relative:
        raise DagValidationError(f"{what} path '{path}' is empty")
    if root == "nodes" or not roots:
        _check_shape(path, relative, what=what, root=root)
        return

    base = posixpath.normpath(roots[0])
    resolved = posixpath.normpath(relative if posixpath.isabs(relative) else posixpath.join(base, relative))
    physical = os.path.realpath(resolved)
    if not any(within(physical, os.path.realpath(candidate)) for candidate in roots):
        # Only name the resolved path when it says something the given one does
        # not -- for an absolute reference the two are the same string. The
        # physical path is deliberately not shown: it can name a directory the
        # author was never entitled to learn about.
        where = f"'{path}'" if resolved == path else f"'{path}' (-> '{resolved}')"
        raise DagValidationError(
            f"{what} path {where} is outside the session workdir and its sub-agent history",
        )


def _check_shape(path: str, relative: str, *, what: str, root: str) -> None:
    """Refuse an absolute or base-escaping path without resolving it."""
    normalized = posixpath.normpath(relative)
    if posixpath.isabs(relative) or relative.startswith("\\") or normalized == ".." or normalized.startswith("../"):
        where = "this session's node artifacts" if root == "nodes" else "the session workdir"
        raise DagValidationError(
            f"{what} path '{path}' must be relative to and within {where}",
        )


def within(child: str, root: str) -> bool:
    """Whether ``child`` is ``root`` or sits under it, comparing text only.

    Text only, and given already-resolved paths by ``check_confined`` -- it
    supplies the containment half, and its caller supplies the resolution. Kept
    public because a caller comparing two paths it has resolved itself should
    use the same rule rather than writing a second one.
    """
    normalized = posixpath.normpath(root)
    return child == normalized or child.startswith(normalized.rstrip("/") + "/")
